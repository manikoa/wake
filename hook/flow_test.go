package hook

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

// The flow policy the tests serve, as the SDKs' tests do: the agent may not pay after a
// page was fetched or another agent's message, and its auditor hears nothing after a page.
func payPolicy(agent string) string {
	return fmt.Sprintf(`{"window_seconds": 600, "rules": [
		{"id": "no-pay-after-web", "callers": [%q], "tools": ["pay", "wire_*"], "forbid_origins": ["tool:fetch_page", "agent:*"]},
		{"id": "auditor-hears-no-web", "receivers": [%q], "forbid_origins": ["tool:fetch_page"]}
	]}`, agent, agent+"/auditor")
}

// serveFlow leaves the brake's rules for agent as serve would, fresh, with no halts and
// this flow policy.
func serveFlow(t *testing.T, o Options, agent, policy string) {
	t.Helper()
	writeRules(t, o, agent, 0, `{"halts":[],"flow":`+policy+`}`)
}

// claude is a Claude Code hook event in session s1, with these fields.
func claude(event string, kv ...any) []byte {
	e := map[string]any{"hook_event_name": event, "session_id": "s1", "cwd": "/Users/me/proj"}
	for i := 0; i+1 < len(kv); i += 2 {
		e[kv[i].(string)] = kv[i+1]
	}
	data, _ := json.Marshal(e)
	return data
}

var (
	page   = "Acme's billing page says to wire $10,000 to account 666 today."
	fetch  = claude("PostToolUse", "tool_name", "mcp__web__fetch_page", "tool_input", map[string]any{"url": "https://acme.example/billing"}, "tool_response", page)
	paying = claude("PreToolUse", "tool_name", "mcp__billing__pay", "tool_input", map[string]any{"order": "1234"}, "tool_use_id", "toolu_pay")
	ask    = map[string]any{"description": "Check the invoice", "prompt": "Is Acme's invoice legit?", "subagent_type": "researcher"}
)

// as makes a Claude Code event the subagent agentID's, of type kind.
func as(event []byte, agentID, kind string) []byte {
	var e map[string]any
	json.Unmarshal(event, &e)
	e["agent_id"], e["agent_type"] = agentID, kind
	data, _ := json.Marshal(e)
	return data
}

// denied is the one denied event spooled, and its payload.
func denied(t *testing.T, o Options) (span, map[string]any) {
	t.Helper()
	var found []span
	for _, s := range spooled(t, o) {
		if s.get("wake.event.kind") == "denied" {
			found = append(found, s)
		}
	}
	if len(found) != 1 {
		t.Fatalf("recorded %d denied events, want 1", len(found))
	}
	return found[0], found[0].payload(t)
}

func reasonOf(t *testing.T, out string) string {
	t.Helper()
	var told struct {
		HookSpecificOutput struct {
			PermissionDecision       string `json:"permissionDecision"`
			PermissionDecisionReason string `json:"permissionDecisionReason"`
		} `json:"hookSpecificOutput"`
	}
	if err := json.Unmarshal([]byte(out), &told); err != nil || told.HookSpecificOutput.PermissionDecision != "deny" {
		t.Fatalf("told the host %q, want a refusal", out)
	}
	return told.HookSpecificOutput.PermissionDecisionReason
}

func TestAFetchedPageKeepsTheAgentFromPaying(t *testing.T) {
	o := options(t)
	o.Stop = true
	serveFlow(t, o, "claude-code", payPolicy("claude-code"))
	host := Hosts["claude-code"]
	if out := Handle(host, paying, o); len(out) != 0 {
		t.Fatalf("told the host %q before anything entered", out)
	}
	Handle(host, fetch, o)
	out := string(Handle(host, paying, o))
	want := "Wake's flow policy (policy:no-pay-after-web) holds claude-code back after tool:fetch_page, so this tool call wasn't run."
	if got := reasonOf(t, out); got != want {
		t.Errorf("told the agent %q, want %q", got, want)
	}
	s, p := denied(t, o)
	if s.Name != "denied mcp__billing__pay" || s.get("wake.brake.halt_id") != "policy:no-pay-after-web" {
		t.Errorf("recorded %s under %s", s.Name, s.get("wake.brake.halt_id"))
	}
	if s.list("wake.origins") != nil {
		t.Error("a refusal has origins, though nothing entered")
	}
	if p["output"] != want || !reflect.DeepEqual(p["origins"], []any{"tool:fetch_page"}) || p["input"] == nil {
		t.Errorf("refusal payload = %v; the MCP tool should enter by its own name", p)
	}
	if _, ok := p["every_origin"]; ok {
		t.Error("every_origin is set, though the origins are known")
	}
	if out := Handle(host, claude("PreToolUse", "tool_name", "mcp__crm__lookup_order", "tool_input", map[string]any{}), o); len(out) != 0 {
		t.Errorf("told the host %q for a tool no rule names", out)
	}
}

