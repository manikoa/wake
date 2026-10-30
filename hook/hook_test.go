package hook

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"io"
	"log"
	"math/rand/v2"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"
	"unicode/utf8"
)

var update = flag.Bool("update", false, "rewrite testdata/otlp.json")

// event reads one host's hook event from testdata/events, built from the vendor's own
// documented examples.
func event(t *testing.T, host, name string) []byte {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("testdata", "events", host+".json"))
	if err != nil {
		t.Fatal(err)
	}
	var events map[string]json.RawMessage
	if err := json.Unmarshal(data, &events); err != nil {
		t.Fatal(err)
	}
	e, ok := events[name]
	if !ok {
		t.Fatalf("no %s event %q", host, name)
	}
	return e
}

// options gives a test its own directory, a clock that moves a millisecond each time
// it's read, and randomness that's the same every run.
func options(t *testing.T) Options {
	now := time.Date(2026, 10, 29, 12, 0, 0, 0, time.UTC)
	return Options{
		Dir:  t.TempDir(),
		Now:  func() time.Time { now = now.Add(time.Millisecond); return now },
		Rand: rand.NewChaCha8([32]byte{}),
	}
}

// antigravityEvents are what each of Antigravity's hooks is told on its command line,
// since its input doesn't say.
var antigravityEvents = map[string]string{
	"pre_tool": "PreToolUse", "post_tool": "PostToolUse", "post_tool_failure": "PostToolUse", "stop": "Stop",
}

func handle(t *testing.T, o Options, host, name string) string {
	t.Helper()
	if host == "antigravity" {
		o.Event = antigravityEvents[name]
	}
	return string(Handle(Hosts[host], event(t, host, name), o))
}

// spooled reads the events waiting in o's spool, oldest first.
func spooled(t *testing.T, o Options) []span {
	t.Helper()
	var spans []span
	for _, data := range spooledRaw(t, o) {
		var s span
		if err := json.Unmarshal(data, &s); err != nil {
			t.Fatal(err)
		}
		spans = append(spans, s)
	}
	return spans
}

func spooledRaw(t *testing.T, o Options) []json.RawMessage {
	t.Helper()
	dir := filepath.Join(o.Dir, spoolDir)
	names, _ := readNames(dir)
	sort.Strings(names)
	var events []json.RawMessage
	for _, n := range names {
		if _, ok := spooledSize(n); !ok {
			continue
		}
		data, err := os.ReadFile(filepath.Join(dir, n))
		if err != nil {
			t.Fatal(err)
		}
		events = append(events, data)
	}
	return events
}

func (s span) get(key string) string {
	for _, kv := range s.Attributes {
		if kv.Key == key && kv.Value.String != nil {
			return *kv.Value.String
		}
	}
	return ""
}

func (s span) list(key string) []string {
	for _, kv := range s.Attributes {
		if kv.Key == key && kv.Value.Array != nil {
			out := []string{}
			for _, v := range kv.Value.Array.Values {
				out = append(out, *v.String)
			}
			return out
		}
	}
	return nil
}

func (s span) payload(t *testing.T) map[string]any {
	t.Helper()
	var p map[string]any
	if err := json.Unmarshal([]byte(s.get("wake.payload")), &p); err != nil {
		t.Fatal(err)
	}
	return p
}

func writeRules(t *testing.T, o Options, agent string, age time.Duration, rules string) {
	t.Helper()
	data, _ := json.Marshal(rulesFile{Agent: agent, Fetched: o.Now().Add(-age).UnixNano(), Rules: json.RawMessage(rules)})
	if err := write(rulesPath(o.Dir, agent), data, o.hex); err != nil {
		t.Fatal(err)
	}
}

func quiet() *log.Logger { return log.New(io.Discard, "", 0) }

