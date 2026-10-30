package hook

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"io/fs"
	"log"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// collector stands in for the collector's OTLP/HTTP receiver, answering each request
// with what answer says.
func collector(t *testing.T, answer func(body []byte) int) (*httptest.Server, *[][]byte) {
	var mu sync.Mutex
	var bodies [][]byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/traces" || r.Header.Get("Content-Type") != "application/json" {
			t.Errorf("sent %s %s", r.URL.Path, r.Header.Get("Content-Type"))
		}
		body, _ := io.ReadAll(r.Body)
		mu.Lock()
		bodies = append(bodies, body)
		mu.Unlock()
		w.WriteHeader(answer(body))
	}))
	t.Cleanup(srv.Close)
	return srv, &bodies
}

func spansIn(t *testing.T, body []byte) int {
	t.Helper()
	var req struct {
		ResourceSpans []struct {
			ScopeSpans []struct {
				Spans []json.RawMessage `json:"spans"`
			} `json:"scopeSpans"`
		} `json:"resourceSpans"`
	}
	if err := json.Unmarshal(body, &req); err != nil {
		t.Fatal(err)
	}
	n := 0
	for _, rs := range req.ResourceSpans {
		for _, ss := range rs.ScopeSpans {
			n += len(ss.Spans)
		}
	}
	return n
}

func TestServeSendsTheSpool(t *testing.T) {
	srv, bodies := collector(t, func([]byte) int { return http.StatusOK })
	o := options(t)
	handle(t, o, "claude-code", "prompt")
	handle(t, o, "claude-code", "post_tool")
	handle(t, o, "cursor", "edit_file")
	(&Server{Dir: o.Dir, Collector: srv.URL, Client: srv.Client(), Log: quiet()}).Pass(context.Background())
	if len(*bodies) != 1 || spansIn(t, (*bodies)[0]) != 3 {
		t.Fatalf("sent %d requests, want one of 3 events", len(*bodies))
	}
	if n := len(spooled(t, o)); n != 0 {
		t.Errorf("%d sent events left in the spool", n)
	}
}

func TestServeKeepsEventsWhileTheCollectorIsDown(t *testing.T) {
	srv, _ := collector(t, func([]byte) int { return http.StatusServiceUnavailable })
	o := options(t)
	handle(t, o, "claude-code", "post_tool")
	var logged bytes.Buffer
	(&Server{Dir: o.Dir, Collector: srv.URL, Client: srv.Client(), Log: log.New(&logged, "", 0)}).Pass(context.Background())
	if n := len(spooled(t, o)); n != 1 {
		t.Errorf("%d events in the spool, want the 1 the collector couldn't take", n)
	}
	if !strings.Contains(logged.String(), "retrying later") {
		t.Errorf("didn't log the failure: %q", logged.String())
	}
}

func TestServeSetsAsideAnEventTheCollectorRefuses(t *testing.T) {
	srv, bodies := collector(t, func(body []byte) int {
		if bytes.Contains(body, []byte("edit_file")) {
			return http.StatusBadRequest
		}
		return http.StatusOK
	})
	o := options(t)
	handle(t, o, "claude-code", "post_tool")
	handle(t, o, "cursor", "edit_file")
	(&Server{Dir: o.Dir, Collector: srv.URL, Client: srv.Client(), Log: quiet()}).Pass(context.Background())
	if n := len(spooled(t, o)); n != 0 {
		t.Errorf("%d events left in the spool", n)
	}
	if names, _ := readNames(filepath.Join(o.Dir, rejectedDir)); len(names) != 1 {
		t.Errorf("set aside %d events, want the 1 refused", len(names))
	}
	// The batch, then each event alone.
	if len(*bodies) != 3 {
		t.Errorf("sent %d requests, want 3", len(*bodies))
	}
}

// wakeProxy stands in for wake-proxy's rules, served where the SDKs fetch them, answering
// each fetch with body.
func wakeProxy(t *testing.T, body string) (*httptest.Server, *[]url.Values) {
	var mu sync.Mutex
	var queries []url.Values
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/memory" {
			t.Errorf("fetched %s, want /v1/memory", r.URL.Path)
			http.NotFound(w, r)
			return
		}
		mu.Lock()
		queries = append(queries, r.URL.Query())
		mu.Unlock()
		w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	return srv, &queries
}

