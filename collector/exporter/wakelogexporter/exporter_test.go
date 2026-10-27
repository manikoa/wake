package wakelogexporter

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/open-telemetry/opentelemetry-collector-contrib/extension/storage/filestorage"
	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/component/componenttest"
	"go.opentelemetry.io/collector/exporter"
	"go.opentelemetry.io/collector/exporter/exportertest"
	"go.opentelemetry.io/collector/extension/extensiontest"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.uber.org/zap/zaptest"

	"github.com/manikoa/wake/collector/event"
	wakelog "github.com/manikoa/wake/log"
)

// testConfig returns the default config for a fresh log in dir.
func testConfig(t *testing.T, dir string) *Config {
	t.Helper()
	private, _, err := wakelog.GenerateKey("test-log")
	if err != nil {
		t.Fatal(err)
	}
	keyFile := filepath.Join(dir, "log.key")
	if err := os.WriteFile(keyFile, []byte(private), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg := createDefaultConfig().(*Config)
	cfg.Dir = filepath.Join(dir, "log")
	cfg.KeyFile = keyFile
	cfg.RetryConfig.InitialInterval = 10 * time.Millisecond
	cfg.RetryConfig.MaxInterval = 50 * time.Millisecond
	return cfg
}

// events returns traces holding n events and one structure span.
func events(n int) ptrace.Traces {
	td := ptrace.NewTraces()
	spans := td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty().Spans()
	spans.AppendEmpty() // a structure span
	for range n {
		spans.AppendEmpty().Attributes().PutStr(event.Kind, "model_call")
	}
	return td
}

func logSize(t *testing.T, cfg *Config) string {
	t.Helper()
	checkpoint, err := os.ReadFile(filepath.Join(cfg.Dir, "checkpoint"))
	if err != nil {
		t.Fatal(err)
	}
	return strings.Split(string(checkpoint), "\n")[1]
}

// flakyLog wraps the real log. Until recover is called, Add either fails or waits.
type flakyLog struct {
	appender
	fail     bool
	healthy  chan struct{}
	attempts atomic.Int64
	added    atomic.Int64
}

func newFlakyLog(fail bool) *flakyLog {
	return &flakyLog{fail: fail, healthy: make(chan struct{})}
}

func (f *flakyLog) open(cfg wakelog.Config) (appender, error) {
	l, err := wakelog.Open(cfg)
	f.appender = l
	return f, err
}

func (f *flakyLog) recover() { close(f.healthy) }

func (f *flakyLog) Add(ctx context.Context, entries [][]byte) ([]uint64, error) {
	f.attempts.Add(1)
	if f.fail {
		select {
		case <-f.healthy:
		default:
			return nil, errors.New("log unavailable")
		}
	} else {
		select {
		case <-f.healthy:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	indexes, err := f.appender.Add(ctx, entries)
	if err == nil {
		f.added.Add(int64(len(entries)))
	}
	return indexes, err
}

func startExporter(t *testing.T, cfg *Config, open func(wakelog.Config) (appender, error), host component.Host) exporter.Traces {
	t.Helper()
	ctx := context.Background()
	set := exportertest.NewNopSettings(component.MustNewType("wakelog"))
	// The persistent queue is keyed by the exporter's id, so a restart needs the same one.
	set.ID = component.NewID(set.ID.Type())
	set.Logger = zaptest.NewLogger(t)
	exp, err := newTraces(ctx, set, cfg, open)
	if err != nil {
		t.Fatal(err)
	}
	if err := exp.Start(ctx, host); err != nil {
		t.Fatal(err)
	}
	return exp
}

// consumeQuickly fails the test if pushing takes longer than queueing should.
func consumeQuickly(t *testing.T, exp exporter.Traces, td ptrace.Traces) {
	t.Helper()
	done := make(chan error, 1)
	go func() { done <- exp.ConsumeTraces(context.Background(), td) }()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("push waited on the log instead of returning once queued")
	}
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestAppendsEventsButNotStructureSpans(t *testing.T) {
	cfg := testConfig(t, t.TempDir())
	exp := startExporter(t, cfg, openLog, componenttest.NewNopHost())
	if err := exp.ConsumeTraces(context.Background(), events(2)); err != nil {
		t.Fatal(err)
	}
	if err := exp.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	if size := logSize(t, cfg); size != "2" {
		t.Fatalf("log size = %s, want 2", size)
	}
}

func TestUnhealthyLogDoesNotHoldPushOpen(t *testing.T) {
	for _, tc := range []struct {
		name string
		fail bool
	}{
		{"slow", false},
		{"failing", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cfg := testConfig(t, t.TempDir())
			log := newFlakyLog(tc.fail)
			exp := startExporter(t, cfg, log.open, componenttest.NewNopHost())

			consumeQuickly(t, exp, events(2))
			waitFor(t, "an append attempt", func() bool { return log.attempts.Load() > 0 })

			log.recover()
			waitFor(t, "the events to reach the log", func() bool { return log.added.Load() == 2 })
			if err := exp.Shutdown(context.Background()); err != nil {
				t.Fatal(err)
			}
			if size := logSize(t, cfg); size != "2" {
				t.Fatalf("log size = %s, want 2", size)
			}
		})
	}
}

func TestQueuedEventsSurviveRestart(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig(t, dir)
	// Keep the failed append waiting to retry, so the restart is what delivers it.
	cfg.RetryConfig.InitialInterval = time.Hour
	cfg.RetryConfig.MaxInterval = time.Hour
	cfg.RetryConfig.MaxElapsedTime = 0
	storageID := component.MustNewID("file_storage")
	cfg.QueueConfig.Get().StorageID = &storageID
	queueDir := filepath.Join(dir, "queue")

	// The first run accepts the events while the log is down, then stops.
	down := newFlakyLog(true)
	exp, storage := startWithStorage(t, cfg, down.open, storageID, queueDir)
	consumeQuickly(t, exp, events(2))
	waitFor(t, "an append attempt", func() bool { return down.attempts.Load() > 0 })
	shutdown(t, exp, storage)

	// The second run finds the events in the queue and appends them.
	up := newFlakyLog(false)
	up.recover()
	exp, storage = startWithStorage(t, cfg, up.open, storageID, queueDir)
	waitFor(t, "the queued events to reach the log", func() bool { return up.added.Load() == 2 })
	shutdown(t, exp, storage)
	if size := logSize(t, cfg); size != "2" {
		t.Fatalf("log size = %s, want 2", size)
	}
}

// host hands components the extensions they look up, here the queue's storage.
type host map[component.ID]component.Component

func (h host) GetExtensions() map[component.ID]component.Component { return h }

func startWithStorage(t *testing.T, cfg *Config, open func(wakelog.Config) (appender, error), id component.ID, dir string) (exporter.Traces, component.Component) {
	t.Helper()
	factory := filestorage.NewFactory()
	storageCfg := factory.CreateDefaultConfig().(*filestorage.Config)
	storageCfg.Directory = dir
	storageCfg.CreateDirectory = true
	if err := storageCfg.Validate(); err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	storage, err := factory.Create(ctx, extensiontest.NewNopSettings(factory.Type()), storageCfg)
	if err != nil {
		t.Fatal(err)
	}
	if err := storage.Start(ctx, componenttest.NewNopHost()); err != nil {
		t.Fatal(err)
	}
	return startExporter(t, cfg, open, host{id: storage}), storage
}

func shutdown(t *testing.T, exp exporter.Traces, storage component.Component) {
	t.Helper()
	ctx := context.Background()
	if err := exp.Shutdown(ctx); err != nil {
		t.Fatal(err)
	}
	if err := storage.Shutdown(ctx); err != nil {
		t.Fatal(err)
	}
}
