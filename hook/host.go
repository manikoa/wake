package hook

import (
	"encoding/json"
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
}

// Hosts are the products wake-hook reads, by the name given on its command line.
var Hosts = map[string]*Host{
	"claude-code": {Name: "claude-code", parse: parseClaude, refuse: refuseClaude},
	// Codex took Claude Code's hooks: the same events, fields and answers.
	"codex":       {Name: "codex", parse: parseClaude, refuse: refuseClaude},
	"cursor":      {Name: "cursor", parse: parseCursor, refuse: refuseCursor, allow: allowCursor},
	"grok":        {Name: "grok", parse: parseGrok, refuse: refuseDecision},
	"gemini":      {Name: "gemini", parse: parseGemini, refuse: refuseDecision},
	"antigravity": {Name: "antigravity", parse: parseAntigravity, refuse: refuseDecision, allow: allowAntigravity},
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
	case "PostToolUse":
		a.Kind = "tool_call"
		a.toolCall(f)
		a.Output = f.take("tool_response")
		a.Duration = f.millis("duration_ms")
	case "PostToolUseFailure":
		a.Kind = "tool_call"
		a.toolCall(f)
		a.Output = map[string]any{"error": f.take("error")}
		a.Duration = f.millis("duration_ms")
	case "UserPromptSubmit":
		a.Kind, a.Gate, a.Prompt = "agent_message", true, true
		a.Input = f.take("prompt")
	default:
		return a, false
	}
	return a, true
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

func parseCursor(f fields, _ string) (Action, bool) {
	a := Action{Event: f.str("hook_event_name"), Session: f.str("conversation_id")}
	switch a.Event {
	case "preToolUse":
		a.Gate = true
		a.toolCall(f)
	case "postToolUse":
		a.Kind = "tool_call"
		a.toolCall(f)
		a.Output = decoded(f.take("tool_output"))
		a.Duration = f.millis("duration")
	case "postToolUseFailure":
		// Its docs give no fields for the failure, so they stay in the payload's hook.
		a.Kind = "tool_call"
		a.toolCall(f)
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
		a.Gate, a.Tool = true, "subagent"
		a.Input = map[string]any{"subagent_type": f.take("subagent_type"), "task": f.take("task")}
	default:
		return a, false
	}
	return a, true
}

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
