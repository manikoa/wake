package hook

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
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

func TestServeFetchesTheRules(t *testing.T) {
	var queries []url.Values
	rules := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		queries = append(queries, r.URL.Query())
		w.Write([]byte(`{"halts":[{"id":"h9","agents":["claude-code"]}]}`))
	}))
	defer rules.Close()
	o := options(t)
	o.Stop = true

	// Without rules the hook refuses, and asks serve for them.
	if out := handle(t, o, "claude-code", "pre_tool"); !strings.Contains(out, refusal(StaleRules)) {
		t.Fatalf("told the host %q before any rules", out)
	}
	s := &Server{Dir: o.Dir, Rules: rules.URL, Client: rules.Client(), Log: quiet(), Now: o.Now}
	s.Pass(context.Background())
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
	if len(queries) != 2 {
		t.Errorf("fetched %d times over two passes", len(queries))
	}
}
