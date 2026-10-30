package hook

// The flow policy (Contain): the rules wake-proxy enforces to keep content from an
// untrusted origin, such as a page a tool fetched, from triggering a privileged call. It
// serves them with the brake's rules, and the hook applies them to the calls its host
// makes, which never pass wake-proxy, as the SDKs do in an agent's own process (wake.flow
// in the Python SDK, docs/flow-everywhere.md). A rule decides on what entered the calling
// agent within the policy's window:
//
//   - a tool's result enters the agent that called it as tool:<name>, by the tool's own
//     name: an MCP tool's, as wake-proxy names it;
//   - a subagent starts with what had entered the agent starting it, and its answer brings
//     back what entered it, each as agent:<sender> with whatever had entered the sender. A
//     Cursor subagent's events are placed in the session that started it (place);
//   - the user's prompt isn't an origin, and isn't checked.
//
// Each hook is a process of its own, so what entered each agent is kept as files, one per
// origin, for the hooks that follow to read. sdk/testdata/flow_policy.json pins the
// decisions every enforcer makes alike.

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

const (
	// UnreadablePolicy is the halt every gated call is refused under while the flow policy
	// can't be used, or checked.
	UnreadablePolicy = "policy:unreadable-policy"

	defaultWindow = 900    // seconds: a policy's window when it sets none, or when there's no policy
	maxWindow     = 86_400 // seconds: the longest window a policy may set; serve forgets a session idle this long
	// maxOrigins is the most distinct origins kept for an agent within the window. Past it,
	// the agent counts as having every origin until the window passes.
	maxOrigins = 256

	// Why an agent counts as having every origin.
	saturated   = "more origins entered it than Wake keeps"
	carried     = "a message or reply reached it from one with every origin"
	noSession   = "the hook event names no session"
	unseenStart = "it's a subagent whose start Wake didn't see"
	unfollowed  = "it started a subagent whose answer Wake can't follow"
)

// A flowRule is one rule: the calls it applies to, and the origins it keeps from them. An
// empty list leaves that out, so a rule naming no callers applies to every caller.
type flowRule struct {
	id           string
	callers      []string
	receivers    []string
	pathPrefixes []string // only ever meets HTTP requests at wake-proxy
	tools        []string
	stores       []string // only ever meets the SDKs' memory writes
	forbid       []string
	allow        []string
}

// keeps is the origins, of these, the rule keeps from a call, in the order given: those a
// forbid_origins pattern matches, and, with allow_origins, those none of its patterns
// matches. So an allowlist passes a caller nothing entered.
func (r *flowRule) keeps(origins []string) []string {
	var kept []string
	for _, o := range origins {
		if anyMatch(r.forbid, o) || len(r.allow) > 0 && !anyMatch(r.allow, o) {
			kept = append(kept, o)
		}
	}
	return kept
}

// A flowPolicy is the policy as wake-proxy serves it: its rules, in file order, and its
// window in seconds. One that can't be used, unreadable saying why, refuses every call.
type flowPolicy struct {
	rules      []flowRule
	window     int64
	unreadable string
}

// seen is what entered an agent within the window: its origins, newest first, or, when
// every is set, every origin as far as a policy knows, and why.
type seen struct {
	origins []string
	every   string
}

// A call is what a gate decides on: a tool call, by its tool's own name, or reaching an
// agent, by its name, as a subagent's start does.
type call struct {
	tool     string
	reaches  bool
	receiver string
}

// A denial is a rule refusing a call: the halt it's refused under, policy:<id>, and the
// origins behind the call it keeps from it, or * for every origin, with why.
type denial struct {
	halt    string
	origins []string
	every   string
}

