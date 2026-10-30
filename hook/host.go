package hook

import (
	"encoding/json"
	"strings"
	"time"
)

// An Action is one hook event, in the terms every host shares.
type Action struct {
	Event        string // the host's name for the event
	Session      string // the host's id for the session
	Subagent     string // the host's id for the subagent the event came from, when it names one
	SubagentType string
	// Kind is what's recorded when the action isn't refused: tool_call or agent_message,
	// or nothing for an event that only asks whether to go ahead.
	Kind     string
	Gate     bool // the host lets the hook refuse it
	Prompt   bool // a prompt being submitted, which hosts refuse their own way
	Tool     string
	CallID   string
	Input    any
	Output   any
	Duration time.Duration
	// Rest holds the event's fields that none of the above took, kept in the payload so
	// nothing the host said is lost.
	Rest map[string]any

	flow    flowAct  // what it means for the flow policy, beyond the tool it calls (flow.go)
	lineage []string // what started the subagent, outermost first, when wake-hook placed it
}

// flowAct is what an action means for the flow policy, beyond the tool it calls: a
// subagent it starts, a subagent's answer, or a subagent that stopped.
type flowAct struct {
	starts     bool   // it starts a subagent of type subagent, or its result is that subagent's answer
	subagent   string // the type of subagent it starts
	onlyStarts bool   // it starts one and calls no tool, as Cursor's subagentStart
	background bool   // the subagent runs on in the background, and answers when it stops
	agentID    string // the id the background subagent's hooks name it by
	stopped    bool   // a subagent stopped: if it ran in the background, its answer arrives

	// Cursor runs each subagent in a conversation of its own, which the Task call that
	// started it ties to the agent that did (place, in flow.go).
	call       string // the Task call starting the subagent, as subagentStart names it
	answers    bool   // its result is the answer of the subagent its Task call started
	parentCall string // the Task call that started the subagent making the call
	child      string // the conversation of the subagent that stopped
	spawner    string // the conversation of the agent that started the subagent
	unplaced   bool   // a subagent makes the call, but wake-hook didn't see it start
}

// A Host reads one product's hook events and answers them its way.
type Host struct {
	Name string
	// parse reads an event. event is the one named on the command line, for a host whose
	// input doesn't say.
	parse  func(f fields, event string) (Action, bool)
	refuse func(a Action, reason string) []byte
	// allow is what a host needs to hear when a gate lets an action through, if anything.
	allow func(a Action) []byte
	// ownName is a tool's own name, given the host's name for it, for the flow policy: an
	// MCP tool's, as wake-proxy names it. The host's name for it when nil.
	ownName func(tool string) string
	// subagents says the host names the subagent making each call, as Claude Code does, or
	// lets wake-hook place it, as Cursor does (conversations), and says when one starts and
	// when it answers. The flow policy then holds each type of subagent as an agent of its
	// own. Elsewhere, a session is one agent to the flow policy, its subagents included.
	subagents bool
	// conversations says the host runs each subagent in a conversation of its own, and
	// names the call that started it on the subagent's own tool hooks, as Cursor does. Its
	// events are placed in the session of the agent that started it (place, in flow.go).
	conversations bool
}

// toolName is a tool's name as a flow rule names it, and as its result enters an agent.
func (h *Host) toolName(tool string) string {
	if h.ownName == nil {
		return tool
	}
	return h.ownName(tool)
}

// Hosts are the products wake-hook reads, by the name given on its command line.
var Hosts = map[string]*Host{
	"claude-code": {Name: "claude-code", parse: parseClaude, refuse: refuseClaude, ownName: mcpToolName, subagents: true},
	// Codex took Claude Code's hooks: the same events, fields and answers, but for its subagents.
	"codex":       {Name: "codex", parse: parseCodex, refuse: refuseClaude, ownName: mcpToolName},
	"cursor":      {Name: "cursor", parse: parseCursor, refuse: refuseCursor, allow: allowCursor, ownName: cursorToolName, subagents: true, conversations: true},
	"grok":        {Name: "grok", parse: parseGrok, refuse: refuseDecision},
	"gemini":      {Name: "gemini", parse: parseGemini, refuse: refuseDecision},
	"antigravity": {Name: "antigravity", parse: parseAntigravity, refuse: refuseDecision, allow: allowAntigravity},
}

