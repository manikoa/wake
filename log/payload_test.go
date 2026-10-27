package wakelog

import (
	"context"
	"encoding/base64"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// entryWithPayload signs event and encodes an entry for it carrying payload.
func entryWithPayload(t *testing.T, event, payload string) (entry Entry, inline bool) {
	t.Helper()
	signer, _ := testSigner(t, "collector")
	sig, err := signer.Sign([]byte(event))
	if err != nil {
		t.Fatal(err)
	}
	data, inline, err := NewEntryWithPayload([]byte(event), payload, base64.StdEncoding.EncodeToString(sig), "collector", true)
	if err != nil {
		t.Fatal(err)
	}
	if len(data) > MaxEntrySize {
		t.Fatalf("entry is %d bytes, over the log's limit", len(data))
	}
	entry, err = ParseEntry(data)
	if err != nil {
		t.Fatal(err)
	}
	return entry, inline
}

func openTestLog(t *testing.T) (*Log, string) {
	t.Helper()
	dir := t.TempDir()
	privateKey, _, err := GenerateKey("test-log")
	if err != nil {
		t.Fatal(err)
	}
	keyFile := filepath.Join(dir, "log.key")
	if err := os.WriteFile(keyFile, []byte(privateKey), 0o600); err != nil {
		t.Fatal(err)
	}
	logDir := filepath.Join(dir, "log")
	l, err := Open(Config{Dir: logDir, KeyFile: keyFile})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { l.Close(context.Background()) })
	return l, logDir
}

// readFrom fetches payload files from a log on local disk.
func readFrom(root string) func(string) ([]byte, error) {
	return func(path string) ([]byte, error) {
		return os.ReadFile(filepath.Join(root, filepath.FromSlash(path)))
	}
}

func noFetch(t *testing.T) func(string) ([]byte, error) {
	return func(path string) ([]byte, error) {
		t.Fatalf("fetched %s for an inline payload", path)
		return nil, nil
	}
}

func TestSmallPayloadGoesInline(t *testing.T) {
	entry, inline := entryWithPayload(t, signedEvent, testPayload)
	if !inline || entry.Payload == nil || *entry.Payload != testPayload {
		t.Fatalf("inline = %v, payload = %v", inline, entry.Payload)
	}
	payload, err := entry.ReadPayload(noFetch(t))
	if err != nil || string(payload) != testPayload {
		t.Fatalf("ReadPayload = %q, %v", payload, err)
	}
}

func TestLargePayloadIsStoredNextToTheLog(t *testing.T) {
	l, root := openTestLog(t)
	payload := `{"output":"` + strings.Repeat("Quarterly numbers attached. ", 4000) + `"}`
	entry, inline := entryWithPayload(t, eventFor(payload), payload)
	if inline || entry.Payload != nil {
		t.Fatalf("a %d byte payload went inline", len(payload))
	}
	if err := l.PutPayload(context.Background(), []byte(payload)); err != nil {
		t.Fatal(err)
	}
	// Storing it again, as a retry does, is fine.
	if err := l.PutPayload(context.Background(), []byte(payload)); err != nil {
		t.Fatal(err)
	}
	got, err := entry.ReadPayload(readFrom(root))
	if err != nil || string(got) != payload {
		t.Fatalf("ReadPayload = %d bytes, %v", len(got), err)
	}
}

func TestPayloadInlineUpToTheLimit(t *testing.T) {
	// Find the payload length whose entry is exactly MaxEntrySize.
	probe, _, err := NewEntryWithPayload([]byte(signedEvent), "", "c2lnbmF0dXJl", "collector", true)
	if err != nil {
		t.Fatal(err)
	}
	fits := strings.Repeat("a", MaxEntrySize-len(probe))
	for _, tc := range []struct {
		payload string
		inline  bool
	}{
		{fits, true},
		{fits + "a", false},
	} {
		data, inline, err := NewEntryWithPayload([]byte(signedEvent), tc.payload, "c2lnbmF0dXJl", "collector", true)
		if err != nil {
			t.Fatal(err)
		}
		if inline != tc.inline || len(data) > MaxEntrySize {
			t.Errorf("%d byte payload: inline = %v, entry = %d bytes", len(tc.payload), inline, len(data))
		}
	}
}

