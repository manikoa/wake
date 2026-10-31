package wakelog

import (
	"context"
	"errors"
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

// grownLog writes a log with one set of entries, keeps its checkpoint, adds more, and
// returns the directory, the two checkpoints in the order they were signed, the key's
// private half, and the checkpoint key.
func grownLog(t *testing.T, first, more [][]byte) (dir string, older, newer []byte, keyFile string, logKey note.Verifier) {
	t.Helper()
	root := t.TempDir()
	privateKey, publicKey, err := GenerateKey("test-log")
	if err != nil {
		t.Fatal(err)
	}
	keyFile = filepath.Join(root, "log.key")
	if err := os.WriteFile(keyFile, []byte(privateKey), 0o600); err != nil {
		t.Fatal(err)
	}
	if logKey, err = note.NewVerifier(publicKey); err != nil {
		t.Fatal(err)
	}
	dir = filepath.Join(root, "log")
	appendTo(t, dir, keyFile, first)
	older = readCheckpoint(t, dir)
	appendTo(t, dir, keyFile, more)
	return dir, older, readCheckpoint(t, dir), keyFile, logKey
}

func appendTo(t *testing.T, dir, keyFile string, entries [][]byte) {
	t.Helper()
	l, err := Open(Config{Dir: dir, KeyFile: keyFile})
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
}

func readCheckpoint(t *testing.T, dir string) []byte {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(dir, layout.CheckpointPath))
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func entriesFor(t *testing.T, signer note.Signer, payloads ...string) [][]byte {
	t.Helper()
	var out [][]byte
	for _, p := range payloads {
		out = append(out, signedEntry(t, signer, eventFor(p)))
	}
	return out
}

func TestVerifyHoldsALogToAnEarlierCheckpoint(t *testing.T) {
	signer, trusted := testSigner(t, "collector")
	dir, older, newer, _, logKey := grownLog(t, entriesFor(t, signer, "one", "two"), entriesFor(t, signer, "three", "four"))
	ctx, trust := context.Background(), Trust{Keys: trusted}

	checked, err := Verify(ctx, DirReader(dir), logKey, trust, nil, older)
	if err != nil {
		t.Fatalf("a log that only grew: %v", err)
	}
	if checked.Size != 4 || string(checked.Checkpoint) != string(newer) {
		t.Fatalf("checked = %+v", checked)
	}
	if _, err := Verify(ctx, DirReader(dir), logKey, trust, nil, older, newer); err != nil {
		t.Fatalf("the checkpoint it's at: %v", err)
	}
	empty, err := Verify(ctx, DirReader(dir), logKey, trust, nil)
	if err != nil || empty.Size != 4 {
		t.Fatalf("no earlier checkpoint: %+v, %v", empty, err)
	}
}

// Whoever can write the log's storage puts back an older checkpoint. Its signature is good
// and the entries under it hash to its root, so the log looks whole with the rest gone.
func TestVerifySeesALogCutBackToAnOlderCheckpoint(t *testing.T) {
	signer, trusted := testSigner(t, "collector")
	dir, older, newer, _, logKey := grownLog(t, entriesFor(t, signer, "one", "two"), entriesFor(t, signer, "three", "four"))
	ctx, trust := context.Background(), Trust{Keys: trusted}
	if err := os.WriteFile(filepath.Join(dir, layout.CheckpointPath), older, 0o644); err != nil {
		t.Fatal(err)
	}

	cut, err := Verify(ctx, DirReader(dir), logKey, trust, nil)
	if err != nil || cut.Size != 2 {
		t.Fatalf("with nothing to compare, a log cut back passes as a shorter log: %+v, %v", cut, err)
	}
	if _, err := Verify(ctx, DirReader(dir), logKey, trust, nil, newer); !errors.Is(err, ErrCutBack) {
		t.Fatalf("held to the checkpoint it had reached: %v", err)
	}
}

// Whoever holds the log's key can sign a different history, shorter, the same length or
// longer. Only a checkpoint kept from before tells it from the real one.
func TestVerifySeesALogRewrittenWithItsOwnKey(t *testing.T) {
	signer, trusted := testSigner(t, "collector")
	honest, older, newer, keyFile, logKey := grownLog(t, entriesFor(t, signer, "one", "two"), entriesFor(t, signer, "three", "four"))
	ctx, trust := context.Background(), Trust{Keys: trusted}

	for name, payloads := range map[string][]string{
		"changed under the older checkpoint":      {"one", "tw0", "three", "four"},
		"the same to it, then changed":            {"one", "two", "thr3e", "four"},
		"changed, then grown past the checkpoint": {"one", "two", "three", "fou4", "five", "six"},
	} {
		t.Run(name, func(t *testing.T) {
			rewritten := filepath.Join(t.TempDir(), "log")
			appendTo(t, rewritten, keyFile, entriesFor(t, signer, payloads...))
			if _, err := Verify(ctx, DirReader(rewritten), logKey, trust, nil); err != nil {
				t.Fatalf("every checkpoint is genuinely signed, so alone it passes: %v", err)
			}
			if _, err := Verify(ctx, DirReader(rewritten), logKey, trust, nil, newer); !errors.Is(err, ErrRewritten) {
				t.Fatalf("held to the checkpoint kept: %v", err)
			}
		})
	}

	// A change under the older checkpoint is seen from that one too.
	rewritten := filepath.Join(t.TempDir(), "log")
	appendTo(t, rewritten, keyFile, entriesFor(t, signer, "0ne", "two", "three", "four"))
	if _, err := Verify(ctx, DirReader(rewritten), logKey, trust, nil, older); !errors.Is(err, ErrRewritten) {
		t.Fatalf("held to the older checkpoint: %v", err)
	}
	// And a change after it isn't: the older checkpoint vouches only for what it covers.
	rewritten = filepath.Join(t.TempDir(), "log")
	appendTo(t, rewritten, keyFile, entriesFor(t, signer, "one", "two", "thr3e", "four"))
	if _, err := Verify(ctx, DirReader(rewritten), logKey, trust, nil, older); err != nil {
		t.Fatalf("the part the older checkpoint covers is the same: %v", err)
	}
	if _, err := Verify(ctx, DirReader(honest), logKey, trust, nil, newer); err != nil {
		t.Fatalf("the honest log: %v", err)
	}
}

func TestAnEarlierCheckpointMustBeThisLogsOwn(t *testing.T) {
	signer, trusted := testSigner(t, "collector")
	dir, older, _, keyFile, logKey := grownLog(t, entriesFor(t, signer, "one", "two"), entriesFor(t, signer, "three"))
	ctx, trust := context.Background(), Trust{Keys: trusted}

	other, _ := writeLog(t, entriesFor(t, signer, "one", "two"))
	for name, earlier := range map[string][]byte{
		"another key's":               readCheckpoint(t, other),
		"altered after it was signed": []byte(strings.Replace(string(older), "2\n", "3\n", 1)),
		"not a checkpoint":            []byte("hello\n"),
		"nothing":                     nil,
	} {
		if _, err := Verify(ctx, DirReader(dir), logKey, trust, nil, earlier); err == nil {
			t.Errorf("%s verified", name)
		}
	}

	if _, err := Verify(ctx, DirReader(dir), logKey, trust, nil, older, older); err != nil {
		t.Fatalf("the same checkpoint twice: %v", err)
	}
	// Two checkpoints one key signed for the same size can't both be true of any log.
	forked := filepath.Join(t.TempDir(), "log")
	appendTo(t, forked, keyFile, entriesFor(t, signer, "one", "tw0"))
	if _, err := Verify(ctx, DirReader(dir), logKey, trust, nil, older, readCheckpoint(t, forked)); !errors.Is(err, ErrRewritten) {
		t.Fatalf("a fork between the two: %v", err)
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