// deciding is the first rule that refuses a call by caller, given what entered it, or nil
// to let it through. lineage names the agents caller acts under. A tool call meets the
// rules wake-proxy's ext_authz applies to every call, those naming no tools, receivers,
// path_prefixes or stores, then those whose tools match the tool. Reaching an agent meets
// those naming no tools, path_prefixes or stores whose receivers are empty or name it.
// Each is in file order.
func (p *flowPolicy) deciding(caller string, lineage []string, s seen, c call) *denial {
	if p.unreadable != "" {
		return &denial{halt: UnreadablePolicy, origins: []string{}}
	}
	var rules []*flowRule
	for i := range p.rules {
		r := &p.rules[i]
		switch {
		case len(r.tools) > 0, len(r.pathPrefixes) > 0, len(r.stores) > 0:
			// Rules for tools come next. Rules for paths only meet HTTP requests at wake-proxy,
			// and rules for stores only the SDKs' memory writes.
		case !c.reaches && len(r.receivers) == 0,
			c.reaches && (len(r.receivers) == 0 || contains(r.receivers, c.receiver)):
			rules = append(rules, r)
		}
	}
	if !c.reaches {
		for i := range p.rules {
			if anyMatch(p.rules[i].tools, c.tool) {
				rules = append(rules, &p.rules[i])
			}
		}
	}
	for _, r := range rules {
		// A rule for an agent covers the calls made under it, a subagent's included.
		if len(r.callers) > 0 && !contains(r.callers, caller) && !containsAny(r.callers, lineage) {
			continue
		}
		if s.every != "" {
			return &denial{halt: "policy:" + r.id, origins: []string{"*"}, every: s.every}
		}
		if kept := r.keeps(s.origins); len(kept) > 0 {
			return &denial{halt: "policy:" + r.id, origins: kept}
		}
	}
	return nil
}

// matches is exact, or a prefix match for a pattern ending in *: a bare * matches every value.
func matches(pattern, value string) bool {
	if prefix, ok := strings.CutSuffix(pattern, "*"); ok {
		return strings.HasPrefix(value, prefix)
	}
	return value == pattern
}

func anyMatch(patterns []string, value string) bool {
	for _, p := range patterns {
		if matches(p, value) {
			return true
		}
	}
	return false
}

func contains(list []string, value string) bool {
	for _, v := range list {
		if v == value {
			return true
		}
	}
	return false
}

func containsAny(list, values []string) bool {
	for _, v := range values {
		if contains(list, v) {
			return true
		}
	}
	return false
}

// acting is every name a caller acts under: these, and the agent each instance,
// <agent>#<id>, is a run of.
func acting(names []string) []string {
	out := make([]string, 0, 2*len(names))
	for _, n := range names {
		out = append(out, n)
		if i := strings.LastIndexByte(n, '#'); i > 0 {
			out = append(out, n[:i])
		}
	}
	return out
}

// servedPolicy reads the flow field of the rules wake-proxy serves: nil when there's none,
// as from a wake-proxy that enforces no policy, or one older than this, so nothing is
// checked. A policy wake-proxy can't use, or that isn't valid, can't be used here either.
func servedPolicy(raw json.RawMessage) *flowPolicy {
	raw = bytes.TrimSpace(raw)
	if len(raw) == 0 || string(raw) == "null" {
		return nil
	}
	if f, ok := object(raw); ok && len(f) == 1 && string(f["unreadable"]) == "true" {
		return &flowPolicy{unreadable: "wake-proxy can't use its policy file"}
	}
	p, err := parsePolicy(raw)
	if err != nil {
		return &flowPolicy{unreadable: "the flow policy isn't valid: " + err.Error()}
	}
	return p
}

// The lists a rule may have, and whether each entry may be a prefix ending in *.
var ruleLists = []struct {
	name     string
	patterns bool
}{
	{"callers", false}, {"receivers", false}, {"path_prefixes", false},
	{"tools", true}, {"stores", true}, {"forbid_origins", true}, {"allow_origins", true},
}