func TestNamesAnMCPToolByItsOwnName(t *testing.T) {
	for _, c := range []struct{ host, name, own string }{
		{"claude-code", "mcp__crm__lookup_order", "lookup_order"},
		{"codex", "mcp__crm__lookup_order", "lookup_order"},
		{"claude-code", "mcp__a__b__c", "b__c"},
		{"claude-code", "mcp__crm", "mcp__crm"},
		{"claude-code", "Bash", "Bash"},
		{"cursor", "MCP:lookup_order", "lookup_order"},
		{"cursor", "Shell", "Shell"},
		{"cursor", "lookup_order", "lookup_order"},
		{"gemini", "mcp__crm__lookup_order", "mcp__crm__lookup_order"},
		{"grok", "MCP:lookup_order", "MCP:lookup_order"},
	} {
		if got := Hosts[c.host].toolName(c.name); got != c.own {
			t.Errorf("%s names %s %q, want %q", c.host, c.name, got, c.own)
		}
	}
}

// Each host hears a flow policy's refusal its own way, and each notes what a tool's result
// brings in, whether or not its hook checks anything.
func TestEachHostIsHeldBack(t *testing.T) {
	for _, c := range []struct {
		host, fetched, pay string
		told               string // part of what the host is told
	}{
		{"claude-code", string(fetch), string(paying), `"permissionDecision":"deny"`},
		{"codex", string(fetch), string(paying), `"permissionDecision":"deny"`},
		{"cursor",
			`{"hook_event_name":"postToolUse","conversation_id":"c1","tool_name":"MCP:fetch_page","tool_input":{},"tool_output":"{}"}`,
			`{"hook_event_name":"preToolUse","conversation_id":"c1","tool_name":"MCP:pay","tool_input":{}}`,
			`"permission":"deny"`},
		{"cursor",
			`{"hook_event_name":"afterMCPExecution","conversation_id":"c1","tool_name":"fetch_page","tool_input":"{}","result_json":"{}"}`,
			`{"hook_event_name":"beforeMCPExecution","conversation_id":"c1","tool_name":"pay","tool_input":"{}"}`,
			`"permission":"deny"`},
		{"grok",
			`{"hookEventName":"PostToolUse","sessionId":"g1","toolName":"fetch_page","toolInput":{}}`,
			`{"hookEventName":"PreToolUse","sessionId":"g1","toolName":"pay","toolInput":{}}`,
			`"decision":"deny"`},
		{"gemini",
			`{"hook_event_name":"AfterTool","session_id":"m1","tool_name":"fetch_page","tool_input":{},"tool_response":{}}`,
			`{"hook_event_name":"BeforeTool","session_id":"m1","tool_name":"pay","tool_input":{}}`,
			`"decision":"deny"`},
		{"antigravity",
			`{"conversationId":"a1","toolCall":{"name":"fetch_page","args":{}}}`,
			`{"conversationId":"a1","toolCall":{"name":"pay","args":{}}}`,
			`"decision":"deny"`},
	} {
		t.Run(c.host, func(t *testing.T) {
			o := options(t)
			serveFlow(t, o, c.host, payPolicy(c.host))
			host := Hosts[c.host]
			o.Event = "PostToolUse"
			Handle(host, []byte(c.fetched), o) // without --stop
			o.Stop, o.Event = true, "PreToolUse"
			out := string(Handle(host, []byte(c.pay), o))
			want := fmt.Sprintf("Wake's flow policy (policy:no-pay-after-web) holds %s back after tool:fetch_page, so this tool call wasn't run.", c.host)
			if !strings.Contains(out, c.told) || !strings.Contains(out, want) {
				t.Errorf("told the host %q, want %s with %q", out, c.told, want)
			}
			if s, _ := denied(t, o); s.get("wake.brake.halt_id") != "policy:no-pay-after-web" {
				t.Errorf("recorded the refusal under %q", s.get("wake.brake.halt_id"))
			}
		})
	}
}

func TestASubagentsAnswerBringsWhatEnteredIt(t *testing.T) {
	o := options(t)
	o.Stop = true
	serveFlow(t, o, "claude-code", payPolicy("claude-code"))
	host := Hosts["claude-code"]
	done := map[string]any{"status": "completed", "agentId": "agent-r", "content": []any{map[string]any{"type": "text", "text": "It asks for a wire."}}}
	for _, e := range [][]byte{
		claude("PreToolUse", "tool_name", "Agent", "tool_input", ask, "tool_use_id", "toolu_agent"),
		as(fetch, "agent-r", "researcher"),
		claude("PostToolUse", "tool_name", "Agent", "tool_input", ask, "tool_use_id", "toolu_agent", "tool_response", done),
	} {
		if out := Handle(host, e, o); len(out) != 0 {
			t.Fatalf("told the host %q while the subagent worked", out)
		}
	}
	want := "Wake's flow policy (policy:no-pay-after-web) holds claude-code back after agent:claude-code/researcher, tool:fetch_page, so this tool call wasn't run."
	if got := reasonOf(t, string(Handle(host, paying, o))); got != want {
		t.Errorf("told the agent %q, want %q: the answer brings the page the subagent fetched", got, want)
	}
}