// mcpToolName is an MCP tool's own name, which Claude Code and Codex call
// mcp__<server>__<tool>.
func mcpToolName(name string) string {
	if parts := strings.SplitN(name, "__", 3); len(parts) == 3 && parts[0] == "mcp" {
		return parts[2]
	}
	return name
}

// cursorToolName is an MCP tool's own name, which Cursor's tool hooks call MCP:<tool>.
// Its MCP hooks give the tool's own name already.
func cursorToolName(name string) string {
	return strings.TrimPrefix(name, "MCP:")
}

// subagentTools are the tools Claude Code starts a subagent with; older versions call it Task.
var subagentTools = map[string]bool{"Agent": true, "Task": true}

// launched is what the Agent tool's result says of a subagent it left running in the background.
var launched = map[string]bool{"async_launched": true, "remote_launched": true}

// subagentCall reads a call to Claude Code's Agent tool: the type of subagent it starts,
// and, from its result, whether it runs on in the background, and the id its hooks name
// it by.
func (a *Action) subagentCall(result any) {
	if !subagentTools[a.Tool] {
		return
	}
	a.flow.starts, a.flow.subagent = true, "general-purpose"
	if input, ok := a.Input.(map[string]any); ok {
		if t, ok := input["subagent_type"].(string); ok && t != "" {
			a.flow.subagent = t
		}
	}
	if r, ok := result.(map[string]any); ok {
		status, _ := r["status"].(string)
		a.flow.background = launched[status]
		a.flow.agentID, _ = r["agentId"].(string)
	}
}

// fields hands out an event's fields one at a time, so whatever's left can be kept.
type fields map[string]any

func (f fields) take(key string) any {
	v := f[key]
	delete(f, key)
	return v
}

func (f fields) str(key string) string {
	s, _ := f.take(key).(string)
	return s
}

// millis reads a duration in milliseconds.
func (f fields) millis(key string) time.Duration {
	ms, _ := f.take(key).(float64)
	return time.Duration(ms * float64(time.Millisecond))
}

// decoded reads a value some hosts send as a JSON string, keeping it as it is when it
// isn't JSON.
func decoded(v any) any {
	s, ok := v.(string)
	if !ok {
		return v
	}
	var out any
	if json.Unmarshal([]byte(s), &out) != nil {
		return s
	}
	return out
}

func (a *Action) toolCall(f fields) {
	a.Tool, a.CallID, a.Input = f.str("tool_name"), f.str("tool_use_id"), decoded(f.take("tool_input"))
}

// refuseDecision is how Grok Build, Gemini CLI and Antigravity hear a refusal.
func refuseDecision(a Action, reason string) []byte {
	return encode(map[string]any{"decision": "deny", "reason": reason})
}

// Claude Code's hooks, and Codex's, which follow them.
func parseClaude(f fields, _ string) (Action, bool) {
	a := Action{Event: f.str("hook_event_name"), Session: f.str("session_id")}
	a.Subagent, a.SubagentType = f.str("agent_id"), f.str("agent_type")
	switch a.Event {
	case "PreToolUse":
		a.Gate = true
		a.toolCall(f)
		a.subagentCall(nil)
	case "PostToolUse":
		a.Kind = "tool_call"
		a.toolCall(f)
		a.Output = f.take("tool_response")
		a.Duration = f.millis("duration_ms")
		a.subagentCall(a.Output)
	case "PostToolUseFailure":
		a.Kind = "tool_call"
		a.toolCall(f)
		a.Output = map[string]any{"error": f.take("error")}
		a.Duration = f.millis("duration_ms")
		a.subagentCall(nil)
	case "UserPromptSubmit":
		a.Kind, a.Gate, a.Prompt = "agent_message", true, true
		a.Input = f.take("prompt")
	case "SubagentStop":
		// Nothing new to record, but a subagent left in the background answers now.
		a.flow.stopped = true
	default:
		return a, false
	}
	return a, true
}

