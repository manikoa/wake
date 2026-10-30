// Command wake-log checks a Wake signed log without trusting whoever wrote or serves it:
// the checkpoint's signature, that every entry is in the signed tree, and each event's
// signature and payload. With -entries it also prints every entry as a JSON line.
//
//	go run github.com/usewake/wake/log/cmd/wake-log verify -log ./log -log-key keys/log.pub \
//	    -key keys/collector.pub -bundle identity/bundle.pem -signer proxy=spiffe://wake.local/proxy
//
// -log is the log's directory, or an http(s) URL it's served from. -key is a note format
// public key for an event signer, such as the collector; -bundle is a PEM trust bundle that
// X.509-SVID signers, such as the proxy, must chain to; -signer says who may sign a capture,
// as <capture>=<SPIFFE ID>, or a path prefix ending in "/". A bundle vouches for every
// workload in its trust domain, agents included, so an SVID signature only counts for a
// capture -signer lists it under. All three repeat.
//
// Each entry is compared with the collector's own check when it appended it:
//
//   - logged valid and still checks: fine.
//   - logged invalid and still fails: evidence the collector recorded, such as an event
//     with a bad signature. Reported, but the log is fine.
//   - logged valid but fails now: changed after it was logged, or checked here with
//     different trust from the collector's.
//   - logged invalid but checks now: checked here with wider trust than the collector's.
//
// It exits 1 if the log doesn't hold together or any entry is in the last two classes,
// including one whose payload is missing or doesn't match its signed digest.
package main