func TestASubagentStartsWithWhatEnteredTheAgentStartingIt(t *testing.T) {
	o := options(t)
	o.Stop = true
	serveFlow(t, o, "claude-code", payPolicy("claude-code"))
	host := Hosts["claude-code"]
	Handle(host, fetch, o)
	if out := Handle(host, claude("PreToolUse", "tool_name", "Agent", "tool_input", ask, "tool_use_id", "toolu_agent"), o); len(out) != 0 {
		t.Fatalf("told the host %q starting a subagent no rule names", out)
	}
	out := string(Handle(host, as(paying, "agent-r", "researcher"), o))
	want := "Wake's flow policy (policy:no-pay-after-web) holds claude-code/researcher back after agent:claude-code, tool:fetch_page, so this tool call wasn't run."
	if got := reasonOf(t, out); got != want {
		t.Errorf("told the subagent %q, want %q: a rule for the agent holds its subagents", got, want)
	}
	s, p := denied(t, o)
	if s.get("wake.agent.instance") != "claude-code/researcher#agent-r" || !reflect.DeepEqual(p["origins"], []any{"agent:claude-code", "tool:fetch_page"}) {
		t.Errorf("recorded %s's refusal with origins %v", s.get("wake.agent.instance"), p["origins"])
	}
}

func TestStartingASubagentARuleKeepsFromTheWebIsRefused(t *testing.T) {
	o := options(t)
	o.Stop = true
	serveFlow(t, o, "claude-code", payPolicy("claude-code"))
	host := Hosts["claude-code"]
	Handle(host, fetch, o)
	audit := map[string]any{"description": "Audit it", "prompt": "Audit the invoice", "subagent_type": "auditor"}
	out := string(Handle(host, claude("PreToolUse", "tool_name", "Agent", "tool_input", audit, "tool_use_id", "toolu_audit"), o))
	want := "Wake's flow policy (policy:auditor-hears-no-web) holds claude-code back after tool:fetch_page, so this tool call wasn't run."
	if got := reasonOf(t, out); got != want {
		t.Errorf("told the agent %q, want %q", got, want)
	}
	if s, p := denied(t, o); s.Name != "denied Agent" || !reflect.DeepEqual(p["origins"], []any{"tool:fetch_page"}) {
		t.Errorf("recorded %s with origins %v", s.Name, p["origins"])
	}
	if _, err := os.Stat(o.flowStore("claude-code", "s1", 600).agentDir("claude-code/auditor")); !os.IsNotExist(err) {
		t.Error("the auditor that never started was given what entered the agent")
	}
}

func TestASubagentInTheBackgroundAnswersWhenItStops(t *testing.T) {
	o := options(t)
	o.Stop = true
	serveFlow(t, o, "claude-code", payPolicy("claude-code"))
	host := Hosts["claude-code"]
	launched := map[string]any{"status": "async_launched", "agentId": "agent-bg", "description": "Check the invoice", "outputFile": "/tmp/agent-bg.output"}
	Handle(host, claude("PreToolUse", "tool_name", "Agent", "tool_input", ask, "tool_use_id", "toolu_bg"), o)
	Handle(host, claude("PostToolUse", "tool_name", "Agent", "tool_input", ask, "tool_use_id", "toolu_bg", "tool_response", launched), o)
	Handle(host, as(fetch, "agent-bg", "researcher"), o)
	if out := Handle(host, paying, o); len(out) != 0 {
		t.Fatalf("told the host %q: nothing the subagent found has reached the agent while it works on", out)
	}
	stopped := claude("SubagentStop", "agent_id", "agent-bg", "agent_type", "researcher", "stop_hook_active", false)
	if out := Handle(host, stopped, o); len(out) != 0 {
		t.Errorf("told the host %q when a subagent stopped", out)
	}
	want := "Wake's flow policy (policy:no-pay-after-web) holds claude-code back after agent:claude-code/researcher, tool:fetch_page, so this tool call wasn't run."
	if got := reasonOf(t, string(Handle(host, paying, o))); got != want {
		t.Errorf("told the agent %q, want %q", got, want)
	}
	if names, _ := readNames(filepath.Join(o.Dir, unknownDir)); len(names) != 0 {
		t.Errorf("counted %v as events it doesn't handle", names)
	}
	for _, s := range spooled(t, o) {
		if s.get("wake.event.kind") != "tool_call" && s.get("wake.event.kind") != "denied" {
			t.Errorf("recorded a %s", s.get("wake.event.kind"))
		}
	}
}

// Codex's subagents use their session's id, and start with a tool whose input its docs
// don't give, so a session and its subagents are one agent to the flow policy: what enters
// one holds back the other, at once.
func TestCodexHoldsASessionAndItsSubagentsAsOne(t *testing.T) {
	o := options(t)
	o.Stop = true
	serveFlow(t, o, "codex", payPolicy("codex"))
	host := Hosts["codex"]
	Handle(host, as(fetch, "agent-w", "worker"), o)
	want := "Wake's flow policy (policy:no-pay-after-web) holds codex back after tool:fetch_page, so this tool call wasn't run."
	for _, call := range [][]byte{paying, as(paying, "agent-w", "worker")} {
		if got := reasonOf(t, string(Handle(host, call, o))); got != want {
			t.Errorf("told the agent %q, want %q", got, want)
		}
	}
	if out := Handle(host, claude("SubagentStop", "agent_id", "agent-w", "agent_type", "worker"), o); len(out) != 0 {
		t.Errorf("told the host %q when a subagent stopped", out)
	}
	if names, _ := readNames(filepath.Join(o.Dir, unknownDir)); len(names) != 0 {
		t.Errorf("counted %v as events it doesn't handle", names)
	}
}