// Codex's hooks. It starts a subagent with spawn_agent, whose input and result its docs
// don't give, so what a subagent starts with and answers can't be told apart from its
// session's. Its subagents' hooks name their session's id, so the flow policy holds them
// as their session's agent: what enters one enters the other.
func parseCodex(f fields, event string) (Action, bool) {
	a, ok := parseClaude(f, event)
	a.flow = flowAct{}
	return a, ok
}

func refuseClaude(a Action, reason string) []byte {
	if a.Prompt {
		return encode(map[string]any{"decision": "block", "reason": reason})
	}
	return encode(map[string]any{"hookSpecificOutput": map[string]any{
		"hookEventName":            a.Event,
		"permissionDecision":       "deny",
		"permissionDecisionReason": reason,
	}})
}

// Cursor's hooks. Each subagent runs in a conversation of its own, whose tool hooks name
// the Task call that started it, from Cursor 3.22: that ties the subagent to the agent that
// started it (place, in flow.go). The fields that do so stay in the payload's hook too.
func parseCursor(f fields, _ string) (Action, bool) {
	a := Action{Event: f.str("hook_event_name"), Session: f.str("conversation_id")}
	a.flow.parentCall, _ = f["parent_tool_call_id"].(string)
	switch a.Event {
	case "preToolUse":
		a.Gate = true
		a.toolCall(f)
	case "postToolUse":
		a.Kind = "tool_call"
		a.toolCall(f)
		a.Output = decoded(f.take("tool_output"))
		a.Duration = f.millis("duration")
		a.flow.answers = a.Tool == cursorSubagentTool
	case "postToolUseFailure":
		// Its docs give no fields for the failure, so they stay in the payload's hook.
		a.Kind = "tool_call"
		a.toolCall(f)
		a.flow.answers = a.Tool == cursorSubagentTool
	case "beforeShellExecution":
		a.Gate, a.Tool = true, "Shell"
		a.Input = map[string]any{"command": f.take("command")}
	case "afterShellExecution":
		a.Kind, a.Tool = "tool_call", "Shell"
		a.Input = map[string]any{"command": f.take("command")}
		a.Output = f.take("output")
		a.Duration = f.millis("duration")
	case "beforeMCPExecution":
		a.Gate = true
		a.Tool, a.Input = f.str("tool_name"), decoded(f.take("tool_input"))
	case "afterMCPExecution":
		a.Kind = "tool_call"
		a.Tool, a.Input = f.str("tool_name"), decoded(f.take("tool_input"))
		a.Output = decoded(f.take("result_json"))
		a.Duration = f.millis("duration")
	case "beforeReadFile":
		// Nothing follows a read, so it's recorded here, with what's about to be read.
		a.Kind, a.Gate, a.Tool = "tool_call", true, "read_file"
		a.Input = map[string]any{"file_path": f.take("file_path")}
		a.Output = f.take("content")
	case "afterFileEdit":
		a.Kind, a.Tool = "tool_call", "edit_file"
		a.Input = map[string]any{"file_path": f.take("file_path"), "edits": f.take("edits")}
	case "beforeSubmitPrompt":
		a.Kind, a.Gate, a.Prompt = "agent_message", true, true
		a.Input = f.take("prompt")
	case "subagentStart":
		// Checked as reaching the subagent. Its own hooks name the Task call starting it, by
		// which they're placed once it's let through.
		a.Gate, a.Tool = true, "subagent"
		subagent := f.take("subagent_type")
		a.Input = map[string]any{"subagent_type": subagent, "task": f.take("task")}
		a.flow.starts, a.flow.onlyStarts = true, true
		a.flow.subagent, _ = subagent.(string)
		if a.flow.call, _ = f["tool_call_id"].(string); a.flow.call == "" {
			a.flow.call, _ = f["subagent_id"].(string)
		}
		a.flow.spawner, _ = f["parent_agent_id"].(string)
	case "subagentStop":
		// Nothing new to record, but the subagent's answer reaches the agent that started it.
		a.flow.stopped = true
		a.flow.child, _ = f["child_conversation_id"].(string)
		a.flow.spawner, _ = f["parent_agent_id"].(string)
	default:
		return a, false
	}
	return a, true
}

