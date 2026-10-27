package wakelog

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/mod/sumdb/note"
)

func TestEntriesAreCoveredBySignedCheckpoint(t *testing.T) {
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
	indexes, err := l.Add(ctx, [][]byte{[]byte("a"), []byte("b"), []byte("c")})
	if err != nil {
		t.Fatal(err)
	}
	if err := l.Close(ctx); err != nil {
		t.Fatal(err)
	}
	if len(indexes) != 3 || indexes[0] != 0 || indexes[2] != 2 {
		t.Fatalf("indexes = %v, want [0 1 2]", indexes)
	}

	checkpoint, err := os.ReadFile(filepath.Join(logDir, "checkpoint"))
	if err != nil {
		t.Fatal(err)
	}
	verifier, err := note.NewVerifier(publicKey)
	if err != nil {
		t.Fatal(err)
	}
	signed, err := note.Open(checkpoint, note.VerifierList(verifier))
	if err != nil {
		t.Fatalf("checkpoint signature doesn't verify: %v", err)
	}
	// A checkpoint is the origin, the tree size and the root hash, one per line.
	lines := strings.Split(signed.Text, "\n")
	if lines[1] != "3" {
		t.Fatalf("checkpoint tree size = %q, want 3", lines[1])
	}
}

func TestOpenFailsWithoutKey(t *testing.T) {
	if _, err := Open(Config{Dir: t.TempDir(), KeyFile: "missing.key"}); err == nil {
		t.Fatal("Open succeeded without a key")
	}
}