// Cursor's subagents run in a conversation of their own, which nothing ties to the one
// that started them, so only the start is checked.
func TestCursorChecksASubagentsStart(t *testing.T) {
	o := options(t)
	o.Stop = true
	serveFlow(t, o, "cursor", `{"rules": [{"id": "explore-hears-no-web", "receivers": ["cursor/explore"], "forbid_origins": ["tool:fetch_page"]}]}`)
	host := Hosts["cursor"]
	if out := string(handle(t, o, "cursor", "subagent_start")); out != "{}" {
		t.Fatalf("told the host %q before anything entered", out)
	}
	Handle(host, []byte(`{"hook_event_name":"postToolUse","conversation_id":"conv-8f2a","tool_name":"MCP:fetch_page","tool_input":{},"tool_output":"{}"}`), o)
	out := handle(t, o, "cursor", "subagent_start")
	want := "Wake's flow policy (policy:explore-hears-no-web) holds cursor back after tool:fetch_page, so this subagent wasn't started."
	if !strings.Contains(out, `"permission":"deny"`) || !strings.Contains(out, want) {
		t.Errorf("told the host %q, want a refusal saying %q", out, want)
	}
	if s, _ := denied(t, o); s.Name != "denied subagent" {
		t.Errorf("recorded %s", s.Name)
	}
}

func TestPastItsBoundAnAgentHasEveryOrigin(t *testing.T) {
	o := options(t)
	o.Stop = true
	serveFlow(t, o, "claude-code", payPolicy("claude-code"))
	s := o.flowStore("claude-code", "s1", 600)
	for i := range maxOrigins + 1 {
		at := o.Now().UnixNano()
		s.note("claude-code", fmt.Sprintf("tool:t%d", i), at, at)
	}
	want := "Wake's flow policy (policy:no-pay-after-web) holds claude-code back, because more origins entered it than Wake keeps, so this tool call wasn't run."
	if got := reasonOf(t, string(Handle(Hosts["claude-code"], paying, o))); got != want {
		t.Errorf("told the agent %q, want %q", got, want)
	}
	if _, p := denied(t, o); !reflect.DeepEqual(p["origins"], []any{"*"}) || p["every_origin"] != saturated {
		t.Errorf("refusal payload = %v", p)
	}
	if out := Handle(Hosts["claude-code"], claude("PreToolUse", "tool_name", "Read", "tool_input", map[string]any{}), o); len(out) != 0 {
		t.Errorf("told the host %q for a call no rule applies to", out)
	}
}

// With no session, nothing is known of what entered the agent, so it has every origin.
func TestAnEventWithNoSessionHasEveryOrigin(t *testing.T) {
	o := options(t)
	o.Stop = true
	serveFlow(t, o, "claude-code", payPolicy("claude-code"))
	var e map[string]any
	json.Unmarshal(paying, &e)
	delete(e, "session_id")
	in, _ := json.Marshal(e)
	want := "Wake's flow policy (policy:no-pay-after-web) holds claude-code back, because the hook event names no session, so this tool call wasn't run."
	if got := reasonOf(t, string(Handle(Hosts["claude-code"], in, o))); got != want {
		t.Errorf("told the agent %q, want %q", got, want)
	}
}

func TestAPolicyThatCantBeUsedRefusesEveryCall(t *testing.T) {
	for name, policy := range map[string]string{
		"unreadable":        `{"unreadable": true}`,
		"invalid":           `{"rules": [{"id": "x", "tools": ["pay"]}]}`,
		"a field twice":     `{"rules": [], "rules": [{"id": "x", "forbid_origins": ["*"]}]}`,
		"a broken store":    payPolicy("claude-code"),
		"no session either": `{"unreadable": true}`,
	} {
		t.Run(name, func(t *testing.T) {
			o := options(t)
			o.Stop = true
			serveFlow(t, o, "claude-code", policy)
			call := claude("PreToolUse", "tool_name", "Read", "tool_input", map[string]any{})
			switch name {
			case "a broken store":
				// What entered the agent can't be read, so the check fails.
				path := filepath.Join(o.flowStore("claude-code", "s1", 600).agentDir("claude-code"), name)
				if err := write(path, []byte("{"), o.hex); err != nil {
					t.Fatal(err)
				}
				call = paying
			case "no session either":
				call = []byte(`{"hook_event_name":"PreToolUse","tool_name":"Read","tool_input":{}}`)
			}
			want := "Wake's flow policy can't be read, so this tool call wasn't run."
			if got := reasonOf(t, string(Handle(Hosts["claude-code"], call, o))); got != want {
				t.Errorf("told the agent %q, want %q", got, want)
			}
			s, p := denied(t, o)
			if s.get("wake.brake.halt_id") != UnreadablePolicy || !reflect.DeepEqual(p["origins"], []any{}) {
				t.Errorf("recorded the refusal under %q with origins %v", s.get("wake.brake.halt_id"), p["origins"])
			}
		})
	}
	o := options(t)
	o.Stop = true
	serveFlow(t, o, "cursor", `{"unreadable": true}`)
	if out := handle(t, o, "cursor", "subagent_start"); !strings.Contains(out, "Wake's flow policy can't be read, so this subagent wasn't started.") {
		t.Errorf("told cursor %q", out)
	}
}

