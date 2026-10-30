package hook

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// The directory the hook and serve share. Each hook writes what it has to say as a file
// of its own and renames it into place, so hooks running at once never lock each other.
const (
	spoolDir    = "spool"    // events waiting to be sent, one per file, named <unix nanos>-<bytes>-<random>.json
	droppedDir  = "dropped"  // a file per event dropped because the spool was full
	unknownDir  = "unknown"  // a file per hook event wake-hook doesn't handle, named after it
	rejectedDir = "rejected" // events the collector refused, kept for a person to look at
	sessionsDir = "sessions" // the latest prompt of each session, a parent for what follows it
	rulesDir    = "rules"    // the brake's rules for each agent, and the agents a hook wants rules for
	flowDir     = "flow"     // what entered each agent of each session, for the flow policy (flow.go)
)

// The spool's bounds. Past them an event is dropped and counted, so a collector that's
// down never fills the disk.
var (
	maxSpoolEvents = 10000
	maxSpoolBytes  = 512 << 20
)

var errSpoolFull = errors.New("spool full")

// write puts data at path by renaming a finished file into place, so a reader never sees
// half of it.
func write(path string, data []byte, r func(int) string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	tmp := filepath.Join(filepath.Dir(path), "."+filepath.Base(path)+"."+r(4)+".tmp")
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		os.Remove(tmp)
		return err
	}
	return nil
}

// spool adds one event for serve to send, unless the spool is full.
func (o Options) spool(data []byte) error {
	dir := filepath.Join(o.Dir, spoolDir)
	names, err := readNames(dir)
	if err != nil && !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	count, size := 0, len(data)
	for _, name := range names {
		if n, ok := spooledSize(name); ok {
			count, size = count+1, size+n
		}
	}
	if count >= maxSpoolEvents || size > maxSpoolBytes {
		o.mark(droppedDir, "spool_full")
		return errSpoolFull
	}
	name := fmt.Sprintf("%019d-%d-%s.json", o.Now().UnixNano(), len(data), o.hex(4))
	return write(filepath.Join(dir, name), data, o.hex)
}

// spooledSize reads an event's size from its name, so bounding the spool never opens it.
func spooledSize(name string) (int, bool) {
	parts := strings.Split(name, "-")
	if len(parts) != 3 || !strings.HasSuffix(name, ".json") {
		return 0, false
	}
	n, err := strconv.Atoi(parts[1])
	return n, err == nil
}

func readNames(dir string) ([]string, error) {
	d, err := os.Open(dir)
	if err != nil {
		return nil, err
	}
	defer d.Close()
	return d.Readdirnames(-1)
}

// mark leaves an empty file under sub, named after what happened, for serve to count.
func (o Options) mark(sub, label string) {
	dir := filepath.Join(o.Dir, sub)
	if os.MkdirAll(dir, 0o700) != nil {
		return
	}
	name := fmt.Sprintf("%019d-%s-%s", o.Now().UnixNano(), safe(label), o.hex(4))
	if f, err := os.OpenFile(filepath.Join(dir, name), os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600); err == nil {
		f.Close()
	}
}

// safe keeps a label to what a file name can hold anywhere.
func safe(label string) string {
	b := []byte(label)
	for i, c := range b {
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_' || c == '.') {
			b[i] = '_'
		}
	}
	if len(b) > 64 {
		b = b[:64]
	}
	if len(b) == 0 {
		return "_"
	}
	return string(b)
}

// name gives a file name for any string, such as a session id or an agent's name.
func name(parts ...string) string {
	sum := sha256.Sum256([]byte(strings.Join(parts, "\x00")))
	return hex.EncodeToString(sum[:16])
}

func (o Options) lastPrompt(host, session string) string {
	if session == "" {
		return ""
	}
	data, err := os.ReadFile(filepath.Join(o.Dir, sessionsDir, name(host, session)))
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(data))
}

func (o Options) setLastPrompt(host, session, id string) {
	if session != "" {
		write(filepath.Join(o.Dir, sessionsDir, name(host, session)), []byte(id), o.hex)
	}
}

// rulesFile is what serve writes for each agent: the brake's rules as wake-proxy last gave
// them, and when.
type rulesFile struct {
	Agent   string          `json:"agent"`
	Fetched int64           `json:"fetched_unix_nano"`
	Rules   json.RawMessage `json:"rules"`
}

func rulesPath(dir, agent string) string {
	return filepath.Join(dir, rulesDir, name(agent)+".json")
}

func wantPath(dir, agent string) string {
	return filepath.Join(dir, rulesDir, name(agent)+".want")
}

// brakeRules is what the hook reads of the rules wake-proxy serves: the halts in force,
// and the flow policy (flow.go).
type brakeRules struct {
	Halts json.RawMessage `json:"halts"`
	Flow  json.RawMessage `json:"flow"`
}

// rules reads the brake's rules serve last left for agent. While they can't be used,
// problem names the halt every gated call is refused under: StaleRules while they're
// missing or too old, and HaltCheckFailed when they can't be read. It fails closed.
func (o Options) rules(agent string) (rules *brakeRules, problem string) {
	data, err := os.ReadFile(rulesPath(o.Dir, agent))
	if errors.Is(err, fs.ErrNotExist) {
		o.want(agent)
		return nil, StaleRules
	}
	var f rulesFile
	if err != nil || json.Unmarshal(data, &f) != nil || f.Agent != agent {
		o.want(agent)
		return nil, HaltCheckFailed
	}
	age := o.Now().Sub(time.Unix(0, f.Fetched))
	if age > o.TTL || age < -o.TTL {
		o.want(agent)
		return nil, StaleRules
	}
	rules = &brakeRules{}
	if _, ok := object(f.Rules); !ok || json.Unmarshal(f.Rules, rules) != nil {
		return nil, HaltCheckFailed
	}
	return rules, ""
}

// want asks serve to fetch agent's rules, unless it's been asked already. A serve that
// restarted knows only the agents it's told of and those whose rules it finds, so the
// hook asks whenever the rules it reads are missing, stale or can't be read.
func (o Options) want(agent string) {
	if _, err := os.Stat(wantPath(o.Dir, agent)); err != nil {
		write(wantPath(o.Dir, agent), []byte(agent), o.hex)
	}
}

// halted names the halt in rules that stops w, or "" when none does. Halts it can't read
// are HaltCheckFailed.
func halted(rules *brakeRules, w who) (halt string) {
	defer func() {
		if recover() != nil {
			halt = HaltCheckFailed
		}
	}()
	if rules.Halts == nil {
		// A wake-proxy older than stops in the agent's process says nothing of halts:
		// there's nothing to go on here, and the proxy deny still holds. Only a missing
		// field means that: halts of null are halts that can't be read.
		return ""
	}
	var halts []struct {
		ID     string   `json:"id"`
		Fleet  bool     `json:"fleet"`
		Agents []string `json:"agents"`
	}
	if !startsWith(rules.Halts, '[') || json.Unmarshal(rules.Halts, &halts) != nil {
		return HaltCheckFailed
	}
	for _, h := range halts {
		if h.ID == "" {
			return HaltCheckFailed
		}
		if h.Fleet {
			return h.ID
		}
		for _, agent := range h.Agents {
			for _, n := range w.names {
				if agent == n {
					return h.ID
				}
			}
		}
	}
	return ""
}