func TestRecordsEachHostsActions(t *testing.T) {
	for _, c := range []struct {
		host, event, kind, tool, callID string
		output                          any // the payload's output, when it's checked
	}{
		{"claude-code", "prompt", "agent_message", "", "", nil},
		{"claude-code", "pre_tool", "", "", "", nil},
		{"claude-code", "post_tool", "tool_call", "Write", "toolu_01DEF456", map[string]any{"filePath": "/Users/me/proj/factorial.py", "type": "create"}},
		{"claude-code", "post_tool_failure", "tool_call", "Bash", "toolu_01ABC123", map[string]any{"error": "Exit code 1\nError: Cannot find module 'express'"}},
		{"codex", "prompt", "agent_message", "", "", nil},
		{"codex", "pre_tool", "", "", "", nil},
		{"codex", "post_tool", "tool_call", "apply_patch", "call_7Qx2", "Success. Updated the following files:\nM utils.py\n"},
		{"cursor", "prompt", "agent_message", "", "", nil},
		{"cursor", "pre_tool", "", "", "", nil},
		{"cursor", "post_tool", "tool_call", "Shell", "tool_9d1", map[string]any{"exit_code": 0.0, "stdout": "added 12 packages"}},
		{"cursor", "before_shell", "", "", "", nil},
		{"cursor", "after_shell", "tool_call", "Shell", "", "<!doctype html><html>...</html>"},
		{"cursor", "before_mcp", "", "", "", nil},
		{"cursor", "after_mcp", "tool_call", "create_issue", "", map[string]any{"id": "LIN-12"}},
		{"cursor", "read_file", "tool_call", "read_file", "", "API_URL=https://api.example.com\n"},
		{"cursor", "edit_file", "tool_call", "edit_file", "", nil},
		{"cursor", "subagent_start", "", "", "", nil},
		{"cursor", "subagent_post_tool", "tool_call", "Grep", "tool_c03", map[string]any{"matches": []any{"src/session.ts:12"}}},
		{"cursor", "subagent_stop", "", "", "", nil},
		{"grok", "prompt", "agent_message", "", "", nil},
		{"grok", "pre_tool", "", "", "", nil},
		{"grok", "post_tool", "tool_call", "Bash", "", nil},
		{"gemini", "prompt", "agent_message", "", "", nil},
		{"gemini", "pre_tool", "", "", "", nil},
		{"gemini", "post_tool", "tool_call", "run_shell_command", "", map[string]any{
			"llmContent": "Command: npm run build\nExit Code: 1\nError: Cannot find module 'vite'", "returnDisplay": "Exit Code: 1"}},
		{"antigravity", "pre_tool", "", "", "", nil},
		{"antigravity", "post_tool", "tool_call", "run_command", "", nil},
		{"antigravity", "post_tool_failure", "tool_call", "run_command", "", map[string]any{"error": "exit status 1"}},
	} {
		t.Run(c.host+"/"+c.event, func(t *testing.T) {
			o := options(t)
			handle(t, o, c.host, c.event)
			spans := spooled(t, o)
			if c.kind == "" {
				if len(spans) != 0 {
					t.Fatalf("recorded %d events for an event that only asks whether to go ahead", len(spans))
				}
				return
			}
			if len(spans) != 1 {
				t.Fatalf("recorded %d events, want 1", len(spans))
			}
			s := spans[0]
			for key, want := range map[string]string{
				"wake.event.kind":     c.kind,
				"wake.capture":        "hook",
				"wake.hook.host":      c.host,
				"wake.agent.id":       c.host,
				"gen_ai.tool.name":    c.tool,
				"gen_ai.tool.call.id": c.callID,
			} {
				if got := s.get(key); got != want {
					t.Errorf("%s = %q, want %q", key, got, want)
				}
			}
			p := s.payload(t)
			if _, ok := p["hook"].(map[string]any); !ok {
				t.Error("the host's other fields weren't kept under hook")
			}
			if c.output != nil && !reflect.DeepEqual(p["output"], c.output) {
				t.Errorf("output = %#v, want %#v", p["output"], c.output)
			}
			if c.kind == "agent_message" && (p["from"] != "user" || p["to"] != c.host || p["content"] == nil) {
				t.Errorf("prompt payload = %v", p)
			}
		})
	}
}