// The halts come first, and a prompt is never checked against the flow policy: it isn't a
// call, and the user's prompt isn't an origin.
func TestWhatTheFlowPolicyDoesntCheck(t *testing.T) {
	everything := `{"rules": [{"id": "nothing-after-web", "forbid_origins": ["tool:fetch_page"]}]}`
	o := options(t)
	o.Stop = true
	serveFlow(t, o, "claude-code", everything)
	host := Hosts["claude-code"]
	Handle(host, fetch, o)
	if out := Handle(host, claude("UserPromptSubmit", "prompt", "Pay it"), o); len(out) != 0 {
		t.Errorf("told the host %q for a prompt", out)
	}
	writeRules(t, o, "claude-code", 0, `{"halts":[{"id":"h1","agents":["claude-code"]}],"flow":`+everything+`}`)
	if out := string(Handle(host, paying, o)); !strings.Contains(out, refusal("h1")) {
		t.Errorf("told the host %q, want the halt's refusal first", out)
	}
	// Without --stop, nothing is refused, but what enters is still noted.
	o.Stop = false
	serveFlow(t, o, "claude-code", everything)
	Handle(host, claude("PostToolUse", "tool_name", "WebFetch", "tool_input", map[string]any{}, "tool_response", page), o)
	if out := Handle(host, paying, o); len(out) != 0 {
		t.Errorf("told the host %q without --stop", out)
	}
	got, err := o.flowStore("claude-code", "s1", defaultWindow).seen("claude-code", o.Now().UnixNano())
	if err != nil || !reflect.DeepEqual(got.origins, []string{"tool:WebFetch", "tool:fetch_page"}) {
		t.Errorf("noted %v, %v", got, err)
	}
}

// Every hook notes what entered its agent, with no rules to read, and no --stop.
func TestNotesWhatEntersWithNoRules(t *testing.T) {
	o := options(t)
	Handle(Hosts["claude-code"], fetch, o)
	Handle(Hosts["claude-code"], claude("PostToolUseFailure", "tool_name", "Bash", "tool_input", map[string]any{}, "error", "exit 1"), o)
	handle(t, o, "cursor", "read_file")
	got, _ := o.flowStore("claude-code", "s1", defaultWindow).seen("claude-code", o.Now().UnixNano())
	if !reflect.DeepEqual(got.origins, []string{"tool:Bash", "tool:fetch_page"}) {
		t.Errorf("noted %v for claude-code: a call that failed enters too", got)
	}
	got, _ = o.flowStore("cursor", "conv-8f2a", defaultWindow).seen("cursor", o.Now().UnixNano())
	if !reflect.DeepEqual(got.origins, []string{"tool:read_file"}) {
		t.Errorf("noted %v for cursor's read", got)
	}
}

// The store, as the SDKs' tests check what entered each agent.

const sec = int64(time.Second)

func store(t *testing.T, window int64) flowStore {
	return flowStore{dir: t.TempDir(), window: window * sec}
}

func origins(t *testing.T, s flowStore, agent string, now int64) seen {
	t.Helper()
	got, err := s.seen(agent, now)
	if err != nil {
		t.Fatal(err)
	}
	return got
}

func TestAnOriginCountsForAWindowFromWhenItLastEntered(t *testing.T) {
	s := store(t, defaultWindow)
	start := 1_000_000 * sec
	s.note("billing", "tool:fetch_page", start, start)
	s.note("billing", "agent:research", start+10*sec, start+10*sec)
	if got := origins(t, s, "billing", start+20*sec); !reflect.DeepEqual(got.origins, []string{"agent:research", "tool:fetch_page"}) {
		t.Errorf("seen %v, want newest first", got)
	}
	// Entering again, it counts from then; entering as of earlier changes nothing.
	s.note("billing", "tool:fetch_page", start+800*sec, start+800*sec)
	s.note("billing", "tool:fetch_page", start+5*sec, start+800*sec)
	if got := origins(t, s, "billing", start+905*sec); !reflect.DeepEqual(got.origins, []string{"tool:fetch_page", "agent:research"}) {
		t.Errorf("seen %v", got)
	}
	if got := origins(t, s, "billing", start+1000*sec); !reflect.DeepEqual(got.origins, []string{"tool:fetch_page"}) {
		t.Errorf("seen %v, want agent:research past the window", got)
	}
	if got := origins(t, s, "billing", start+1800*sec); len(got.origins) != 0 || got.every != "" {
		t.Errorf("seen %v past the window", got)
	}
	if got := origins(t, s, "support", start); len(got.origins) != 0 {
		t.Errorf("seen %v for an agent nothing entered", got)
	}
}

func TestOriginsThatEnteredAtOnceAreOrderedByName(t *testing.T) {
	s := store(t, defaultWindow)
	start := 1_000_000 * sec
	s.note("a", "tool:b", start, start)
	s.note("a", "tool:a", start, start)
	s.note("a", "tool:c", start+sec, start+sec)
	if got := origins(t, s, "a", start+sec); !reflect.DeepEqual(got.origins, []string{"tool:c", "tool:a", "tool:b"}) {
		t.Errorf("seen %v", got)
	}
}