// parsePolicy reads a policy as wake-proxy serves it, and checks it as wake-proxy checks
// its file: a field it doesn't know, one given twice, a value of the wrong type, a rule
// that keeps nothing, names tools with receivers or path_prefixes, or stores with any of
// those, an id that isn't 1 to 64 of A-Z a-z 0-9 . _ : - or that another rule has, an
// entry that's empty or has a * anywhere a pattern can't, or a window outside 1 to 86,400
// seconds, and it's refused.
func parsePolicy(raw []byte) (*flowPolicy, error) {
	raw = bytes.TrimSpace(raw)
	if err := uniqueKeys(raw); err != nil {
		return nil, err
	}
	top, ok := object(raw)
	if !ok {
		return nil, errors.New("a policy must be a JSON object")
	}
	for key := range top {
		if key != "version" && key != "window_seconds" && key != "rules" {
			return nil, fmt.Errorf("unknown field %q", key)
		}
	}
	if v, ok := top["version"]; ok {
		if _, ok := jsonString(v); !ok {
			return nil, errors.New("version must be a string")
		}
	}
	p := &flowPolicy{window: defaultWindow}
	if v, ok := top["window_seconds"]; ok {
		w, ok := wholeNumber(v)
		if !ok || w < 1 || w > maxWindow {
			return nil, fmt.Errorf("window_seconds must be a whole number from 1 to %d, not %s", maxWindow, v)
		}
		p.window = w
	}
	if v, ok := top["rules"]; ok {
		var rules []json.RawMessage
		if !startsWith(v, '[') || json.Unmarshal(v, &rules) != nil {
			return nil, errors.New("rules must be a list")
		}
		ids := map[string]bool{}
		for _, raw := range rules {
			r, err := parseRule(raw)
			if err != nil {
				return nil, err
			}
			if ids[r.id] {
				return nil, fmt.Errorf("two rules are %q", r.id)
			}
			ids[r.id] = true
			p.rules = append(p.rules, r)
		}
	}
	return p, nil
}

func parseRule(raw json.RawMessage) (flowRule, error) {
	fields, ok := object(raw)
	if !ok {
		return flowRule{}, errors.New("each rule must be a JSON object")
	}
	for key := range fields {
		known := key == "id"
		for _, l := range ruleLists {
			known = known || key == l.name
		}
		if !known {
			return flowRule{}, fmt.Errorf("unknown field %q in a rule", key)
		}
	}
	id, ok := jsonString(fields["id"])
	if !ok || !validID(id) {
		return flowRule{}, fmt.Errorf("rule id %s must be 1 to 64 of A-Z a-z 0-9 . _ : -", fields["id"])
	}
	lists := map[string][]string{}
	for _, l := range ruleLists {
		var entries []string
		if v, ok := fields[l.name]; ok {
			if entries, ok = jsonStrings(v); !ok {
				return flowRule{}, fmt.Errorf("rule %s: %s must be a list of strings", id, l.name)
			}
		}
		for _, e := range entries {
			star := strings.IndexByte(e, '*')
			if e == "" || star >= 0 && (!l.patterns || star != len(e)-1) {
				return flowRule{}, fmt.Errorf("rule %s: %s entry %q must be exact, or for a pattern a prefix ending in *", id, l.name, e)
			}
		}
		lists[l.name] = entries
	}
	r := flowRule{
		id: id, callers: lists["callers"], receivers: lists["receivers"], pathPrefixes: lists["path_prefixes"],
		tools: lists["tools"], stores: lists["stores"], forbid: lists["forbid_origins"], allow: lists["allow_origins"],
	}
	switch {
	case len(r.forbid) == 0 && len(r.allow) == 0:
		return r, fmt.Errorf("rule %s keeps nothing: give it forbid_origins or allow_origins", id)
	case len(r.tools) > 0 && (len(r.receivers) > 0 || len(r.pathPrefixes) > 0):
		return r, fmt.Errorf("rule %s names tools with receivers or path_prefixes", id)
	case len(r.stores) > 0 && (len(r.tools) > 0 || len(r.receivers) > 0 || len(r.pathPrefixes) > 0):
		return r, fmt.Errorf("rule %s names stores with tools, receivers or path_prefixes", id)
	}
	return r, nil
}

func validID(id string) bool {
	if id == "" || len(id) > 64 {
		return false
	}
	for _, c := range []byte(id) {
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || strings.IndexByte("._:-", c) >= 0) {
			return false
		}
	}
	return true
}

func startsWith(raw []byte, c byte) bool {
	return len(raw) > 0 && raw[0] == c
}

// object reads a JSON object's fields; JSON's null isn't one.
func object(raw []byte) (map[string]json.RawMessage, bool) {
	var f map[string]json.RawMessage
	if !startsWith(raw, '{') || json.Unmarshal(raw, &f) != nil {
		return nil, false
	}
	return f, true
}

