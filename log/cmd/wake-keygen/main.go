// Command wake-keygen writes a note format key pair: <out>.key (private) and <out>.pub.
// Use one pair for the log's checkpoints and another for the collector's event signatures.
//
//	go run github.com/manikoa/wake/log/cmd/wake-keygen -name spiffe://example.org/wake/collector -out keys/collector
package main

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"

	wakelog "github.com/manikoa/wake/log"
)

func main() {
	name := flag.String("name", "", "signer name, for example a SPIFFE id")
	out := flag.String("out", "", "path prefix for the .key and .pub files")
	keep := flag.Bool("keep-existing", false, "succeed without writing if the key pair already exists, as on a restart")
	flag.Parse()
	if *name == "" || *out == "" {
		flag.Usage()
		os.Exit(2)
	}
	if *keep && exists(*out+".key") && exists(*out+".pub") {
		return
	}
	if err := run(*name, *out); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func exists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

func run(name, out string) error {
	privateKey, publicKey, err := wakelog.GenerateKey(name)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(out), 0o700); err != nil {
		return err
	}
	// O_EXCL: never overwrite an existing key.
	f, err := os.OpenFile(out+".key", os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return err
	}
	if _, err := fmt.Fprintln(f, privateKey); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	return os.WriteFile(out+".pub", []byte(publicKey+"\n"), 0o644)
}