func TestPastItsBoundAnAgentHasEveryOriginForAWindow(t *testing.T) {
	s := store(t, defaultWindow)
	start := 1_000_000 * sec
	for i := range maxOrigins {
		s.note("a", fmt.Sprintf("tool:t%d", i), start, start)
	}
	if got := origins(t, s, "a", start); len(got.origins) != maxOrigins {
		t.Fatalf("kept %d origins, want the bound itself kept", len(got.origins))
	}
	s.note("a", "tool:one-too-many", start, start)
	if got := origins(t, s, "a", start+sec); got.every != saturated || got.origins != nil {
		t.Errorf("seen %v, want every origin", got)
	}
	// Once the window has passed with nothing new, it's known again, and origins older
	// than the window don't count toward the bound.
	later := start + (defaultWindow+1)*sec
	if got := origins(t, s, "a", later); got.every != "" || len(got.origins) != 0 {
		t.Errorf("seen %v once the window passed", got)
	}
	for i := range maxOrigins {
		s.note("a", fmt.Sprintf("tool:later%d", i), later, later)
	}
	if got := origins(t, s, "a", later); len(got.origins) != maxOrigins {
		t.Errorf("kept %d origins, want %d", len(got.origins), maxOrigins)
	}
	if names, _ := readNames(s.agentDir("a")); len(names) > maxOrigins+1 {
		t.Errorf("kept %d files for the agent: the old ones should have made room", len(names))
	}
	// Past the window, they make room for the next to enter.
	next := later + (defaultWindow+1)*sec
	s.note("a", "tool:next", next, next)
	if names, _ := readNames(s.agentDir("a")); len(names) != 2 {
		t.Errorf("kept %v: want the new origin and the note of every origin", names)
	}
}

// Two hooks noting at once can each take the last room there is. An agent found with
// more origins than are kept counts as having every origin all the same.
func TestPastItsBoundByARaceAnAgentHasEveryOrigin(t *testing.T) {
	s := store(t, defaultWindow)
	start := 1_000_000 * sec
	for i := range maxOrigins + 1 {
		origin := fmt.Sprintf("tool:t%d", i)
		if err := s.put(filepath.Join(s.agentDir("a"), name(origin)), originFile{Origin: origin, At: start}); err != nil {
			t.Fatal(err)
		}
	}
	if got := origins(t, s, "a", start); got.every != saturated {
		t.Errorf("seen %d origins, want every origin", len(got.origins))
	}
	// Every origin counts from the latest it was given, and an earlier note of it, carried
	// in late, doesn't cut that short.
	s.saturate("b", start+600*sec, saturated)
	s.saturate("b", start, carried)
	if got := origins(t, s, "b", start+1000*sec); got.every != saturated {
		t.Errorf("seen %v, want every origin since the later note", got)
	}
}

func TestAMessageCarriesWhatEnteredItsSenderAsOfWhenItDid(t *testing.T) {
	s := store(t, defaultWindow)
	start := 1_000_000 * sec
	s.note("research", "tool:fetch_page", start, start)
	s.note("research", "agent:billing", start+5*sec, start+5*sec)
	// Research answers billing: the page comes with it, as of when research fetched it,
	// and billing's own message doesn't come back to it.
	s.message("research", "billing", start+100*sec)
	if got := origins(t, s, "billing", start+100*sec); !reflect.DeepEqual(got.origins, []string{"agent:research", "tool:fetch_page"}) {
		t.Errorf("seen %v", got)
	}
	// Passing it on never makes it newer: it leaves billing when it leaves research.
	s.carry("billing", "support", start+200*sec)
	for _, agent := range []string{"research", "billing", "support"} {
		if got := origins(t, s, agent, start+901*sec); contains(got.origins, "tool:fetch_page") {
			t.Errorf("%s still has the page: %v", agent, got)
		}
	}
	// Nothing to carry keeps no agent, and an agent's own message isn't an origin.
	s.carry("idle", "listener", start)
	if _, err := os.Stat(s.agentDir("listener")); !os.IsNotExist(err) {
		t.Error("kept an agent nothing reached")
	}
	s.message("billing", "billing", start)
	if got := origins(t, s, "billing", start+100*sec); contains(got.origins, "agent:billing") {
		t.Errorf("billing's own message entered it: %v", got)
	}
}

func TestAMessageFromAnAgentWithEveryOriginGivesEveryOriginForAsLong(t *testing.T) {
	s := store(t, defaultWindow)
	start := 1_000_000 * sec
	for i := range maxOrigins + 1 {
		s.note("research", fmt.Sprintf("tool:t%d", i), start, start)
	}
	s.carry("research", "billing", start+500*sec)
	s.carry("billing", "support", start+600*sec)
	for _, agent := range []string{"billing", "support"} {
		if got := origins(t, s, agent, start+600*sec); got.every != carried {
			t.Errorf("%s: seen %v, want every origin, carried", agent, got)
		}
	}
	for _, agent := range []string{"research", "billing", "support"} {
		if got := origins(t, s, agent, start+(defaultWindow+1)*sec); got.every != "" || len(got.origins) != 0 {
			t.Errorf("%s: seen %v, not kept alive past research's window", agent, got)
		}
	}
}