func TestLinksASessionsEvents(t *testing.T) {
	o := options(t)
	handle(t, o, "claude-code", "prompt")
	handle(t, o, "claude-code", "post_tool")
	handle(t, o, "claude-code", "subagent_post_tool")
	other := bytes.Replace(event(t, "claude-code", "post_tool"), []byte(`"abc123"`), []byte(`"xyz789"`), 1)
	Handle(Hosts["claude-code"], other, o)
	spans := spooled(t, o)
	if len(spans) != 4 {
		t.Fatalf("recorded %d events, want 4", len(spans))
	}
	prompt, tool, sub, elsewhere := spans[0], spans[1], spans[2], spans[3]

	if prompt.TraceID != tool.TraceID || tool.TraceID != sub.TraceID {
		t.Error("a session's events don't share a trace")
	}
	if elsewhere.TraceID == prompt.TraceID {
		t.Error("another session's events share the first session's trace")
	}
	for _, s := range []span{tool, sub} {
		if got := s.list("wake.parents"); !reflect.DeepEqual(got, []string{prompt.get("wake.event.id")}) {
			t.Errorf("%s's parents = %v, want the session's prompt", s.Name, got)
		}
	}
	if got := elsewhere.list("wake.parents"); got != nil {
		t.Errorf("another session's call took parents %v", got)
	}
	if got := prompt.list("wake.origins"); !reflect.DeepEqual(got, []string{"user"}) {
		t.Errorf("prompt's origins = %v", got)
	}
	if got := tool.list("wake.origins"); !reflect.DeepEqual(got, []string{"tool:Write"}) {
		t.Errorf("tool call's origins = %v", got)
	}
	if got := tool.get("wake.agent.instance"); got != "claude-code#abc123" {
		t.Errorf("session instance = %q", got)
	}
	if got := tool.list("wake.agent.lineage"); !reflect.DeepEqual(got, []string{"claude-code"}) {
		t.Errorf("session lineage = %v", got)
	}
	if got := sub.get("wake.agent.instance"); got != "claude-code/Explore#agent-abc123" {
		t.Errorf("subagent instance = %q", got)
	}
	if got := sub.list("wake.agent.lineage"); !reflect.DeepEqual(got, []string{"claude-code", "claude-code#abc123"}) {
		t.Errorf("subagent lineage = %v", got)
	}
	if tool.Start == tool.End {
		t.Error("the call's duration was lost")
	}
}

func TestNamesTheAgentItsGiven(t *testing.T) {
	o := options(t)
	o.Agent = "payments-team"
	handle(t, o, "cursor", "post_tool")
	s := spooled(t, o)[0]
	if s.get("wake.agent.id") != "payments-team" || s.get("wake.agent.instance") != "payments-team#conv-8f2a" {
		t.Errorf("agent %q, instance %q", s.get("wake.agent.id"), s.get("wake.agent.instance"))
	}
}

func TestCountsEventsItDoesntHandle(t *testing.T) {
	o := options(t)
	for _, c := range []struct{ host, event string }{
		{"claude-code", "session_start"}, {"cursor", "stop"}, {"codex", "stop"}, {"gemini", "session_start"}, {"antigravity", "stop"},
	} {
		if out := handle(t, o, c.host, c.event); out != "" {
			t.Errorf("told %s %q for an event it doesn't handle", c.host, out)
		}
	}
	if out := Handle(Hosts["grok"], []byte("not json"), o); out != nil {
		t.Errorf("told grok %q for an event it couldn't read", out)
	}
	if n := len(spooled(t, o)); n != 0 {
		t.Errorf("recorded %d events it doesn't handle", n)
	}
	var logged bytes.Buffer
	(&Server{Dir: o.Dir, Log: log.New(&logged, "", 0)}).Pass(context.Background())
	for _, want := range []string{"claude_code.SessionStart", "cursor.stop", "codex.Stop", "gemini.SessionStart", "antigravity.Stop", "grok.unreadable"} {
		if !strings.Contains(logged.String(), want) {
			t.Errorf("serve didn't count %s:\n%s", want, logged.String())
		}
	}
	if names, _ := readNames(filepath.Join(o.Dir, unknownDir)); len(names) != 0 {
		t.Error("serve left the marks it counted")
	}
}

