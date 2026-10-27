package wakelogexporter

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/component/componenttest"
	"go.opentelemetry.io/collector/consumer"
	"go.opentelemetry.io/collector/exporter/exportertest"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.opentelemetry.io/collector/processor"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"

	"github.com/manikoa/wake/collector/event"
	"github.com/manikoa/wake/collector/processor/wakesignprocessor"
	wakelog "github.com/manikoa/wake/log"
)

// countingLog counts calls to Add on the real log.
type countingLog struct {
	appender
	attempts atomic.Int64
}

func (c *countingLog) open(cfg wakelog.Config) (appender, error) {
	l, err := wakelog.Open(cfg)
	c.appender = l
	return c, err
}

func (c *countingLog) Add(ctx context.Context, entries [][]byte) ([]uint64, error) {
	c.attempts.Add(1)
	return c.appender.Add(ctx, entries)
}

// startSigner returns the collector's signing processor, passing what it signs to next.
func startSigner(t *testing.T, dir string, next consumer.Traces) processor.Traces {
	t.Helper()
	private, _, err := wakelog.GenerateKey("collector")
	if err != nil {
		t.Fatal(err)
	}
	keyFile := filepath.Join(dir, "collector.key")
	if err := os.WriteFile(keyFile, []byte(private), 0o600); err != nil {
		t.Fatal(err)
	}
	factory := wakesignprocessor.NewFactory()
	cfg := factory.CreateDefaultConfig().(*wakesignprocessor.Config)
	cfg.SigningKeyFile = keyFile
	set := processor.Settings{
		ID:                component.NewID(factory.Type()),
		TelemetrySettings: componenttest.NewNopTelemetrySettings(),
	}
	proc, err := factory.CreateTraces(context.Background(), set, cfg, next)
	if err != nil {
		t.Fatal(err)
	}
	if err := proc.Start(context.Background(), componenttest.NewNopHost()); err != nil {
		t.Fatal(err)
	}
	return proc
}

// addSDKEvent adds an unsigned SDK event carrying payload, as the Python SDK sends it.
func addSDKEvent(spans ptrace.SpanSlice, kind string, payload any) {
	body, err := json.Marshal(payload)
	if err != nil {
		panic(err)
	}
	digest := sha256.Sum256(body)
	attrs := spans.AppendEmpty().Attributes()
	attrs.PutStr(event.Kind, kind)
	attrs.PutStr(event.Capture, "sdk")
	attrs.PutStr("wake.payload", string(body))
	attrs.PutStr("wake.payload.sha256", hex.EncodeToString(digest[:]))
}

// TestOversizedEventToday shows what happens today to an event whose log entry is
// larger than the 65,535 bytes Tessera allows: a tool call that read a 100 KB email,
// sent in the same SDK request as two small model calls.
//
//   - The SDK is told the request arrived, because the queue accepted it.
//   - The oversized event never reaches the log: Tessera rejects it on every attempt.
//   - The whole request is retried, and the small events in it are appended again on
//     every attempt, so the log fills with duplicates.
//   - Once the retry gives up, the request is dropped. The only trace of the lost
//     event is a collector log line and the exporter's failed span count.
//
// docs/large-payloads.md proposes the fix. This test changes with it.
func TestOversizedEventToday(t *testing.T) {
	dir := t.TempDir()
	cfg := testConfig(t, dir)
	cfg.RetryConfig.MaxElapsedTime = 300 * time.Millisecond

	core, collectorLogs := observer.New(zap.InfoLevel)
	log := &countingLog{}
	set := exportertest.NewNopSettings(component.MustNewType("wakelog"))
	set.Logger = zap.New(core)
	exp, err := newTraces(context.Background(), set, cfg, log.open)
	if err != nil {
		t.Fatal(err)
	}
	if err := exp.Start(context.Background(), componenttest.NewNopHost()); err != nil {
		t.Fatal(err)
	}
	signer := startSigner(t, dir, exp)

	td := ptrace.NewTraces()
	spans := td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty().Spans()
	addSDKEvent(spans, "model_call", map[string]any{"input": "Summarize my inbox", "output": "Reading it now"})
	email := strings.Repeat("Quarterly numbers attached. Please review before Friday.\n", 100_000/57)
	addSDKEvent(spans, "tool_call", map[string]any{"input": map[string]any{"id": "msg-1"}, "output": email})
	addSDKEvent(spans, "model_call", map[string]any{"input": "Summarize the email", "output": "Numbers are due Friday"})
	consumeQuickly(t, signer, td)

	waitFor(t, "the request to be dropped", func() bool {
		return collectorLogs.FilterMessageSnippet("Dropping data").Len() > 0
	})
	if err := exp.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}

	dropped := collectorLogs.FilterMessageSnippet("Dropping data").All()[0]
	if msg := dropped.ContextMap()["error"]; !strings.Contains(msg.(string), "entry data too large") {
		t.Fatalf("dropped with %q, want Tessera's size error", msg)
	}
	attempts := log.attempts.Load()
	if attempts < 2 {
		t.Fatalf("%d append attempts, want the request retried", attempts)
	}
	// Each attempt appended the two small events again and never the large one.
	if size, want := logSize(t, cfg), 2*attempts; size != strconv.FormatInt(want, 10) {
		t.Fatalf("log size = %s, want %d: two entries for each of %d attempts", size, want, attempts)
	}
	t.Logf("%d attempts appended %d copies of each small event; the 100 KB event was dropped", attempts, attempts)
}
