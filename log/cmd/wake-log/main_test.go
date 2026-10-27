package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	wakelog "github.com/manikoa/wake/log"
	"golang.org/x/mod/sumdb/note"
)

const payload = `{"amount":500,"account":"999-1234"}`

func event(id string) []byte {
	sum := sha256.Sum256([]byte(payload))
	return []byte(`{"attributes":{"wake.event.id":"` + id + `","wake.payload.sha256":"` + hex.EncodeToString(sum[:]) +
		`"},"end_time_unix_nano":2,"span_id":"0102030405060708","start_time_unix_nano":1,"trace_id":"0102030405060708090a0b0c0d0e0f10"}`)
}

// A log with two signed events carrying the same payload: one in its entry, one stored
// next to the tiles.
func writeLog(t *testing.T) (dir string, args []string) {
	t.Helper()
	dir = t.TempDir()
	write := func(name, data string) string {
		path := filepath.Join(dir, name)
		if err := os.WriteFile(path, []byte(data+"\n"), 0o600); err != nil {
			t.Fatal(err)
		}
		return path
	}
	logPrivate, logPublic, err := wakelog.GenerateKey("test-log")
	if err != nil {
		t.Fatal(err)
	}
	private, public, err := note.GenerateKey(rand.Reader, "collector")
	if err != nil {
		t.Fatal(err)
	}
	signer, err := note.NewSigner(private)
	if err != nil {
		t.Fatal(err)
	}
	sign := func(ev []byte) string {
		sig, err := signer.Sign(ev)
		if err != nil {
			t.Fatal(err)
		}
		return base64.StdEncoding.EncodeToString(sig)
	}
	inline, _, err := wakelog.NewEntryWithPayload(event("e1"), payload, sign(event("e1")), "collector", true)
	if err != nil {
		t.Fatal(err)
	}
	stored, err := wakelog.NewEntry(event("e2"), sign(event("e2")), "collector", true)
	if err != nil {
		t.Fatal(err)
	}
	l, err := wakelog.Open(wakelog.Config{Dir: filepath.Join(dir, "log"), KeyFile: write("log.key", logPrivate)})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if err := l.PutPayload(ctx, []byte(payload)); err != nil {
		t.Fatal(err)
	}
	if _, err := l.Add(ctx, [][]byte{inline, stored}); err != nil {
		t.Fatal(err)
	}
	if err := l.Close(ctx); err != nil {
		t.Fatal(err)
	}
	return dir, []string{"verify", "-log", filepath.Join(dir, "log"), "-log-key", write("log.pub", logPublic),
		"-key", write("collector.pub", public), "-entries"}
}

func TestVerifyPrintsEntriesWithTheirPayloads(t *testing.T) {
	_, args := writeLog(t)
	var stdout, stderr bytes.Buffer
	if code := run(args, &stdout, &stderr); code != 0 {
		t.Fatalf("exit %d: %s", code, stderr.String())
	}
	lines := strings.Split(strings.TrimSpace(stdout.String()), "\n")
	if len(lines) != 2 {
		t.Fatalf("%d lines: %s", len(lines), stdout.String())
	}
	for _, line := range lines {
		var entry struct {
			Signed  bool            `json:"signed"`
			Payload json.RawMessage `json:"payload"`
		}
		if err := json.Unmarshal([]byte(line), &entry); err != nil {
			t.Fatal(err)
		}
		if !entry.Signed || string(entry.Payload) != payload {
			t.Errorf("entry = %s", line)
		}
	}
}

func TestVerifyFailsOnATamperedPayload(t *testing.T) {
	dir, args := writeLog(t)
	sum := sha256.Sum256([]byte(payload))
	rel, err := wakelog.PayloadPath(hex.EncodeToString(sum[:]))
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "log", filepath.FromSlash(rel))
	if err := os.WriteFile(path, []byte(`{"amount":5,"account":"999-1234"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	var stdout, stderr bytes.Buffer
	if code := run(args, &stdout, &stderr); code != 1 {
		t.Fatalf("exit %d for a tampered payload: %s", code, stderr.String())
	}
	if !strings.Contains(stdout.String(), `"payload_mismatch":true`) {
		t.Fatalf("no mismatch reported: %s", stdout.String())
	}
}
