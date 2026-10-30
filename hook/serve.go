package hook

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

const (
	batchEvents   = 100
	batchBytes    = 8 << 20
	maxRulesBytes = 8 << 20 // the SDKs' limit on the brake's rules
	// How often what entered the agents of sessions idle for a day is swept away.
	sweepEvery = 10 * time.Minute
)

// A Server sends the events hooks spool to the collector, and fetches the brake's rules
// for the hooks to check. One runs for each user on each machine, beside the hooks.
type Server struct {
	Dir       string
	Collector string   // the collector's OTLP/HTTP endpoint, such as https://collector:4318
	Rules     string   // wake-proxy's address, such as http://wake-proxy:9003; no rules are fetched when empty
	Agents    []string // agents to fetch rules for from the start; a hook asks for any other
	Client    *http.Client
	Log       *log.Logger
	Version   string
	Now       func() time.Time // time.Now when nil

	opts    Options
	process string
	agents  map[string]string // agent -> the last error fetching its rules, so each new one is logged once
	swept   time.Time
}

// NewClient is the client serve talks to the collector and wake-proxy with: it trusts
// caFile's roots when given, and never goes through the environment's HTTP proxy.
func NewClient(caFile string) (*http.Client, error) {
	transport := &http.Transport{Proxy: nil, TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS12}}
	if caFile != "" {
		pem, err := os.ReadFile(caFile)
		if err != nil {
			return nil, err
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(pem) {
			return nil, fmt.Errorf("no certificates in %s", caFile)
		}
		transport.TLSClientConfig.RootCAs = pool
	}
	return &http.Client{Transport: transport, Timeout: 10 * time.Second}, nil
}

func (s *Server) init() {
	if s.agents != nil {
		return
	}
	s.opts = Options{Dir: s.Dir, Now: s.Now, Rand: rand.Reader}
	if s.opts.Now == nil {
		s.opts.Now = time.Now
	}
	s.process = fmt.Sprintf("%d.%s", os.Getpid(), s.opts.hex(6))
	s.agents = map[string]string{}
	for _, a := range s.Agents {
		s.agents[a] = ""
	}
	// And every agent a hook asked for before a restart, whose rules it goes on reading.
	files, _ := filepath.Glob(filepath.Join(s.Dir, rulesDir, "*.json"))
	for _, path := range files {
		var f rulesFile
		if data, err := os.ReadFile(path); err == nil && json.Unmarshal(data, &f) == nil &&
			f.Agent != "" && filepath.Base(path) == filepath.Base(rulesPath(s.Dir, f.Agent)) {
			s.agents[f.Agent] = ""
		}
	}
	if s.Log == nil {
		s.Log = log.New(os.Stderr, "wake-hook: ", log.LstdFlags)
	}
}

// Run makes a pass every interval until ctx is done.
func (s *Server) Run(ctx context.Context, interval time.Duration) {
	tick := time.NewTicker(interval)
	defer tick.Stop()
	for {
		s.Pass(ctx)
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
	}
}

// Pass counts what the hooks marked, fetches the brake's rules, sends what's spooled, then
// forgets the sessions idle for a day.
func (s *Server) Pass(ctx context.Context) {
	s.init()
	s.count()
	if s.Rules != "" {
		s.fetchRules(ctx)
	}
	if s.Collector != "" {
		s.send(ctx)
	}
	s.sweep()
}

// count logs and clears the hooks' marks: events dropped because the spool was full, and
// hook events wake-hook doesn't handle. Each is a health signal, never dropped silently.
func (s *Server) count() {
	for _, sub := range []string{droppedDir, unknownDir} {
		dir := filepath.Join(s.Dir, sub)
		names, _ := readNames(dir)
		counts := map[string]int{}
		for _, n := range names {
			// <unix nanos>-<what happened>-<random>, as mark names them.
			parts := strings.Split(n, "-")
			if len(parts) == 3 && os.Remove(filepath.Join(dir, n)) == nil {
				counts[parts[1]]++
			}
		}
		for label, n := range counts {
			if sub == droppedDir {
				s.Log.Printf("dropped %d events (%s)", n, label)
			} else {
				s.Log.Printf("%d hook events it doesn't handle (%s)", n, label)
			}
		}
	}
}

// fetchRules fetches each agent's rules from wake-proxy, naming the agent and this
// process as the SDKs do, so the brake can confirm a halt reached this machine.
func (s *Server) fetchRules(ctx context.Context) {
	wants, _ := filepath.Glob(filepath.Join(s.Dir, rulesDir, "*.want"))
	for _, w := range wants {
		if data, err := os.ReadFile(w); err == nil && len(data) > 0 {
			if _, ok := s.agents[string(data)]; !ok {
				s.agents[string(data)] = ""
			}
		}
	}
	for agent := range s.agents {
		err := s.fetch(ctx, agent)
		msg := ""
		if err != nil {
			msg = err.Error()
			if msg != s.agents[agent] {
				s.Log.Printf("couldn't fetch the brake's rules for %s: %v", agent, err)
			}
		} else {
			os.Remove(wantPath(s.Dir, agent))
		}
		s.agents[agent] = msg
	}
}

