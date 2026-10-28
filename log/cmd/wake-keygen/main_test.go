package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/mod/sumdb/note"
)

func readPair(t *testing.T, out string) (note.Signer, note.Verifier) {
	t.Helper()
	private, err := os.ReadFile(out + ".key")
	if err != nil {
		t.Fatal(err)
	}
	public, err := os.ReadFile(out + ".pub")
	if err != nil {
		t.Fatal(err)
	}
	signer, err := note.NewSigner(strings.TrimSpace(string(private)))
	if err != nil {
		t.Fatal(err)
	}
	verifier, err := note.NewVerifier(strings.TrimSpace(string(public)))
	if err != nil {
		t.Fatal(err)
	}
	sig, err := signer.Sign([]byte("x"))
	if err != nil || !verifier.Verify([]byte("x"), sig) {
		t.Fatal("the key pair doesn't match")
	}
	return signer, verifier
}

func TestWritesAMatchingPairAndNeverReplacesTheKey(t *testing.T) {
	out := filepath.Join(t.TempDir(), "keys", "collector")
	if err := run("wake-collector", out, false); err != nil {
		t.Fatal(err)
	}
	signer, _ := readPair(t, out)
	if info, err := os.Stat(out + ".key"); err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("private key mode: %v, %v", info.Mode(), err)
	}
	if err := run("wake-collector", out, false); err == nil {
		t.Fatal("wrote over an existing key")
	}
	if err := run("wake-collector", out, true); err != nil {
		t.Fatal(err)
	}
	if again, _ := readPair(t, out); again.KeyHash() != signer.KeyHash() {
		t.Fatal("-keep-existing replaced the key")
	}
	if entries, _ := os.ReadDir(filepath.Dir(out)); len(entries) != 2 {
		t.Fatalf("left behind: %v", entries)
	}
}

// Key data is base64, which can hold "+", the note format's separator.
func TestDerivesThePublicKeyOfAnyKey(t *testing.T) {
	for range 20 { // about half of all keys have a "+"
		out := filepath.Join(t.TempDir(), "k")
		if err := run("wake-log", out, false); err != nil {
			t.Fatal(err)
		}
		if err := os.Remove(out + ".pub"); err != nil {
			t.Fatal(err)
		}
		if err := run("wake-log", out, true); err != nil {
			t.Fatal(err)
		}
		readPair(t, out)
	}
}

// A crash after the private key landed and before the public key did used to leave a key
// that -keep-existing refused forever, and the collector without a public key to publish.
func TestKeepExistingCompletesAPairCutShort(t *testing.T) {
	out := filepath.Join(t.TempDir(), "log")
	if err := run("wake-log", out, false); err != nil {
		t.Fatal(err)
	}
	signer, _ := readPair(t, out)
	if err := os.Remove(out + ".pub"); err != nil {
		t.Fatal(err)
	}
	if err := run("wake-log", out, true); err != nil {
		t.Fatal(err)
	}
	if again, _ := readPair(t, out); again.KeyHash() != signer.KeyHash() {
		t.Fatal("the private key changed")
	}
	// A public key left from another key is replaced by the one that matches.
	if err := os.WriteFile(out+".pub", []byte("stale\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := run("wake-log", out, true); err != nil {
		t.Fatal(err)
	}
	readPair(t, out)
}