func TestRemembersASubagentInTheBackgroundUntilItStops(t *testing.T) {
	s := store(t, defaultWindow)
	s.remember("agent-bg", "claude-code/researcher", "claude-code")
	if from, to, ok := s.answered("agent-bg"); !ok || from != "claude-code/researcher" || to != "claude-code" {
		t.Errorf("answered %q -> %q, %v", from, to, ok)
	}
	if _, _, ok := s.answered("agent-bg"); ok {
		t.Error("answered twice")
	}
	if _, _, ok := s.answered("never-launched"); ok {
		t.Error("answered for a subagent never left in the background")
	}
}

// The shared vectors: every decision wake-proxy and both SDKs make, the hook makes alike.

type vectors struct {
	Policies map[string]json.RawMessage `json:"policies"`
	Cases    []struct {
		Name     string          `json:"name"`
		Policy   string          `json:"policy"`
		Caller   string          `json:"caller"`
		Lineage  []string        `json:"lineage"`
		Tool     *string         `json:"tool"`
		Receiver *string         `json:"receiver"`
		Store    *string         `json:"store"`
		Origins  json.RawMessage `json:"origins"`
		DeniedBy *string         `json:"denied_by"`
		Kept     []string        `json:"kept"`
	} `json:"cases"`
	Invalid []struct {
		Name   string          `json:"name"`
		Policy json.RawMessage `json:"policy"`
	} `json:"invalid"`
}

func readVectors(t *testing.T) vectors {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("..", "sdk", "testdata", "flow_policy.json"))
	if err != nil {
		t.Fatal(err)
	}
	var v vectors
	if err := json.Unmarshal(data, &v); err != nil {
		t.Fatal(err)
	}
	return v
}

func TestDecidesAsTheSharedVectorsSay(t *testing.T) {
	v := readVectors(t)
	policies := map[string]*flowPolicy{}
	stores := map[string]bool{} // the halt of each rule naming stores
	for name, raw := range v.Policies {
		p, err := parsePolicy(raw)
		if err != nil {
			t.Fatalf("policy %s: %v", name, err)
		}
		policies[name] = p
		for _, r := range p.rules {
			if len(r.stores) > 0 {
				stores["policy:"+r.id] = true
			}
		}
	}
	decided := 0
	for _, c := range v.Cases {
		p := policies[c.Policy]
		if p == nil {
			t.Fatalf("%s: no policy %q", c.Name, c.Policy)
		}
		s := seen{every: "every origin"}
		if string(c.Origins) != `"*"` {
			s = seen{}
			if err := json.Unmarshal(c.Origins, &s.origins); err != nil {
				t.Fatalf("%s: %v", c.Name, err)
			}
		}
		// No rule naming stores holds a call the hook sees, a tool called or an agent
		// reached by a store's name included: a memory write passes no hook.
		names := []string{"write_memory"}
		for _, value := range []*string{c.Tool, c.Receiver, c.Store} {
			if value != nil {
				names = append(names, *value)
			}
		}
		for _, r := range p.rules {
			for _, pattern := range r.stores {
				if n := strings.TrimRight(pattern, "*"); n != "" {
					names = append(names, n)
				}
			}
		}
		for _, n := range names {
			for _, call := range []call{{tool: n}, {reaches: true, receiver: n}} {
				for _, s := range []seen{s, {every: "every origin"}} {
					if d := p.deciding(c.Caller, c.Lineage, s, call); d != nil && stores[d.halt] {
						t.Errorf("%s: %s held %+v", c.Name, d.halt, call)
					}
				}
			}
		}
		if c.Store != nil {
			continue
		}
		var d *denial
		switch {
		case c.Tool != nil && c.Receiver == nil:
			d = p.deciding(c.Caller, c.Lineage, s, call{tool: *c.Tool})
		case c.Receiver != nil && c.Tool == nil:
			d = p.deciding(c.Caller, c.Lineage, s, call{reaches: true, receiver: *c.Receiver})
		default:
			t.Fatalf("%s: a case is a tool call, reaches an agent, or writes to a store", c.Name)
		}
		decided++
		want, kept := "", []string{}
		if c.DeniedBy != nil {
			want = *c.DeniedBy
		}
		got, gotKept := "", []string{}
		if d != nil {
			got, gotKept = d.halt, d.origins
		}
		if got != want || !reflect.DeepEqual(gotKept, append(kept, c.Kept...)) {
			t.Errorf("%s: denied by %q keeping %v, want %q keeping %v", c.Name, got, gotKept, want, c.Kept)
		}
	}
	if decided < 20 {
		t.Errorf("decided %d cases, want the vectors' tool calls and agents reached", decided)
	}
	if facts := policies["memory"].rules[2]; !reflect.DeepEqual(facts.stores, []string{"facts", "customers/*"}) {
		t.Errorf("read the stores of %s as %v", facts.id, facts.stores)
	}
}