func TestRefusesWhatAHaltCovers(t *testing.T) {
	for _, c := range []struct {
		name, host, event, halts string
		refused                  bool
		told                     string // part of what the host is told
	}{
		{"the agent", "claude-code", "pre_tool", `[{"id":"h1","agents":["claude-code"]}]`, true, `"permissionDecision":"deny"`},
		{"its session", "claude-code", "pre_tool", `[{"id":"h1","agents":["claude-code#abc123"]}]`, true, `"permissionDecision":"deny"`},
		{"another session", "claude-code", "pre_tool", `[{"id":"h1","agents":["claude-code#other"]}]`, false, ""},
		{"the fleet", "codex", "pre_tool", `[{"id":"h1","fleet":true}]`, true, `"permissionDecision":"deny"`},
		{"a prompt", "codex", "prompt", `[{"id":"h1","fleet":true}]`, true, `"decision":"block"`},
		{"nothing", "cursor", "pre_tool", `[]`, false, `{}`},
		{"cursor's tool", "cursor", "pre_tool", `[{"id":"h1","agents":["cursor"]}]`, true, `"permission":"deny"`},
		{"cursor's shell", "cursor", "before_shell", `[{"id":"h1","agents":["cursor"]}]`, true, `"permission":"deny"`},
		{"cursor's read", "cursor", "read_file", `[{"id":"h1","agents":["cursor"]}]`, true, `"permission":"deny"`},
		{"cursor's subagent", "cursor", "subagent_start", `[{"id":"h1","agents":["cursor"]}]`, true, `"permission":"deny"`},
		{"cursor's prompt", "cursor", "prompt", `[{"id":"h1","agents":["cursor"]}]`, true, `"continue":false`},
		{"cursor's prompt let through", "cursor", "prompt", `[]`, false, `{"continue":true}`},
		{"grok's tool", "grok", "pre_tool", `[{"id":"h1","agents":["grok"]}]`, true, `"decision":"deny"`},
		{"gemini's tool", "gemini", "pre_tool", `[{"id":"h1","agents":["gemini"]}]`, true, `"decision":"deny"`},
		{"gemini's prompt", "gemini", "prompt", `[{"id":"h1","agents":["gemini"]}]`, true, `"decision":"deny"`},
		{"antigravity's tool", "antigravity", "pre_tool", `[{"id":"h1","agents":["antigravity#ec33ebf9-0cba-4100-8142-c61503f6c587"]}]`, true, `"decision":"deny"`},
		{"antigravity's tool let through", "antigravity", "pre_tool", `[]`, false, `{"decision":"allow"}`},
	} {
		t.Run(c.name, func(t *testing.T) {
			o := options(t)
			o.Stop = true
			writeRules(t, o, c.host, 0, `{"halts":`+c.halts+`}`)
			out := handle(t, o, c.host, c.event)
			if !strings.Contains(out, c.told) || c.told == "" && out != "" {
				t.Errorf("told the host %q, want %q", out, c.told)
			}
			spans := spooled(t, o)
			if !c.refused {
				for _, s := range spans {
					if s.get("wake.event.kind") == "denied" {
						t.Error("recorded a refusal for a call it let through")
					}
				}
				return
			}
			if !strings.Contains(out, "h1") {
				t.Errorf("the refusal %q doesn't name the halt", out)
			}
			if len(spans) != 1 || spans[0].get("wake.event.kind") != "denied" || spans[0].get("wake.brake.halt_id") != "h1" {
				t.Fatalf("recorded %+v, want one denied event naming h1", spans)
			}
			if p := spans[0].payload(t); p["output"] != refusal("h1") || p["input"] == nil {
				t.Errorf("refusal payload = %v", p)
			}
			if spans[0].list("wake.origins") != nil {
				t.Error("a refusal has origins, though nothing entered")
			}
		})
	}
}