// fetch fetches one agent's rules from wake-proxy's /v1/memory, where the SDKs fetch theirs,
// given wake-proxy's address.
func (s *Server) fetch(ctx context.Context, agent string) error {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	query := url.Values{"agent": {agent}, "process": {s.process}}
	address := strings.TrimRight(s.Rules, "/") + "/v1/memory?" + query.Encode()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, address, nil)
	if err != nil {
		return err
	}
	resp, err := s.Client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("wake-proxy answered %s", resp.Status)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxRulesBytes+1))
	if err != nil {
		return err
	}
	if len(body) > maxRulesBytes {
		return fmt.Errorf("rules over %d bytes", maxRulesBytes)
	}
	var rules map[string]any
	if err := json.Unmarshal(body, &rules); err != nil {
		return fmt.Errorf("unexpected rules: %v", err)
	}
	if rules == nil {
		return errors.New("unexpected rules: not a JSON object")
	}
	data, _ := json.Marshal(rulesFile{Agent: agent, Fetched: s.opts.Now().UnixNano(), Rules: body})
	return write(rulesPath(s.Dir, agent), data, s.opts.hex)
}

// sweep forgets what entered the agents of each session nothing has entered for a day,
// the longest a flow policy's window can be. It looks every ten minutes.
func (s *Server) sweep() {
	now := s.opts.Now()
	if !s.swept.IsZero() && now.Sub(s.swept) < sweepEvery {
		return
	}
	s.swept = now
	root := filepath.Join(s.Dir, flowDir)
	sessions, _ := readNames(root)
	for _, n := range sessions {
		dir := filepath.Join(root, n)
		var newest time.Time
		filepath.WalkDir(dir, func(_ string, d fs.DirEntry, err error) error {
			if err == nil {
				if info, err := d.Info(); err == nil && info.ModTime().After(newest) {
					newest = info.ModTime()
				}
			}
			return nil
		})
		if now.Sub(newest) > maxWindow*time.Second {
			os.RemoveAll(dir)
		}
	}
}

// send posts the spool to the collector, oldest first, in batches. Sent events are
// removed. While the collector can't take them they wait for the next pass; one it
// refuses outright is set aside in rejected, so it never holds up the rest.
func (s *Server) send(ctx context.Context) {
	dir := filepath.Join(s.Dir, spoolDir)
	names, _ := readNames(dir)
	var spooled []string
	for _, n := range names {
		if _, ok := spooledSize(n); ok {
			spooled = append(spooled, n)
		} else if strings.HasSuffix(n, ".tmp") {
			// Left by a hook that died before renaming its event into place.
			if info, err := os.Stat(filepath.Join(dir, n)); err == nil && time.Since(info.ModTime()) > time.Hour {
				os.Remove(filepath.Join(dir, n))
			}
		}
	}
	sort.Strings(spooled)
	limit := batchEvents
	for len(spooled) > 0 {
		var batch []string
		var spans []json.RawMessage
		size := 0
		for _, n := range spooled {
			if len(batch) == limit || len(batch) > 0 && size > batchBytes {
				break
			}
			batch = append(batch, n)
			data, err := os.ReadFile(filepath.Join(dir, n))
			if err != nil || !json.Valid(data) {
				spans = append(spans, nil)
				continue
			}
			spans, size = append(spans, data), size+len(data)
		}
		var good []json.RawMessage
		for i, span := range spans {
			if span == nil {
				s.reject(batch[i], "unreadable")
			} else {
				good = append(good, span)
			}
		}
		status, err := s.post(ctx, good)
		switch {
		case len(good) == 0:
		case err != nil || status == http.StatusTooManyRequests || status == http.StatusRequestTimeout || status >= 500:
			if err == nil {
				err = fmt.Errorf("the collector answered %d", status)
			}
			s.Log.Printf("couldn't send %d events, retrying later: %v", len(good), err)
			return
		case status >= 300 && len(batch) > 1:
			// Find the event it refuses by sending them one at a time.
			limit = 1
			continue
		case status >= 300:
			s.reject(batch[0], fmt.Sprintf("the collector answered %d", status))
		default:
			for _, n := range batch {
				os.Remove(filepath.Join(dir, n))
			}
		}
		spooled = spooled[len(batch):]
	}
}

func (s *Server) post(ctx context.Context, spans []json.RawMessage) (int, error) {
	if len(spans) == 0 {
		return 0, nil
	}
	body := exportRequest(spans, s.Version)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimSuffix(s.Collector, "/")+"/v1/traces", bytes.NewReader(body))
	if err != nil {
		return 0, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := s.Client.Do(req)
	if err != nil {
		return 0, err
	}
	io.Copy(io.Discard, io.LimitReader(resp.Body, 1<<20))
	resp.Body.Close()
	return resp.StatusCode, nil
}

func (s *Server) reject(n, why string) {
	to := filepath.Join(s.Dir, rejectedDir, n)
	os.MkdirAll(filepath.Dir(to), 0o700)
	if err := os.Rename(filepath.Join(s.Dir, spoolDir, n), to); err != nil {
		if !errors.Is(err, os.ErrNotExist) {
			s.Log.Printf("couldn't set aside an event the collector won't take (%s): %v", why, err)
		}
		return
	}
	s.Log.Printf("set aside an event the collector won't take (%s): %s", why, to)
}
