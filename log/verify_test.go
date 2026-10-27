package wakelog

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/transparency-dev/tessera/api/layout"
	"golang.org/x/mod/sumdb/note"
)

// writeLog writes these entries to a new log and returns its directory and checkpoint key.
func writeLog(t *testing.T, entries [][]byte) (string, note.Verifier) {
	t.Helper()
	dir := t.TempDir()
	privateKey, publicKey, err := GenerateKey("test-log")
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
	ctx := context.Background()
	if _, err := l.Add(ctx, entries); err != nil {
		t.Fatal(err)
	}
	if err := l.Close(ctx); err != nil {
		t.Fatal(err)
	}
	verifier, err := note.NewVerifier(publicKey)
	if err != nil {
		t.Fatal(err)
	}
	return logDir, verifier
}

func TestVerifyChecksTheTreeAndEverySignature(t *testing.T) {
	signer, trusted := testSigner(t, "collector")
	other, _ := testSigner(t, "collector")
	event := eventFor("hello, world")
	dir, logKey := writeLog(t, [][]byte{
		signedEntry(t, signer, event),
		signedEntry(t, other, event), // the right name, the wrong key
		[]byte("not an entry"),
		signedEntry(t, signer, eventFor("hello again")), // the same entry twice would land once
	})

	var seen []uint64
	checked, err := Verify(context.Background(), DirReader(dir), logKey, Trust{Keys: trusted}, func(i uint64, e Entry, signed bool) {
		seen = append(seen, i)
		if signed && e.Signer != "collector" {
			t.Errorf("entry %d signed by %q", i, e.Signer)
		}
	})
	if err != nil {
		t.Fatal(err)
	}
	if checked.Size != 4 || checked.Origin != "test-log" || len(seen) != 4 {
		t.Fatalf("checked = %+v, saw %v", checked, seen)
	}
	if got := checked.Unsigned; len(got) != 2 || got[0] != 1 || got[1] != 2 {
		t.Fatalf("unsigned = %v, want [1 2]", got)
	}
}

func TestVerifyRejectsATamperedLog(t *testing.T) {
	signer, trusted := testSigner(t, "collector")
	dir, logKey := writeLog(t, [][]byte{signedEntry(t, signer, eventFor("a payload"))})

	// Someone with write access to the bucket edits an entry in place.
	bundle := filepath.Join(dir, layout.EntriesPath(0, 1))
	data, err := os.ReadFile(bundle)
	if err != nil {
		t.Fatal(err)
	}
	tampered := strings.Replace(string(data), "tool_call", "tool_cal!", 1)
	if tampered == string(data) {
		t.Fatal("nothing to tamper with")
	}
	if err := os.WriteFile(bundle, []byte(tampered), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := Verify(context.Background(), DirReader(dir), logKey, Trust{Keys: trusted}, nil); err == nil {
		t.Fatal("a tampered log verified")
	}

	// And a log checked against the wrong key doesn't verify either.
	_, wrongKey := writeLog(t, nil)
	if _, err := Verify(context.Background(), DirReader(dir), wrongKey, Trust{Keys: trusted}, nil); err == nil {
		t.Fatal("verified against another log's key")
	}
}

// A reader holding a checkpoint asks for the partial bundle it covers. Once the bundle
// fills, Tessera deletes the partial one, and the full bundle starts with the same entries.
func TestVerifyFallsBackToTheFullBundle(t *testing.T) {
	signer, trusted := testSigner(t, "collector")
	dir, logKey := writeLog(t, [][]byte{
		signedEntry(t, signer, eventFor("one")),
		signedEntry(t, signer, eventFor("two")),
		signedEntry(t, signer, eventFor("three")),
	})
	partial := filepath.Join(dir, layout.EntriesPath(0, 3))
	if err := os.Rename(partial, filepath.Join(dir, layout.EntriesPath(0, 0))); err != nil {
		t.Fatal(err)
	}
	checked, err := Verify(context.Background(), DirReader(dir), logKey, Trust{Keys: trusted}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if checked.Size != 3 || len(checked.Unsigned) != 0 {
		t.Fatalf("checked = %+v", checked)
	}
}
