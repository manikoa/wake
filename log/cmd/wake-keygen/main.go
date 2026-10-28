// Command wake-keygen writes a note format key pair: <out>.key (private) and <out>.pub.
// Use one pair for the log's checkpoints and another for the collector's event signatures.
//
//	go run github.com/manikoa/wake/log/cmd/wake-keygen -name spiffe://example.org/wake/collector -out keys/collector
//
// The private key is written whole or not at all, and never over an existing one. The
// public key is derived from it, so a crash between the two files leaves a pair that
// -keep-existing completes on the next run.
package main

import (
	"crypto/ed25519"
	"encoding/base64"
	"errors"
	"flag"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	wakelog "github.com/manikoa/wake/log"
	"golang.org/x/mod/sumdb/note"
)

func main() {
	name := flag.String("name", "", "signer name, for example a SPIFFE id")
	out := flag.String("out", "", "path prefix for the .key and .pub files")
	keep := flag.Bool("keep-existing", false, "succeed without writing a new key if one already exists, as on a restart")
	flag.Parse()
	if *name == "" || *out == "" {
		flag.Usage()
		os.Exit(2)
	}
	if err := run(*name, *out, *keep); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run(name, out string, keep bool) error {
	dir := filepath.Dir(out)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	privateKey, _, err := wakelog.GenerateKey(name)
	if err != nil {
		return err
	}
	// Link a synced temporary file into place: never over an existing key, and never half
	// a key.
	err = place(out+".key", privateKey, 0o600, os.Link)
	if errors.Is(err, fs.ErrExist) && keep {
		data, readErr := os.ReadFile(out + ".key")
		if readErr != nil {
			return readErr
		}
		privateKey = strings.TrimSpace(string(data))
	} else if err != nil {
		return err
	}
	publicKey, err := publicKeyOf(privateKey)
	if err != nil {
		return fmt.Errorf("%s.key: %w", out, err)
	}
	if data, err := os.ReadFile(out + ".pub"); err == nil && strings.TrimSpace(string(data)) == publicKey {
		return nil
	}
	// The public key always matches the private one, so replacing it is safe.
	return place(out+".pub", publicKey, 0o644, os.Rename)
}

// place writes data to path through a synced temporary file in the same directory, which
// put moves into place (os.Link refuses an existing path; os.Rename replaces it), then
// syncs the directory so the name survives a crash.
func place(path, data string, mode os.FileMode, put func(from, to string) error) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".tmp-"+filepath.Base(path)+"-")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	_, err = tmp.WriteString(data + "\n")
	if err == nil {
		err = tmp.Chmod(mode)
	}
	if err == nil {
		err = tmp.Sync()
	}
	if closeErr := tmp.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return err
	}
	if err := put(tmp.Name(), path); err != nil {
		return err
	}
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
}

// publicKeyOf derives the public key from a note format Ed25519 private key,
// PRIVATE+KEY+<name>+<hash>+<base64 of 0x01 and the seed>, and checks the pair signs and
// verifies.
func publicKeyOf(privateKey string) (string, error) {
	signer, err := note.NewSigner(privateKey)
	if err != nil {
		return "", err
	}
	// Names can't hold "+", but base64 can.
	fields := strings.SplitN(privateKey, "+", 5)
	key, err := base64.StdEncoding.DecodeString(fields[len(fields)-1])
	if err != nil || len(key) != 1+ed25519.SeedSize || key[0] != 1 {
		return "", errors.New("not an Ed25519 note key")
	}
	publicKey, err := note.NewEd25519VerifierKey(signer.Name(), ed25519.NewKeyFromSeed(key[1:]).Public().(ed25519.PublicKey))
	if err != nil {
		return "", err
	}
	verifier, err := note.NewVerifier(publicKey)
	if err != nil {
		return "", err
	}
	msg := []byte("wake-keygen\n")
	sig, err := signer.Sign(msg)
	if err != nil || !verifier.Verify(msg, sig) || verifier.KeyHash() != signer.KeyHash() {
		return "", errors.New("derived public key doesn't match")
	}
	return publicKey, nil
}
