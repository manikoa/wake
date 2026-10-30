// Package hook records what coding agents do through their hooks, and refuses what the
// brake's halts cover. The agent's host runs wake-hook on each hook event; it writes the
// event to a spool and answers at once, and wake-hook serve sends the spool to the
// collector. See docs/hooks.md.
package hook

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"time"
	"unicode/utf8"
)

const (
	// StaleRules is the halt a call is refused under while the brake's rules are missing
	// or too old, as the SDKs refuse theirs.
	StaleRules = "stale-rules"
	// HaltCheckFailed is the halt a call is refused under when its halts couldn't be read.
	HaltCheckFailed = "halt-check-failed"

	// The SDKs' limits on a payload: one larger is sent as its size, digest and a prefix.
	maxPayload = 4 << 20
	prefixLen  = 2 << 20
)

// Options configure the hook for one event.
type Options struct {
	Agent string // the agent's name, as the brake halts it; the host's name when empty
	Dir   string // where events wait for serve, and where serve leaves the brake's rules
	Event string // the hook event, for a host whose input doesn't name it (Antigravity)
	// Stop refuses what the brake's halts cover, and every gated call while the rules
	// serve leaves are missing, stale or unreadable.
	Stop bool
	TTL  time.Duration // how old the rules may be; 30 seconds when zero, as in the SDKs
	Now  func() time.Time
	Rand io.Reader
}

func (o *Options) defaults(host *Host) {
	if o.Agent == "" {
		o.Agent = host.Name
	}
	if o.TTL == 0 {
		o.TTL = 30 * time.Second
	}
	if o.Now == nil {
		o.Now = time.Now
	}
	if o.Rand == nil {
		o.Rand = rand.Reader
	}
}

// Handle answers one hook event from host, read from in, and returns what to print. The
// exit code is always 0: hosts fail open on a hook that errors, so a refusal is always
// said in the host's own words, and nothing here is allowed to fail any other way.
func Handle(host *Host, in []byte, o Options) (out []byte) {
	o.defaults(host)
	var a Action
	defer func() {
		if recover() != nil {
			// Whatever broke, a gate that should check halts refuses.
			out = nil
			if a.Gate && o.Stop {
				out = host.refuse(a, refusal(HaltCheckFailed))
			}
		}
	}()
	var f fields
	if json.Unmarshal(in, &f) != nil || f == nil {
		o.mark(unknownDir, host.Name+".unreadable")
		return nil
	}
	a, ok := host.parse(f, o.Event)
	if !ok {
		o.mark(unknownDir, host.Name+"."+a.Event)
		if a.Gate && o.Stop {
			return host.refuse(a, refusal(HaltCheckFailed))
		}
		return nil
	}
	a.Rest = f
	w := identify(o.Agent, a)
	if a.Gate && o.Stop {
		if halt := o.halted(w); halt != "" {
			o.record(host, a, w, halt)
			return host.refuse(a, refusal(halt))
		}
	}
	if a.Kind != "" {
		o.record(host, a, w, "")
	}
	if a.Gate && host.allow != nil {
		return host.allow(a)
	}
	return nil
}

func refusal(halt string) string {
	switch halt {
	case StaleRules:
		return "Wake couldn't get the brake's current rules, so this was refused."
	case HaltCheckFailed:
		return "Wake couldn't check its halts, so this was refused."
	}
	return fmt.Sprintf("Wake's brake has halted this agent (halt %s), so this was refused.", halt)
}

// who is the agent acting, and every name a halt can stop it by.
type who struct {
	agent    string
	instance string
	lineage  []string
	names    []string // the agent, the subagent type it runs as, its instance and the instances that started it
}

// identify names the agent behind an action. Each session is an instance of the agent,
// and a subagent the host names is an instance started by its session.
func identify(agent string, a Action) who {
	w := who{agent: agent, names: []string{agent}}
	if a.Session == "" {
		return w
	}
	session := agent + "#" + a.Session
	w.instance, w.lineage = session, []string{agent}
	if a.Subagent != "" {
		kind := agent + "/" + a.SubagentType
		w.instance, w.lineage = kind+"#"+a.Subagent, []string{agent, session}
		w.names = append(w.names, kind)
	}
	w.names = append(w.names, w.lineage[1:]...)
	w.names = append(w.names, w.instance)
	return w
}