func jsonString(raw []byte) (string, bool) {
	var s string
	if !startsWith(raw, '"') || json.Unmarshal(raw, &s) != nil {
		return "", false
	}
	return s, true
}

func jsonStrings(raw []byte) ([]string, bool) {
	var items []json.RawMessage
	if !startsWith(raw, '[') || json.Unmarshal(raw, &items) != nil {
		return nil, false
	}
	out := make([]string, 0, len(items))
	for _, item := range items {
		s, ok := jsonString(item)
		if !ok {
			return nil, false
		}
		out = append(out, s)
	}
	return out, true
}

// wholeNumber reads a JSON number written as a whole number: 900, but not 900.0 or 9e2,
// which wake-proxy refuses.
func wholeNumber(raw []byte) (int64, bool) {
	digits := bytes.TrimPrefix(raw, []byte("-"))
	if len(digits) == 0 || bytes.IndexFunc(digits, func(r rune) bool { return r < '0' || r > '9' }) >= 0 {
		return 0, false
	}
	n, err := strconv.ParseInt(string(raw), 10, 64)
	return n, err == nil
}

var errDuplicate = errors.New("a field is given twice")

// uniqueKeys refuses JSON with an object that names a field twice, as wake-proxy does:
// which of the two the writer meant isn't known.
func uniqueKeys(raw []byte) error {
	d := json.NewDecoder(bytes.NewReader(raw))
	if err := uniqueValue(d); err != nil {
		return err
	}
	if _, err := d.Token(); err != io.EOF {
		return errors.New("more than one JSON value")
	}
	return nil
}

func uniqueValue(d *json.Decoder) error {
	t, err := d.Token()
	if err != nil {
		return err
	}
	switch t {
	case json.Delim('{'):
		keys := map[string]bool{}
		for d.More() {
			k, err := d.Token()
			if err != nil {
				return err
			}
			key, _ := k.(string)
			if keys[key] {
				return errDuplicate
			}
			keys[key] = true
			if err := uniqueValue(d); err != nil {
				return err
			}
		}
		_, err = d.Token()
	case json.Delim('['):
		for d.More() {
			if err := uniqueValue(d); err != nil {
				return err
			}
		}
		_, err = d.Token()
	}
	return err
}

// flowReason is what the agent is told of a call the flow policy refuses, in the SDKs'
// words. who is the agent held back, and outcome what became of the call.
func flowReason(d *denial, who, outcome string) string {
	switch {
	case d.halt == UnreadablePolicy:
		return fmt.Sprintf("Wake's flow policy can't be read, so %s.", outcome)
	case d.every != "":
		return fmt.Sprintf("Wake's flow policy (%s) holds %s back, because %s, so %s.", d.halt, who, d.every, outcome)
	}
	return fmt.Sprintf("Wake's flow policy (%s) holds %s back after %s, so %s.", d.halt, who, strings.Join(d.origins, ", "), outcome)
}

// flowAgent is the agent an action is made by, as the flow policy names it: the agent,
// or <agent>/<agent type> for a subagent Claude Code names, or a Cursor subagent placed
// (place), as the SDKs name it.
func flowAgent(host *Host, agent string, a Action) string {
	if host.subagents && a.Subagent != "" && a.SubagentType != "" {
		return agent + "/" + a.SubagentType
	}
	return agent
}

