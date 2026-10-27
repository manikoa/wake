// Package wakelog is Wake's self hosted signed log: an append only, hash chained
// tlog-tiles log built on Trillian Tessera, with checkpoints signed by the log's key.
//
// Anyone can check a log with Tessera's fsck tool and the log's public key, without
// trusting Wake:
//
//	go run github.com/transparency-dev/tessera/cmd/fsck@v1.0.4 --storage_url=<log url> --public_key=log.pub
package wakelog

import (
	"context"
	"crypto/rand"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/transparency-dev/tessera"
	"github.com/transparency-dev/tessera/storage/posix"
	"golang.org/x/mod/sumdb/note"
)

// Config says where the log lives and which key signs its checkpoints.
type Config struct {
	// Dir is where the tiles and the checkpoint are written.
	Dir string
	// KeyFile holds the note format private key that signs checkpoints.
	KeyFile string
	// CheckpointInterval is how often a new checkpoint is published. Defaults to one second.
	CheckpointInterval time.Duration
}

// Log appends entries to a signed log on local disk.
type Log struct {
	appender *tessera.Appender
	shutdown func(context.Context) error
	cancel   context.CancelFunc
}

// Open opens the log in cfg.Dir, creating it if needed.
func Open(cfg Config) (*Log, error) {
	key, err := os.ReadFile(cfg.KeyFile)
	if err != nil {
		return nil, fmt.Errorf("read log key: %w", err)
	}
	signer, err := note.NewSigner(strings.TrimSpace(string(key)))
	if err != nil {
		return nil, fmt.Errorf("parse log key: %w", err)
	}
	interval := cfg.CheckpointInterval
	if interval == 0 {
		interval = time.Second
	}

	// Tessera's background work lives as long as the log, not as long as the caller's context.
	ctx, cancel := context.WithCancel(context.Background())
	driver, err := posix.New(ctx, posix.Config{Path: cfg.Dir})
	if err != nil {
		cancel()
		return nil, fmt.Errorf("open log storage: %w", err)
	}
	opts := tessera.NewAppendOptions().
		WithCheckpointSigner(signer).
		WithCheckpointInterval(interval).
		WithBatching(256, 100*time.Millisecond)
	appender, shutdown, _, err := tessera.NewAppender(ctx, driver, opts)
	if err != nil {
		cancel()
		return nil, fmt.Errorf("start log: %w", err)
	}
	return &Log{appender: appender, shutdown: shutdown, cancel: cancel}, nil
}

// Add appends entries and waits until each has a durable index in the log.
func (l *Log) Add(ctx context.Context, entries [][]byte) ([]uint64, error) {
	futures := make([]tessera.IndexFuture, len(entries))
	for i, entry := range entries {
		futures[i] = l.appender.Add(ctx, tessera.NewEntry(entry))
	}
	indexes := make([]uint64, len(entries))
	for i, future := range futures {
		index, err := future()
		if err != nil {
			return nil, fmt.Errorf("append entry: %w", err)
		}
		indexes[i] = index.Index
	}
	return indexes, nil
}

// Close publishes a checkpoint covering every added entry, then stops the log.
func (l *Log) Close(ctx context.Context) error {
	defer l.cancel()
	return l.shutdown(ctx)
}

// GenerateKey returns a new note format key pair. name identifies the signer, for
// example a SPIFFE id.
func GenerateKey(name string) (privateKey, publicKey string, err error) {
	return note.GenerateKey(rand.Reader, name)
}