// An event that can't be read could be any, a tool call or a prompt, so with --stop it's
// refused the one way the hosts share: on stderr, with exit code 2.
func TestRefusesAnEventItCantRead(t *testing.T) {
	for _, in := range []string{"", "not json", "null", "[1]", `"PreToolUse"`, `{"hook_event_name":"PreToolUse","tool_name":`} {
		o := options(t)
		if out, stderr := Answer(Hosts["claude-code"], []byte(in), o); out != nil || stderr != "" {
			t.Errorf("%q: answered %q, %q without --stop", in, out, stderr)
		}
		o.Stop = true
		if out, stderr := Answer(Hosts["claude-code"], []byte(in), o); out != nil || stderr != Unreadable {
			t.Errorf("%q: answered %q, %q with --stop", in, out, stderr)
		}
		if names, _ := readNames(filepath.Join(o.Dir, unknownDir)); len(names) != 2 {
			t.Errorf("%q: counted %v", in, names)
		}
	}
	// Anything it can read is answered on stdout.
	o := options(t)
	o.Stop = true
	if out, stderr := Answer(Hosts["claude-code"], event(t, "claude-code", "pre_tool"), o); stderr != "" || !strings.Contains(string(out), refusal(StaleRules)) {
		t.Errorf("answered %q, %q", out, stderr)
	}
}

// Antigravity's hooks are told their event on the command line. One that isn't can't tell
// what it's answering: it records nothing, and where it would check halts it refuses.
func TestAntigravityHooksNameTheirEvent(t *testing.T) {
	o := options(t)
	in := event(t, "antigravity", "pre_tool")
	if out := Handle(Hosts["antigravity"], in, o); out != nil {
		t.Errorf("told the host %q with no event and no --stop", out)
	}
	o.Stop = true
	writeRules(t, o, "antigravity", 0, `{"halts":[]}`)
	if out := string(Handle(Hosts["antigravity"], in, o)); !strings.Contains(out, `"decision":"deny"`) || !strings.Contains(out, refusal(HaltCheckFailed)) {
		t.Errorf("told the host %q with no event and --stop, want a refusal", out)
	}
	if n := len(spooled(t, o)); n != 0 {
		t.Errorf("recorded %d events it couldn't read", n)
	}
	if names, _ := readNames(filepath.Join(o.Dir, unknownDir)); len(names) != 2 || !strings.Contains(names[0], "antigravity.no_event") {
		t.Errorf("counted %v, want two marks of antigravity.no_event", names)
	}
}