// flowRefusal is the refusal of a gated call that the flow policy in the brake's rules
// holds back, if it does: a tool call, by its tool's own name, and a subagent's start, as
// reaching it. by is the agent making the call, as the policy names it. Any failure of the
// check refuses, as a policy that can't be read does.
func (o Options) flowRefusal(host *Host, a Action, w who, by string, rules *brakeRules) (r *refused) {
	outcome := "this tool call wasn't run"
	if a.flow.onlyStarts {
		outcome = "this subagent wasn't started"
	}
	unreadable := &refused{halt: UnreadablePolicy, reason: flowReason(&denial{halt: UnreadablePolicy}, by, outcome),
		flow: true, origins: []string{}}
	defer func() {
		if recover() != nil {
			r = unreadable
		}
	}()
	p := servedPolicy(rules.Flow)
	if p == nil {
		return nil
	}
	if p.unreadable != "" {
		return unreadable
	}
	s := seen{every: noSession}
	switch {
	case a.flow.unplaced:
		// What it started with isn't known.
		s = seen{every: unseenStart}
	case a.Session != "":
		var err error
		if s, err = o.flowStore(host.Name, a.Session, p.window).seen(by, o.Now().UnixNano()); err != nil {
			return unreadable
		}
	}
	lineage := acting(w.names)
	var d *denial
	if !a.flow.onlyStarts {
		d = p.deciding(by, lineage, s, call{tool: host.toolName(a.Tool)})
	}
	if d == nil && a.flow.starts {
		d = p.deciding(by, lineage, s, call{reaches: true, receiver: w.agent + "/" + a.flow.subagent})
	}
	if d == nil {
		return nil
	}
	return &refused{halt: d.halt, reason: flowReason(d, by, outcome), flow: true, origins: d.origins, every: d.every}
}

// entered notes what an action that went ahead brings into the agents of its session:
// a tool's result enters the agent that called it, as tool:<name>; a subagent starts with
// what had entered the agent starting it, and its answer brings back what entered it.
// It's noted whether or not anything is checked, since a gate that checks reads what the
// hooks that don't noted. w is who acts, and by the agent it is to the flow policy.
// Noting never fails the hook.
func (o Options) entered(host *Host, a Action, w who, by string) {
	defer func() { recover() }()
	f := a.flow
	switch {
	case a.Session == "":
		return // nothing to keep it by
	case !f.stopped && !f.starts && a.Kind != "tool_call":
		return // a prompt, or a call yet to run
	}
	window := o.flowWindow(o.Agent)
	s := o.flowStore(host.Name, a.Session, window)
	now := o.Now().UnixNano()
	target := o.Agent + "/" + f.subagent
	known := o.subagents(host.Name)
	switch {
	case f.stopped && host.conversations:
		// A Cursor subagent stopped: subagentStop names its conversation, which its first
		// event placed. When none was, Wake followed nothing the subagent did, as before
		// Cursor named the Task call on its hooks, so it can't tell what the answer brings.
		if p, ok := known.get(byConversation, f.child); ok {
			o.flowStore(host.Name, p.Session, window).message(p.From, p.To, now)
		} else {
			s.saturate(by, now, unfollowed)
		}
	case f.stopped:
		// A subagent left running in the background has stopped: its answer reaches the
		// agent that started it.
		if from, to, ok := s.answered(a.Subagent); ok {
			s.message(from, to, now)
		}
	case f.starts && a.Gate:
		s.message(by, target, now)
		if f.call != "" {
			// Cursor's subagent: its own hooks will name the call starting it.
			known.keep(byTaskCall, f.call, subagentFile{
				Call: f.call, Session: a.Session, Type: f.subagent, From: target, To: by,
				Lineage: append(append([]string{}, w.lineage...), w.instance),
			})
		}
	case f.starts && f.background:
		// It answers when it stops.
		if f.agentID != "" {
			s.remember(f.agentID, target, by)
		}
	case f.starts:
		s.message(target, by, now)
	case f.answers:
		// A Cursor Task call came back with its subagent's answer.
		p, ok := known.get(byTaskCall, a.CallID)
		switch {
		case ok:
			session := o.flowStore(host.Name, p.Session, window)
			session.message(p.From, p.To, now)
			if p.Conversation == "" {
				// None of the subagent's own events named the call, so Wake followed none.
				session.saturate(p.To, now, unfollowed)
			}
		case a.Event == "postToolUse":
			// A subagent ran whose start wake-hook didn't see.
			s.saturate(by, now, unfollowed)
		default:
			// It failed, starting none, as when its start was refused.
			s.note(by, "tool:"+host.toolName(a.Tool), now, now)
		}
	default:
		s.note(by, "tool:"+host.toolName(a.Tool), now, now)
	}
}