func TestPayloadThatIsNotUTF8IsStoredExactly(t *testing.T) {
	l, root := openTestLog(t)
	payload := "caf\xe9"
	entry, inline := entryWithPayload(t, eventFor(payload), payload)
	if inline {
		t.Fatal("invalid UTF-8 went inline, where JSON would rewrite it")
	}
	if err := l.PutPayload(context.Background(), []byte(payload)); err != nil {
		t.Fatal(err)
	}
	got, err := entry.ReadPayload(readFrom(root))
	if err != nil || string(got) != payload {
		t.Fatalf("ReadPayload = %q, %v", got, err)
	}
}

func TestReadPayloadRejectsTampering(t *testing.T) {
	entry, _ := entryWithPayload(t, signedEvent, testPayload)
	changed := `{"output":"pay $1"}`
	entry.Payload = &changed
	if _, err := entry.ReadPayload(noFetch(t)); !errors.Is(err, ErrPayloadMismatch) {
		t.Errorf("tampered inline payload: %v", err)
	}

	l, root := openTestLog(t)
	payload := strings.Repeat("x", MaxEntrySize)
	stored, _ := entryWithPayload(t, eventFor(payload), payload)
	if err := l.PutPayload(context.Background(), []byte(payload)); err != nil {
		t.Fatal(err)
	}
	ev, err := stored.Decode()
	if err != nil {
		t.Fatal(err)
	}
	path, err := PayloadPath(ev.String(PayloadSHA256))
	if err != nil {
		t.Fatal(err)
	}
	file := filepath.Join(root, filepath.FromSlash(path))
	if err := os.WriteFile(file, []byte(strings.Repeat("y", MaxEntrySize)), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := stored.ReadPayload(readFrom(root)); !errors.Is(err, ErrPayloadMismatch) {
		t.Errorf("tampered payload file: %v", err)
	}

	if err := os.Remove(file); err != nil {
		t.Fatal(err)
	}
	if _, err := stored.ReadPayload(readFrom(root)); err == nil {
		t.Error("missing payload file read without an error")
	}
}

func TestReadPayloadWithoutDigest(t *testing.T) {
	event := `{"attributes":{"wake.event.kind":"tool_call"},"end_time_unix_nano":2,"span_id":"01","start_time_unix_nano":1,"trace_id":"02"}`
	data, err := NewEntry([]byte(event), "c2ln", "collector", true)
	if err != nil {
		t.Fatal(err)
	}
	entry, err := ParseEntry(data)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := entry.ReadPayload(noFetch(t)); !errors.Is(err, ErrNoPayload) {
		t.Fatalf("ReadPayload = %v, want ErrNoPayload", err)
	}
}

func TestPayloadPathRefusesAnythingButADigest(t *testing.T) {
	good := strings.Repeat("ab", 32)
	if path, err := PayloadPath(good); err != nil || path != "payload/sha256/"+good {
		t.Fatalf("PayloadPath = %q, %v", path, err)
	}
	for _, digest := range []string{"", "ab", strings.Repeat("AB", 32), "../../" + strings.Repeat("a", 58), strings.Repeat("g", 64)} {
		if _, err := PayloadPath(digest); err == nil {
			t.Errorf("PayloadPath(%q) succeeded", digest)
		}
	}
}

func TestEntryTooLargeWithoutItsPayload(t *testing.T) {
	event := `{"attributes":{"wake.parents":["` + strings.Repeat("e", MaxEntrySize) + `"]},"end_time_unix_nano":2,"span_id":"01","start_time_unix_nano":1,"trace_id":"02"}`
	if _, err := NewEntry([]byte(event), "c2ln", "collector", true); !errors.Is(err, ErrEntryTooLarge) {
		t.Errorf("NewEntry = %v, want ErrEntryTooLarge", err)
	}
	if _, _, err := NewEntryWithPayload([]byte(event), "{}", "c2ln", "collector", true); !errors.Is(err, ErrEntryTooLarge) {
		t.Errorf("NewEntryWithPayload = %v, want ErrEntryTooLarge", err)
	}
}