func TestServeFetchesTheRules(t *testing.T) {
	rules, fetched := wakeProxy(t, `{"halts":[{"id":"h9","agents":["claude-code"]}]}`)
	o := options(t)
	o.Stop = true

	// Without rules the hook refuses, and asks serve for them.
	if out := handle(t, o, "claude-code", "pre_tool"); !strings.Contains(out, refusal(StaleRules)) {
		t.Fatalf("told the host %q before any rules", out)
	}
	// Given wake-proxy's address, as the SDKs are, a slash at its end or not.
	s := &Server{Dir: o.Dir, Rules: rules.URL + "/", Client: rules.Client(), Log: quiet(), Now: o.Now}
	s.Pass(context.Background())
	queries := *fetched
	if len(queries) != 1 || queries[0].Get("agent") != "claude-code" || queries[0].Get("process") == "" {
		t.Fatalf("fetched rules with %v, want the agent and this process named", queries)
	}
	if _, err := os.Stat(wantPath(o.Dir, "claude-code")); !os.IsNotExist(err) {
		t.Error("the hook's ask for rules is still there")
	}
	if out := handle(t, o, "claude-code", "pre_tool"); !strings.Contains(out, "h9") {
		t.Errorf("told the host %q, want a refusal under h9", out)
	}
	// It goes on fetching the agent's rules on every pass.
	s.Pass(context.Background())
	if len(*fetched) != 2 {
		t.Errorf("fetched %d times over two passes", len(*fetched))
	}
}

// A serve that restarted fetches the rules of every agent a hook asked for before, though
// the hooks asked only once: theirs would go stale, and every call be refused, for good.
func TestServeFetchesForTheAgentsWhoseRulesItFinds(t *testing.T) {
	rules, fetched := wakeProxy(t, `{"halts":[]}`)
	o := options(t)
	o.Stop, o.Agent = true, "payments-team"
	writeRules(t, o, "payments-team", time.Minute, `{"halts":[]}`)
	write(filepath.Join(o.Dir, rulesDir, "junk.json"), []byte("{"), o.hex)
	(&Server{Dir: o.Dir, Rules: rules.URL, Client: rules.Client(), Log: quiet(), Now: o.Now}).Pass(context.Background())
	if len(*fetched) != 1 || (*fetched)[0].Get("agent") != "payments-team" {
		t.Fatalf("fetched %v, want the rules of the agent it found", *fetched)
	}
	if out := handle(t, o, "cursor", "pre_tool"); out != "{}" {
		t.Errorf("told the host %q, want the call let through with fresh rules", out)
	}
}

func TestServeRefusesRulesThatArentAnObject(t *testing.T) {
	rules, _ := wakeProxy(t, `null`)
	o := options(t)
	var logged bytes.Buffer
	(&Server{Dir: o.Dir, Rules: rules.URL, Agents: []string{"claude-code"}, Client: rules.Client(), Log: log.New(&logged, "", 0), Now: o.Now}).Pass(context.Background())
	if _, err := os.Stat(rulesPath(o.Dir, "claude-code")); !os.IsNotExist(err) {
		t.Error("kept rules of null")
	}
	if !strings.Contains(logged.String(), "not a JSON object") {
		t.Errorf("logged %q", logged.String())
	}
}

// What entered the agents of a session nothing has entered for a day is swept away: no
// policy's window is longer.
func TestServeForgetsSessionsIdleForADay(t *testing.T) {
	o := options(t)
	Handle(Hosts["claude-code"], fetch, o)
	handle(t, o, "cursor", "read_file")
	idle := o.flowStore("claude-code", "s1", 0).dir
	busy := o.flowStore("cursor", "conv-8f2a", 0).dir
	now := time.Now()
	filepath.WalkDir(idle, func(path string, _ fs.DirEntry, _ error) error {
		return os.Chtimes(path, now, now.Add(-25*time.Hour))
	})
	s := &Server{Dir: o.Dir, Log: quiet(), Now: func() time.Time { return now }}
	s.Pass(context.Background())
	if _, err := os.Stat(idle); !os.IsNotExist(err) {
		t.Error("kept a session idle for a day")
	}
	if _, err := os.Stat(busy); err != nil {
		t.Errorf("swept a session in use: %v", err)
	}
}