// place puts an event of a Cursor subagent in the session of the agent that started it,
// as that subagent: <agent>/<subagent type>, an instance of its own by the Task call that
// started it. Cursor runs each subagent in a conversation of its own, but from 3.22 the
// subagent's tool hooks name the Task call that started it (parent_tool_call_id), which
// subagentStart named too, and wake-hook kept when it let that start through. Once one of
// its events is placed, so is every event in its conversation: the shell, MCP and file
// hooks, which name no call, and the start of a subagent of its own. subagentStart and
// subagentStop belong to the agent starting the subagent, whose conversation they name
// from 3.27 (parent_agent_id). An event naming a call whose start wake-hook didn't let
// through stays in its own conversation, and its subagent counts as having every origin,
// since what it started with isn't known. Placing never fails the hook: an event it can't
// place stays where it is.
func (o Options) place(host *Host, a *Action) {
	defer func() { recover() }()
	f := &a.flow
	known := o.subagents(host.Name)
	var p subagentFile
	var ok bool
	switch {
	case f.parentCall != "":
		if p, ok = known.get(byTaskCall, f.parentCall); !ok {
			f.unplaced = true
			return
		}
		if a.Session != "" && p.Conversation != a.Session {
			// Its first event: what it does from now is followed, and its answer with it.
			p.Conversation = a.Session
			known.keep(byTaskCall, p.Call, p)
			known.keep(byConversation, a.Session, p)
		}
	case f.spawner != "" && f.spawner != a.Session:
		if p, ok = known.get(byConversation, f.spawner); !ok {
			p, ok = subagentFile{Session: f.spawner}, true // the agent's own conversation
		}
	default:
		p, ok = known.get(byConversation, a.Session)
	}
	if !ok {
		return
	}
	if a.Rest == nil {
		a.Rest = map[string]any{}
	}
	a.Rest["conversation_id"] = a.Session // its own, kept in the payload
	a.Session, a.Subagent, a.SubagentType, a.lineage = p.Session, p.Call, p.Type, p.Lineage
}

// flowWindow is the window of the flow policy in the rules serve last left for agent, or
// the default while there's none to read: what entered an agent counts for that long, and
// toward the bound.
func (o Options) flowWindow(agent string) int64 {
	if data, err := os.ReadFile(rulesPath(o.Dir, agent)); err == nil {
		var f rulesFile
		var rules brakeRules
		if json.Unmarshal(data, &f) == nil && f.Agent == agent && json.Unmarshal(f.Rules, &rules) == nil {
			if p := servedPolicy(rules.Flow); p != nil && p.unreadable == "" {
				return p.window
			}
		}
	}
	return defaultWindow
}

// A flowStore holds what entered each agent of one session, as files, so each hook sees
// what the others noted. The session's directory holds one for each agent, with a file
// for each origin that entered it, saying when it last did, and, once more entered it
// than are kept, a file saying since when, and why, it has had every origin. Each is
// written whole and renamed into place, as the rest of the hook's files are.
type flowStore struct {
	dir    string
	window int64 // nanoseconds
}

func (o Options) flowStore(host, session string, windowSeconds int64) flowStore {
	return flowStore{dir: filepath.Join(o.Dir, flowDir, name(host, session)), window: windowSeconds * int64(time.Second)}
}

// An originFile is one origin that entered an agent, and when it last did, in Unix nanoseconds.
type originFile struct {
	Origin string `json:"origin"`
	At     int64  `json:"at"`
}

// An everyFile says an agent has had every origin since a time, and why.
type everyFile struct {
	Since int64  `json:"since"`
	Why   string `json:"why"`
}

// A backgroundFile is a subagent left running in the background: the agent it is, and
// the agent its answer goes to when it stops.
type backgroundFile struct {
	From string `json:"from"`
	To   string `json:"to"`
}

const everyName = "every"

func (s flowStore) agentDir(agent string) string {
	return filepath.Join(s.dir, "agents", name(agent))
}

// put writes a file of the store. A file name is never guessed by anyone else, so it
// doesn't need the hook's randomness, which tests fix.
func (s flowStore) put(path string, v any) error {
	data, err := json.Marshal(v)
	if err != nil {
		return err
	}
	if err = write(path, data, randomHex); err != nil {
		// serve may have just swept the session, idle for a day, from under it.
		err = write(path, data, randomHex)
	}
	return err
}

func randomHex(n int) string {
	b := make([]byte, n)
	rand.Read(b)
	return hex.EncodeToString(b)
}