import (
	"context"
	"crypto/x509"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/transparency-dev/tessera/client"
	wakelog "github.com/usewake/wake/log"
	"golang.org/x/mod/sumdb/note"
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

type files []string

func (f *files) String() string     { return strings.Join(*f, ",") }
func (f *files) Set(v string) error { *f = append(*f, v); return nil }

// finding is an entry whose check now differs from what was logged, or that fails.
type finding struct {
	index   uint64
	problem error
}

func run(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 || args[0] != "verify" {
		fmt.Fprintln(stderr, "usage: wake-log verify -log <dir or url> -log-key <file> [-key <file>]... [-bundle <file>]... [-signer <capture>=<id>]... [-entries]")
		return 2
	}
	flags := flag.NewFlagSet("verify", flag.ContinueOnError)
	flags.SetOutput(stderr)
	location := flags.String("log", "", "the log's directory, or an http(s) URL it's served from")
	logKey := flags.String("log-key", "", "the log's checkpoint public key")
	entries := flags.Bool("entries", false, "print every entry as a JSON line")
	var keys, bundles, signers files
	flags.Var(&keys, "key", "an event signer's public key (repeatable)")
	flags.Var(&bundles, "bundle", "a PEM trust bundle for X.509-SVID signers (repeatable)")
	flags.Var(&signers, "signer", "who may sign a capture, as <capture>=<SPIFFE ID or prefix ending in />, such as proxy=spiffe://wake.local/proxy (repeatable)")
	if err := flags.Parse(args[1:]); err != nil {
		return 2
	}
	if *location == "" || *logKey == "" {
		flags.Usage()
		return 2
	}

	verifier, err := readVerifier(*logKey)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 2
	}
	trust := wakelog.Trust{Keys: map[string]note.Verifier{}, Signers: wakelog.Signers{}}
	for _, k := range keys {
		v, err := readVerifier(k)
		if err != nil {
			fmt.Fprintln(stderr, err)
			return 2
		}
		trust.Keys[v.Name()] = v
	}
	if len(bundles) > 0 {
		trust.Bundles = x509.NewCertPool()
		for _, b := range bundles {
			data, err := os.ReadFile(b)
			if err != nil || !trust.Bundles.AppendCertsFromPEM(data) {
				fmt.Fprintf(stderr, "read trust bundle %s: no certificates\n", b)
				return 2
			}
		}
	}
	for _, s := range signers {
		capture, id, ok := strings.Cut(s, "=")
		if !ok || capture == "" || id == "" {
			fmt.Fprintf(stderr, "-signer %q: want <capture>=<id>, such as proxy=spiffe://wake.local/proxy\n", s)
			return 2
		}
		trust.Signers[capture] = append(trust.Signers[capture], id)
	}
	if err := trust.Signers.Validate(); err != nil {
		fmt.Fprintf(stderr, "-signer: %v\n", err)
		return 2
	}
	if len(bundles) > 0 && len(signers) == 0 {
		fmt.Fprintln(stderr, "-bundle needs -signer: a bundle vouches for every workload in its trust domain, so say who may sign each capture, such as -signer proxy=spiffe://wake.local/proxy")
		return 2
	}
	reader, err := open(*location)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 2
	}

	out := json.NewEncoder(stdout)
	out.SetEscapeHTML(false)
	var tampered, invalid, unexpected []finding
	each := func(index uint64, e wakelog.Entry, signed bool) {
		line := map[string]any{"index": index, "signed": signed, "valid": e.Valid, "signer": e.Signer}
		var problem error
		switch {
		case len(e.Event) == 0:
			problem = errors.New("not a Wake log entry")
		case !signed:
			problem = e.CheckSignature(trust)
		}
		if ev, err := e.Decode(); err == nil {
			line["event"] = ev
		}
		switch payload, err := e.ReadPayload(reader.ReadPayload); {
		case err == nil && json.Valid(payload):
			line["payload"] = json.RawMessage(payload)
		case err == nil:
			line["payload"] = string(payload)
		case errors.Is(err, wakelog.ErrNoPayload):
		default:
			if errors.Is(err, wakelog.ErrPayloadMismatch) {
				line["payload_mismatch"] = true
			}
			if problem == nil {
				problem = fmt.Errorf("payload: %w", err)
			}
		}
		switch {
		case problem != nil && (e.Valid || len(e.Event) == 0):
			tampered = append(tampered, finding{index, problem})
		case problem != nil:
			invalid = append(invalid, finding{index, problem})
		case !e.Valid:
			unexpected = append(unexpected, finding{index, nil})
		}
		if problem != nil {
			line["problem"] = problem.Error()
		}
		if *entries {
			_ = out.Encode(line)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()
	checked, err := wakelog.Verify(ctx, reader, verifier, trust, each)
	if err != nil {
		fmt.Fprintf(stderr, "the log doesn't verify: %v\n", err)
		return 1
	}
	fmt.Fprintf(stderr, "%s: %d entries, all in the signed tree\n", checked.Origin, checked.Size)
	if len(invalid) > 0 {
		report(stderr, invalid, "%d entries were logged invalid and still don't check. The collector recorded them as evidence:")
	}
	if len(tampered) > 0 {
		report(stderr, tampered, "%d entries were logged valid but don't check now. They changed after they were logged, or this check trusts differently from the collector:")
	}
	if len(unexpected) > 0 {
		report(stderr, unexpected, "%d entries were logged invalid but check now. This check trusts more than the collector did:")
	}
	if len(tampered) > 0 || len(unexpected) > 0 {
		return 1
	}
	fmt.Fprintln(stderr, "every entry checks as the collector logged it")
	return 0
}

// report prints a class of findings, up to ten of them.
func report(w io.Writer, findings []finding, heading string) {
	fmt.Fprintf(w, heading+"\n", len(findings))
	for i, f := range findings {
		if i == 10 {
			fmt.Fprintf(w, "  and %d more\n", len(findings)-i)
			break
		}
		if f.problem == nil {
			fmt.Fprintf(w, "  entry %d\n", f.index)
		} else {
			fmt.Fprintf(w, "  entry %d: %v\n", f.index, f.problem)
		}
	}
}

func readVerifier(path string) (note.Verifier, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	v, err := note.NewVerifier(strings.TrimSpace(string(data)))
	if err != nil {
		return nil, fmt.Errorf("parse public key %s: %w", path, err)
	}
	return v, nil
}

// source reads a log's tiles and the payloads stored next to them.
type source interface {
	wakelog.Reader
	ReadPayload(path string) ([]byte, error)
}

type dirSource struct{ wakelog.DirReader }

func (d dirSource) ReadPayload(path string) ([]byte, error) {
	return os.ReadFile(filepath.Join(string(d.DirReader), filepath.FromSlash(path)))
}

type httpSource struct {
	*client.HTTPFetcher
	root *url.URL
	http *http.Client
}

func (h httpSource) ReadPayload(path string) ([]byte, error) {
	resp, err := h.http.Get(h.root.JoinPath(path).String())
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		return nil, os.ErrNotExist
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("GET %s: %s", path, resp.Status)
	}
	return io.ReadAll(io.LimitReader(resp.Body, 64<<20))
}

func open(location string) (source, error) {
	if strings.HasPrefix(location, "http://") || strings.HasPrefix(location, "https://") {
		u, err := url.Parse(strings.TrimSuffix(location, "/") + "/")
		if err != nil {
			return nil, err
		}
		c := &http.Client{Timeout: time.Minute}
		f, err := client.NewHTTPFetcher(u, c)
		if err != nil {
			return nil, err
		}
		return httpSource{f, u, c}, nil
	}
	if _, err := os.Stat(location); err != nil {
		return nil, err
	}
	return dirSource{wakelog.DirReader(location)}, nil
}