// record spools an action as a Wake event: a denied event when halt names what refused
// it. Recording never fails the hook: a dropped event is counted, and that's all.
func (o Options) record(host *Host, a Action, w who, halt string) {
	id := o.uuid()
	kind := a.Kind
	if halt != "" {
		kind = "denied"
	}
	s := span{
		TraceID: traceID(host.Name, a.Session, o.Rand),
		SpanID:  o.hex(8),
		Name:    kind,
		Kind:    spanKindInternal,
	}
	end := o.Now().UnixNano()
	start := end
	if a.Duration > 0 && a.Duration < 24*time.Hour {
		start = end - a.Duration.Nanoseconds()
	}
	s.Start, s.End = fmt.Sprint(start), fmt.Sprint(end)

	s.str("wake.event.id", id)
	s.str("wake.event.kind", kind)
	s.str("wake.agent.id", w.agent)
	if w.instance != "" {
		s.str("wake.agent.instance", w.instance)
		s.strs("wake.agent.lineage", w.lineage)
	}
	s.str("wake.capture", "hook")
	s.str("wake.hook.host", host.Name)

	payload := map[string]any{}
	switch {
	case halt != "":
		// The call the agent tried, with the refusal as its output, as the SDKs record theirs.
		s.str("wake.brake.halt_id", halt)
		payload["input"], payload["output"] = a.Input, refusal(halt)
	case kind == "agent_message":
		s.strs("wake.origins", []string{"user"})
		payload["from"], payload["to"], payload["content"] = "user", w.agent, a.Input
	default:
		s.strs("wake.origins", []string{"tool:" + a.Tool})
		payload["input"], payload["output"] = a.Input, a.Output
	}
	if a.Tool != "" && !a.Prompt {
		s.Name += " " + a.Tool
		s.str("gen_ai.operation.name", "execute_tool")
		s.str("gen_ai.tool.name", a.Tool)
		if a.CallID != "" {
			s.str("gen_ai.tool.call.id", a.CallID)
		}
	}
	if !a.Prompt {
		if prompt := o.lastPrompt(host.Name, a.Session); prompt != "" {
			s.strs("wake.parents", []string{prompt})
		}
	}
	if len(a.Rest) > 0 {
		payload["hook"] = a.Rest
	}
	s.str("wake.payload", string(cut(encode(payload))))

	data, err := json.Marshal(s)
	if err != nil || o.spool(data) != nil {
		return
	}
	if a.Prompt && halt == "" {
		o.setLastPrompt(host.Name, a.Session, id)
	}
}

// traceID derives a session's trace from the host and its id, so every event of the
// session shares one. An event outside any session gets a trace of its own.
func traceID(host, session string, r io.Reader) string {
	if session == "" {
		b := make([]byte, 16)
		io.ReadFull(r, b)
		return hex.EncodeToString(b)
	}
	sum := sha256.Sum256([]byte("wake-hook\x00" + host + "\x00" + session))
	return hex.EncodeToString(sum[:16])
}

func (o Options) hex(n int) string {
	b := make([]byte, n)
	io.ReadFull(o.Rand, b)
	return hex.EncodeToString(b)
}

// uuid is a random (version 4) UUID.
func (o Options) uuid() string {
	b := make([]byte, 16)
	io.ReadFull(o.Rand, b)
	b[6] = b[6]&0x0f | 0x40
	b[8] = b[8]&0x3f | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

// encode is JSON without HTML escaping, so a payload keeps <, > and & as the host sent them.
func encode(v any) []byte {
	var b bytes.Buffer
	e := json.NewEncoder(&b)
	e.SetEscapeHTML(false)
	if e.Encode(v) != nil {
		return []byte("null")
	}
	return bytes.TrimSuffix(b.Bytes(), []byte("\n"))
}

// cut sends a payload over 4 MiB as the SDKs do: its size, its SHA-256 and its first
// 2 MiB, cut where a character starts.
func cut(payload []byte) []byte {
	if len(payload) <= maxPayload {
		return payload
	}
	sum := sha256.Sum256(payload)
	n := prefixLen
	for n > 0 && !utf8.RuneStart(payload[n]) {
		n--
	}
	return encode(map[string]any{
		"truncated": true,
		"bytes":     len(payload),
		"sha256":    hex.EncodeToString(sum[:]),
		"prefix":    string(payload[:n]),
	})
}