func readJSON(path string, v any) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return json.Unmarshal(data, v)
}

// note notes that origin entered agent at at, now being now: the file for it keeps the
// latest time it did. Origins older than the window don't count toward maxOrigins; past
// it, the agent has every origin from at.
func (s flowStore) note(agent, origin string, at, now int64) {
	dir := s.agentDir(agent)
	path := filepath.Join(dir, name(origin))
	var known originFile
	if readJSON(path, &known) == nil {
		if known.At >= at {
			return
		}
	} else if s.full(dir, now) {
		s.saturate(agent, at, saturated)
		return
	}
	// Two hooks noting it at once could each write over the other, so it's written again
	// until the file holds a time at least this one.
	for range 3 {
		if s.put(path, originFile{Origin: origin, At: at}) != nil {
			return
		}
		if readJSON(path, &known) == nil && known.At >= at {
			return
		}
	}
}

// full says whether an agent's directory has no room for another origin. Once it holds
// maxOrigins, those older than the window are forgotten to make room, and it's full if
// maxOrigins still entered within the window.
func (s flowStore) full(dir string, now int64) bool {
	names, _ := readNames(dir)
	var files []string
	for _, n := range names {
		if !strings.HasPrefix(n, ".") && n != everyName {
			files = append(files, filepath.Join(dir, n))
		}
	}
	if len(files) < maxOrigins {
		return false
	}
	count := 0
	for _, path := range files {
		var o originFile
		switch err := readJSON(path, &o); {
		case errors.Is(err, os.ErrNotExist):
		case err == nil && o.At < now-s.window:
			os.Remove(path)
		default:
			// Within the window, or a file that can't be read, which could be.
			count++
		}
	}
	return count >= maxOrigins
}

// saturate notes that agent has had every origin since since, for why, unless it has since later.
func (s flowStore) saturate(agent string, since int64, why string) {
	path := filepath.Join(s.agentDir(agent), everyName)
	var known everyFile
	if readJSON(path, &known) == nil && known.Since > since {
		return
	}
	s.put(path, everyFile{Since: since, Why: why})
}

// every is since when, and why, agent has had every origin, if it has since since.
func (s flowStore) every(agent string, since int64) (*everyFile, error) {
	var e everyFile
	switch err := readJSON(filepath.Join(s.agentDir(agent), everyName), &e); {
	case err == nil && e.Since >= since:
		return &e, nil
	case err != nil && !errors.Is(err, os.ErrNotExist):
		return nil, err
	}
	return nil, nil
}

// recorded is each origin noted for agent since since, with when it last entered, whether
// or not it has every origin.
func (s flowStore) recorded(agent string, since int64) (map[string]int64, error) {
	dir := s.agentDir(agent)
	names, err := readNames(dir)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	origins := map[string]int64{}
	for _, n := range names {
		if strings.HasPrefix(n, ".") || n == everyName {
			continue
		}
		var o originFile
		switch err := readJSON(filepath.Join(dir, n), &o); {
		case errors.Is(err, os.ErrNotExist):
			// Forgotten by a hook making room since the directory was read.
		case err != nil:
			return nil, err
		case o.At >= since && o.At > origins[o.Origin]:
			origins[o.Origin] = o.At
		}
	}
	return origins, nil
}

// what is what entered agent since since: each origin with when it last entered, or, when
// every is set, every origin, since when and why.
func (s flowStore) what(agent string, since int64) (origins map[string]int64, every *everyFile, err error) {
	if every, err = s.every(agent, since); every != nil || err != nil {
		return nil, every, err
	}
	origins, err = s.recorded(agent, since)
	return origins, nil, err
}

// seen is what entered agent within the window, now being now: each origin once, newest
// first, ties by name.
func (s flowStore) seen(agent string, now int64) (seen, error) {
	origins, every, err := s.what(agent, now-s.window)
	if err != nil {
		return seen{}, err
	}
	if every != nil {
		return seen{every: every.Why}, nil
	}
	if len(origins) > maxOrigins {
		// Two hooks noting at once can each take the last room there is.
		return seen{every: saturated}, nil
	}
	out := sortedOrigins(origins)
	return seen{origins: out}, nil
}

