// Command wake-log checks a Wake signed log without trusting whoever wrote or serves it:
// the checkpoint's signature, that every entry is in the signed tree, and each event's
// signature. With -entries it also prints every entry as a JSON line, with its payload
// checked against the event's signed digest.
//
//	go run github.com/manikoa/wake/log/cmd/wake-log verify -log ./log -log-key keys/log.pub \
//	    -key keys/collector.pub -bundle identity/bundle.pem
//
// -log is the log's directory, or an http(s) URL it's served from. -key is a note format
// public key for an event signer, such as the collector; -bundle is a PEM trust bundle that
// X.509-SVID signers, such as the proxy, must chain to. Both repeat. It exits 1 if the log
// doesn't hold together, any event's signature doesn't check, or a printed payload doesn't
// match its digest.
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

	wakelog "github.com/manikoa/wake/log"
	"github.com/transparency-dev/tessera/client"
	"golang.org/x/mod/sumdb/note"
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

type files []string

func (f *files) String() string     { return strings.Join(*f, ",") }
func (f *files) Set(v string) error { *f = append(*f, v); return nil }

func run(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 || args[0] != "verify" {
		fmt.Fprintln(stderr, "usage: wake-log verify -log <dir or url> -log-key <file> [-key <file>]... [-bundle <file>]... [-entries]")
		return 2
	}
	flags := flag.NewFlagSet("verify", flag.ContinueOnError)
	flags.SetOutput(stderr)
	location := flags.String("log", "", "the log's directory, or an http(s) URL it's served from")
	logKey := flags.String("log-key", "", "the log's checkpoint public key")
	entries := flags.Bool("entries", false, "print every entry as a JSON line")
	var keys, bundles files
	flags.Var(&keys, "key", "an event signer's public key (repeatable)")
	flags.Var(&bundles, "bundle", "a PEM trust bundle for X.509-SVID signers (repeatable)")
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
	trust := wakelog.Trust{Keys: map[string]note.Verifier{}}
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
	reader, err := open(*location)
	if err != nil {
		fmt.Fprintln(stderr, err)
		return 2
	}

	out := json.NewEncoder(stdout)
	out.SetEscapeHTML(false)
	var each func(uint64, wakelog.Entry, bool)
	var mismatched []uint64
	if *entries {
		each = func(index uint64, e wakelog.Entry, signed bool) {
			line := map[string]any{"index": index, "signed": signed, "signer": e.Signer}
			if ev, err := e.Decode(); err == nil {
				line["event"] = ev
			}
			switch payload, err := e.ReadPayload(reader.ReadPayload); {
			case err == nil && json.Valid(payload):
				line["payload"] = json.RawMessage(payload)
			case err == nil:
				line["payload"] = string(payload)
			case errors.Is(err, wakelog.ErrPayloadMismatch):
				line["payload_mismatch"] = true
				mismatched = append(mismatched, index)
			}
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
	failed := false
	if len(checked.Unsigned) > 0 {
		fmt.Fprintf(stderr, "%d entries have an event signature that doesn't check: %v\n", len(checked.Unsigned), checked.Unsigned)
		failed = true
	}
	if len(mismatched) > 0 {
		fmt.Fprintf(stderr, "%d entries have a payload that doesn't match its signed digest: %v\n", len(mismatched), mismatched)
		failed = true
	}
	if failed {
		return 1
	}
	fmt.Fprintln(stderr, "every event's signature checks")
	return 0
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