// cursorSubagentTool is the tool a Cursor agent starts a subagent with. Its result is the
// subagent's answer.
const cursorSubagentTool = "Task"

func refuseCursor(a Action, reason string) []byte {
	if a.Prompt {
		return encode(map[string]any{"continue": false, "user_message": reason})
	}
	return encode(map[string]any{"permission": "deny", "user_message": reason, "agent_message": reason})
}

// allowCursor answers a gate that lets the action through. A permission hook gets {},
// never "allow", which might skip Cursor's own approval of the action.
func allowCursor(a Action) []byte {
	if a.Prompt {
		return []byte(`{"continue":true}`)
	}
	return []byte(`{}`)
}

// Grok Build's own hooks. Its docs name no field for a tool's result, so a result stays
// in the payload's hook, wherever Grok Build puts it.
func parseGrok(f fields, _ string) (Action, bool) {
	a := Action{Event: f.str("hookEventName"), Session: f.str("sessionId")}
	switch a.Event {
	case "PreToolUse":
		a.Gate = true
		a.Tool, a.Input = f.str("toolName"), f.take("toolInput")
	case "PostToolUse", "PostToolUseFailure":
		a.Kind = "tool_call"
		a.Tool, a.Input = f.str("toolName"), f.take("toolInput")
	case "UserPromptSubmit":
		// Only PreToolUse can block in Grok Build.
		a.Kind = "agent_message"
		a.Input = f.take("prompt")
	default:
		return a, false
	}
	return a, true
}

// Gemini CLI's hooks. Its events name no subagent, so a subagent's calls are its session's.
// A failed call is an AfterTool too, its error in the response.
func parseGemini(f fields, _ string) (Action, bool) {
	a := Action{Event: f.str("hook_event_name"), Session: f.str("session_id")}
	switch a.Event {
	case "BeforeTool":
		a.Gate = true
		a.Tool, a.Input = f.str("tool_name"), f.take("tool_input")
	case "AfterTool":
		a.Kind = "tool_call"
		a.Tool, a.Input = f.str("tool_name"), f.take("tool_input")
		a.Output = f.take("tool_response")
	case "BeforeAgent":
		a.Kind, a.Gate, a.Prompt = "agent_message", true, true
		a.Input = f.take("prompt")
	default:
		return a, false
	}
	return a, true
}

// Antigravity's hooks. Its input doesn't say which event fired, so each hook is told on
// its command line. Its docs name no field for a tool's result, only an error when a
// call failed, and its hooks see no prompt.
func parseAntigravity(f fields, event string) (Action, bool) {
	a := Action{Event: event, Session: f.str("conversationId")}
	switch event {
	case "PreToolUse":
		a.Gate = true
	case "PostToolUse":
		a.Kind = "tool_call"
		if err, _ := f.take("error").(string); err != "" {
			a.Output = map[string]any{"error": err}
		}
	case "":
		// Not told its event, it can't tell what it's answering, so where it would check
		// halts it refuses.
		a.Event, a.Gate = "no_event", true
		return a, false
	default:
		return a, false
	}
	if call, ok := f["toolCall"].(map[string]any); ok {
		a.Tool, _ = call["name"].(string)
		a.Input = call["args"]
		delete(call, "name")
		delete(call, "args")
		if len(call) == 0 {
			delete(f, "toolCall")
		}
	}
	return a, true
}

// allowAntigravity lets a call through to Antigravity's next hook or default policy, as
// its "allow" does; "auto_approve" would skip that policy.
func allowAntigravity(Action) []byte {
	return []byte(`{"decision":"allow"}`)
}