func TestRefusesEveryPolicyTheSharedVectorsCallInvalid(t *testing.T) {
	v := readVectors(t)
	invalid := map[string]string{}
	for _, c := range v.Invalid {
		invalid[c.Name] = string(c.Policy)
	}
	// And those the Python SDK's tests refuse beside them.
	for i, bad := range []string{
		`[]`, `"rules"`,
		`{"version": 7, "rules": []}`,
		`{"version": null, "rules": []}`,
		`{"window_seconds": true, "rules": []}`,
		`{"window_seconds": 900.0, "rules": []}`,
		`{"window_seconds": 9e2, "rules": []}`,
		`{"window_seconds": -0, "rules": []}`,
		`{"window_seconds": null, "rules": []}`,
		`{"rules": null}`,
		`{"rules": [{"forbid_origins": ["*"]}]}`,
		`{"rules": [{"id": 5, "forbid_origins": ["*"]}]}`,
		`{"rules": [{"id": "x\n", "forbid_origins": ["*"]}]}`,
		`{"rules": [{"id": "café", "forbid_origins": ["*"]}]}`,
		`{"rules": [{"id": "x", "forbid_origins": null}]}`,
		`{"rules": [{"id": "x", "forbid_origins": [5]}]}`,
		`{"rules": [{"id": "x", "forbid_origins": ["tool:**"]}]}`,
		`{"rules": [{"id": "x", "tools": ["*pay"], "forbid_origins": ["*"]}]}`,
		`{"rules": [{"id": "x", "receivers": ["bill*"], "forbid_origins": ["*"]}]}`,
		`{"rules": [{"id": "x", "path_prefixes": ["/a2a*"], "forbid_origins": ["*"]}]}`,
		`{"rules": [{"id": "x", "receivers": [""], "forbid_origins": ["*"]}]}`,
		`{"rules": [{"id": "x", "stores": ["*facts"], "forbid_origins": ["*"]}]}`,
		`{"rules": [{"id": "x", "stores": "facts", "forbid_origins": ["*"]}]}`,
		`{"rules": [{"id": "x", "stores": [7], "forbid_origins": ["*"]}]}`,
		`{"rules": [{"id": "x", "stores": ["facts"]}]}`,
		`{"unreadable": false}`,
		`{"rules": [{"id": "x", "forbid_origins": ["*"], "id": "y"}]}`,
		`{"window_seconds": 60, "window_seconds": 600, "rules": []}`,
	} {
		invalid[fmt.Sprintf("python's %d: %s", i, bad)] = bad
	}
	for name, raw := range invalid {
		if _, err := parsePolicy([]byte(raw)); err == nil {
			t.Errorf("%s: taken", name)
		}
		p := servedPolicy(json.RawMessage(raw))
		if p == nil || p.unreadable == "" {
			t.Errorf("%s: served as usable", name)
			continue
		}
		for _, call := range []call{{tool: "pay"}, {reaches: true, receiver: "billing"}} {
			if d := p.deciding("billing", nil, seen{}, call); d == nil || d.halt != UnreadablePolicy {
				t.Errorf("%s: decided %+v for %+v", name, d, call)
			}
		}
	}
}

func TestReadsThePolicyAsWakeProxyServesIt(t *testing.T) {
	if servedPolicy(nil) != nil || servedPolicy(json.RawMessage("null")) != nil {
		t.Error("a wake-proxy that enforces no policy has one")
	}
	if p := servedPolicy(json.RawMessage(`{"unreadable": true}`)); p == nil || p.unreadable != "wake-proxy can't use its policy file" {
		t.Errorf("read %+v", p)
	}
	p := servedPolicy(json.RawMessage(`{"version": "` + strings.Repeat("ab", 32) + `", "window_seconds": 600, "rules": [
		{"id": "no-pay-after-web", "callers": ["billing"], "tools": ["pay", "wire_*"], "forbid_origins": ["tool:fetch_page", "agent:*"]}]}`))
	if p == nil || p.unreadable != "" || p.window != 600 || len(p.rules) != 1 ||
		!reflect.DeepEqual(p.rules[0].tools, []string{"pay", "wire_*"}) || p.rules[0].receivers != nil {
		t.Errorf("read %+v", p)
	}
	if p := servedPolicy(json.RawMessage(`{}`)); p == nil || p.unreadable != "" || p.window != defaultWindow || len(p.rules) != 0 {
		t.Errorf("read %+v from an empty policy", p)
	}
}

func TestABareStarMatchesEveryOriginAndAPrefixItsRest(t *testing.T) {
	p, err := parsePolicy([]byte(`{"rules": [{"id": "r", "tools": ["refund_*"], "forbid_origins": ["*"]}]}`))
	if err != nil {
		t.Fatal(err)
	}
	s := seen{origins: []string{"user:anything"}}
	if d := p.deciding("a", nil, s, call{tool: "refund_eur"}); d == nil || !reflect.DeepEqual(d.origins, []string{"user:anything"}) {
		t.Errorf("decided %+v", d)
	}
	for _, tool := range []string{"refund", "refunds"} {
		if d := p.deciding("a", nil, s, call{tool: tool}); d != nil {
			t.Errorf("%s: decided %+v, but a prefix needs what comes before the *", tool, d)
		}
	}
}