func sortedOrigins(origins map[string]int64) []string {
	out := make([]string, 0, len(origins))
	for o := range origins {
		out = append(out, o)
	}
	sort.Slice(out, func(i, j int) bool {
		if origins[out[i]] != origins[out[j]] {
			return origins[out[i]] > origins[out[j]]
		}
		return out[i] < out[j]
	})
	return out
}

// carry notes that content from sender reached receiver now: whatever had entered sender
// within the window enters receiver too. Each origin keeps when it entered, so passing
// content on never makes it newer, and agents talking back and forth don't keep it alive.
// receiver's own messages, agent:<receiver>, don't come back to it. When sender has every
// origin, so does receiver, for as long as sender does, and what entered sender after that
// time enters receiver as well, so it still counts once every origin no longer does.
func (s flowStore) carry(sender, receiver string, now int64) {
	since := now - s.window
	every, err := s.every(sender, since)
	if err != nil {
		return
	}
	if every != nil {
		s.saturate(receiver, every.Since, carried)
	}
	origins, err := s.recorded(sender, since)
	if err != nil {
		return
	}
	for _, o := range sortedOrigins(origins) {
		// What entered by the time every origin did counts no longer than every origin does.
		if o != "agent:"+receiver && (every == nil || origins[o] > every.Since) {
			s.note(receiver, o, origins[o], now)
		}
	}
}

// message notes that something from sender reached receiver now: the start of its run,
// or a subagent's answer. agent:<sender> enters receiver, with whatever had entered sender.
func (s flowStore) message(sender, receiver string, now int64) {
	if sender == "" || receiver == "" || sender == receiver {
		return
	}
	s.carry(sender, receiver, now)
	s.note(receiver, "agent:"+sender, now, now)
}

func (s flowStore) backgroundPath(agentID string) string {
	return filepath.Join(s.dir, "background", name(agentID))
}

// remember keeps a subagent left running in the background, by the id its hooks name it
// by, until it stops: from is the agent it is, and to the agent it answers.
func (s flowStore) remember(agentID, from, to string) {
	s.put(s.backgroundPath(agentID), backgroundFile{From: from, To: to})
}

// answered is the subagent in the background with this id, now that it has stopped, and
// the agent it answers, once.
func (s flowStore) answered(agentID string) (from, to string, ok bool) {
	if agentID == "" {
		return "", "", false
	}
	path := s.backgroundPath(agentID)
	var b backgroundFile
	if readJSON(path, &b) != nil || os.Remove(path) != nil {
		return "", "", false
	}
	return b.From, b.To, true
}

// Cursor's subagents are kept by the Task call that started each, and, once one of its own
// events named that call, by the conversation it runs in (place). Each is kept in a
// directory of its own under flow/, which serve forgets once it's been left a day, as it
// forgets a session's.
const (
	byTaskCall     = "task call"
	byConversation = "conversation"
)

// A subagentFile is a subagent Cursor started with a Task call: the call, the session of
// the agent that started it, its type, the agent it is and the agent it answers, as the
// flow policy names them, what started it, outermost first, and, once one of its events
// named the call, its conversation.
type subagentFile struct {
	Call         string   `json:"call"`
	Session      string   `json:"session"`
	Type         string   `json:"type"`
	From         string   `json:"from"`
	To           string   `json:"to"`
	Lineage      []string `json:"lineage"`
	Conversation string   `json:"conversation,omitempty"`
}

// subagents is where one host's subagents are kept, in the flow directory.
type subagents struct {
	root flowStore
	host string
}

func (o Options) subagents(host string) subagents {
	return subagents{root: flowStore{dir: filepath.Join(o.Dir, flowDir)}, host: host}
}

func (k subagents) path(by, id string) string {
	return filepath.Join(k.root.dir, name(k.host, by, id), "subagent")
}

// get is the subagent kept by id, a Task call's or a conversation's.
func (k subagents) get(by, id string) (p subagentFile, ok bool) {
	ok = id != "" && readJSON(k.path(by, id), &p) == nil
	return p, ok
}

func (k subagents) keep(by, id string, p subagentFile) {
	k.root.put(k.path(by, id), p)
}