func TestFailsClosed(t *testing.T) {
	o := options(t)
	o.Stop = true
	refused := func(halt string) {
		t.Helper()
		if out := handle(t, o, "claude-code", "pre_tool"); !strings.Contains(out, `"deny"`) || !strings.Contains(out, refusal(halt)) {
			t.Errorf("told the host %q, want a refusal under %s", out, halt)
		}
	}
	asked := func(why string) {
		t.Helper()
		if _, err := os.Stat(wantPath(o.Dir, "claude-code")); err != nil {
			t.Errorf("serve wasn't asked for the agent's rules %s", why)
		}
		os.Remove(wantPath(o.Dir, "claude-code"))
	}
	refused(StaleRules)
	asked("before it had any")
	// A serve that restarted might not know the agent: stale rules ask again.
	writeRules(t, o, "claude-code", 31*time.Second, `{"halts":[]}`)
	refused(StaleRules)
	asked("once they went stale")
	writeRules(t, o, "claude-code", 0, `{"halts":[{"id":"h1","fleet":"yes"}]}`)
	refused(HaltCheckFailed)
	writeRules(t, o, "claude-code", 0, `{"halts":null}`)
	refused(HaltCheckFailed)
	writeRules(t, o, "claude-code", 0, `null`)
	refused(HaltCheckFailed)
	if err := write(rulesPath(o.Dir, "claude-code"), []byte("{"), o.hex); err != nil {
		t.Fatal(err)
	}
	refused(HaltCheckFailed)
	asked("when it couldn't read them")

	// A wake-proxy older than stops in the agent's process says nothing of halts: only a
	// missing field means that.
	writeRules(t, o, "claude-code", 0, `{"quarantines":[]}`)
	if out := handle(t, o, "claude-code", "pre_tool"); out != "" {
		t.Errorf("told the host %q with no halts to go on", out)
	}
	// Without --stop, nothing's refused.
	o.Stop = false
	writeRules(t, o, "claude-code", 0, `{"halts":[{"id":"h1","fleet":true}]}`)
	if out := handle(t, o, "claude-code", "pre_tool"); out != "" {
		t.Errorf("told the host %q without --stop", out)
	}
}

func TestSpoolIsBounded(t *testing.T) {
	defer func(n int) { maxSpoolEvents = n }(maxSpoolEvents)
	maxSpoolEvents = 2
	o := options(t)
	for range 3 {
		handle(t, o, "claude-code", "post_tool")
	}
	if n := len(spooled(t, o)); n != 2 {
		t.Errorf("spooled %d events, want 2", n)
	}
	if names, _ := readNames(filepath.Join(o.Dir, droppedDir)); len(names) != 1 {
		t.Errorf("counted %d drops, want 1", len(names))
	}
}

func TestCutsLargePayloads(t *testing.T) {
	o := options(t)
	in, _ := json.Marshal(map[string]any{
		"conversation_id": "conv-1",
		"hook_event_name": "beforeReadFile",
		"file_path":       "/big.txt",
		"content":         strings.Repeat("é", 3<<20), // 6 MiB
	})
	Handle(Hosts["cursor"], in, o)
	p := spooled(t, o)[0].payload(t)
	prefix, _ := p["prefix"].(string)
	size, _ := p["bytes"].(float64)
	if p["truncated"] != true || size <= maxPayload || len(prefix) > prefixLen || len(prefix) < prefixLen-4 || !utf8.ValidString(prefix) {
		t.Errorf("payload cut to truncated=%v bytes=%v prefix of %d bytes", p["truncated"], size, len(prefix))
	}
}

// testdata/otlp.json is what serve sends for a short session: a prompt, a tool call and a
// refusal. The collector's tests sign it, so the two stay in step.
func TestSendsOTLPTheCollectorReads(t *testing.T) {
	o := options(t)
	handle(t, o, "claude-code", "prompt")
	handle(t, o, "claude-code", "post_tool")
	o.Stop = true
	writeRules(t, o, "claude-code", 0, `{"halts":[{"id":"h1","agents":["claude-code#abc123"]}]}`)
	handle(t, o, "claude-code", "pre_tool")

	var got bytes.Buffer
	json.Indent(&got, exportRequest(spooledRaw(t, o), "test"), "", "  ")
	got.WriteByte('\n')
	path := filepath.Join("testdata", "otlp.json")
	if *update {
		if err := os.WriteFile(path, got.Bytes(), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(bytes.ReplaceAll(want, []byte("\r\n"), []byte("\n")), got.Bytes()) {
		t.Errorf("what's sent differs from %s; run go test -update if that's meant", path)
	}
}
