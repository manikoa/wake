package wakelogexporter

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/transparency-dev/tessera/api"
	"github.com/transparency-dev/tessera/api/layout"
	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/component/componenttest"
	"go.opentelemetry.io/collector/consumer"
	"go.opentelemetry.io/collector/exporter"
	"go.opentelemetry.io/collector/exporter/exportertest"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.opentelemetry.io/collector/processor"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest/observer"
	"golang.org/x/mod/sumdb/note"

	"github.com/usewake/wake/collector/event"
	"github.com/usewake/wake/collector/processor/wakesignprocessor"
	wakelog "github.com/usewake/wake/log"
)

// countingLog wraps the real log, counting appends and failing the first payload writes.
type countingLog struct {
	appender
	failPayloads atomic.Int64 // payload writes still to fail
	attempts     atomic.Int64
	added        atomic.Int64
}

func (c *countingLog) open(cfg wakelog.Config) (appender, error) {
	l, err := wakelog.Open(cfg)
	c.appender = l
	return c, err
}

func (c *countingLog) Add(ctx context.Context, entries [][]byte) ([]uint64, error) {
	c.attempts.Add(1)
	indexes, err := c.appender.Add(ctx, entries)
	if err == nil {
		c.added.Add(int64(len(entries)))
	}
	return indexes, err
}

func (c *countingLog) PutPayload(ctx context.Context, payload []byte) error {
	if c.failPayloads.Add(-1) >= 0 {
		return errors.New("bucket unavailable")
	}
	return c.appender.PutPayload(ctx, payload)
}

// pipeline is the collector's signing processor feeding the wakelog exporter.
type pipeline struct {
	signer    processor.Traces
	exporter  exporter.Traces
	log       *countingLog
	cfg       *Config
	trusted   map[string]note.Verifier
	logs      *observer.ObservedLogs
	telemetry *componenttest.Telemetry
}

func startPipeline(t *testing.T) *pipeline {
	t.Helper()
	dir := t.TempDir()
	p := &pipeline{cfg: testConfig(t, dir), log: &countingLog{}, telemetry: componenttest.NewTelemetry()}
	t.Cleanup(func() { p.telemetry.Shutdown(context.Background()) })

	core, logs := observer.New(zap.InfoLevel)
	p.logs = logs
	set := exportertest.NewNopSettings(component.MustNewType("wakelog"))
	set.TelemetrySettings = p.telemetry.NewTelemetrySettings()
	set.Logger = zap.New(core)
	exp, err := newTraces(context.Background(), set, p.cfg, p.log.open)
	if err != nil {
		t.Fatal(err)
	}
	if err := exp.Start(context.Background(), componenttest.NewNopHost()); err != nil {
		t.Fatal(err)
	}
	p.exporter = exp
	p.signer, p.trusted = startSigner(t, dir, exp)
	return p
}

// startSigner returns the collector's signing processor, passing what it signs to next,
// and the collector's public key.
func startSigner(t *testing.T, dir string, next consumer.Traces) (processor.Traces, map[string]note.Verifier) {
	t.Helper()
	private, public, err := wakelog.GenerateKey("collector")
	if err != nil {
		t.Fatal(err)
	}
	verifier, err := note.NewVerifier(public)
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
	return proc, map[string]note.Verifier{"collector": verifier}
}

// addSDKEvent adds an unsigned SDK event carrying payload, as the Python SDK sends it.
func addSDKEvent(spans ptrace.SpanSlice, id, kind string, payload any) ptrace.Span {
	body, err := json.Marshal(payload)
	if err != nil {
		panic(err)
	}
	digest := sha256.Sum256(body)
	span := spans.AppendEmpty()
	attrs := span.Attributes()
	attrs.PutStr("wake.event.id", id)
	attrs.PutStr(event.Kind, kind)
	attrs.PutStr(event.Capture, "sdk")
	attrs.PutStr(event.Payload, string(body))
	attrs.PutStr(event.PayloadSHA256, hex.EncodeToString(digest[:]))
	return span
}

// inboxRequest is one SDK request: a tool call that read a 100 KB email, between two
// small model calls.
func inboxRequest() (ptrace.Traces, string) {
	td := ptrace.NewTraces()
	spans := td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty().Spans()
	addSDKEvent(spans, "e1", "model_call", map[string]any{"input": "Summarize my inbox", "output": "Reading it now"})
	email := strings.Repeat("Quarterly numbers attached. Please review before Friday.\n", 100_000/57)
	addSDKEvent(spans, "e2", "tool_call", map[string]any{"input": map[string]any{"id": "msg-1"}, "output": email})
	addSDKEvent(spans, "e3", "model_call", map[string]any{"input": "Summarize the email", "output": "Numbers are due Friday"})
	return td, email
}

// entries reads every entry in the log back from its tiles, as any tlog-tiles reader would.
func entries(t *testing.T, cfg *Config) []wakelog.Entry {
	t.Helper()
	var size uint64
	if err := json.Unmarshal([]byte(logSize(t, cfg)), &size); err != nil {
		t.Fatal(err)
	}
	var out []wakelog.Entry
	for index := uint64(0); index < size; index += layout.EntryBundleWidth {
		data, err := os.ReadFile(filepath.Join(cfg.Dir, layout.EntriesPathForLogIndex(index, size)))
		if err != nil {
			t.Fatal(err)
		}
		var bundle api.EntryBundle
		if err := bundle.UnmarshalText(data); err != nil {
			t.Fatal(err)
		}
		for _, raw := range bundle.Entries {
			entry, err := wakelog.ParseEntry(raw)
			if err != nil {
				t.Fatal(err)
			}
			out = append(out, entry)
		}
	}
	return out
}

// readFrom fetches payload files from a log on local disk.
func readFrom(cfg *Config) func(string) ([]byte, error) {
	return func(path string) ([]byte, error) {
		return os.ReadFile(filepath.Join(cfg.Dir, filepath.FromSlash(path)))
	}
}

func (p *pipeline) shutdown(t *testing.T) {
	t.Helper()
	if err := p.exporter.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
}

// TestLargePayloadLandsInTheLog sends a tool call that read a 100 KB email, in one SDK
// request with two small model calls, through the signing processor and the exporter.
// Before payloads could be stored next to the log, Tessera refused the large entry, the
// whole request was retried with the small events appended again each time, and the
// large event was dropped after five minutes. See docs/large-payloads.md.
func TestLargePayloadLandsInTheLog(t *testing.T) {
	p := startPipeline(t)
	td, email := inboxRequest()
	consumeQuickly(t, p.signer, td)
	waitFor(t, "the events to reach the log", func() bool { return p.log.added.Load() == 3 })
	p.shutdown(t)

	if attempts := p.log.attempts.Load(); attempts != 1 {
		t.Fatalf("%d append attempts, want 1", attempts)
	}
	logged := entries(t, p.cfg)
	if len(logged) != 3 {
		t.Fatalf("log holds %d entries, want each event once", len(logged))
	}
	inline := 0
	for _, entry := range logged {
		// Each entry and its payload check on their own, with only the collector's public
		// key and the log's files.
		if !entry.Verify(p.trusted) {
			t.Fatalf("entry doesn't verify: %s", entry.Event)
		}
		payload, err := entry.ReadPayload(readFrom(p.cfg))
		if err != nil {
			t.Fatal(err)
		}
		ev, err := entry.Decode()
		if err != nil {
			t.Fatal(err)
		}
		if _, signed := ev.Attributes[event.Payload]; signed {
			t.Errorf("%s: payload is in the signed bytes", ev.String("wake.event.id"))
		}
		if entry.Payload != nil {
			inline++
			continue
		}
		var body struct{ Output string }
		if err := json.Unmarshal(payload, &body); err != nil || body.Output != email {
			t.Fatalf("stored payload isn't the email: %v", err)
		}
		if ev.String("wake.event.id") != "e2" {
			t.Errorf("%s's payload was stored outside its entry", ev.String("wake.event.id"))
		}
	}
	if inline != 2 {
		t.Errorf("%d payloads inline, want the two small ones", inline)
	}
	if n := p.logs.FilterMessageSnippet("Exporting failed").Len(); n != 0 {
		t.Errorf("%d export failures logged", n)
	}
}

func TestFailedPayloadWriteRetriesWithoutDuplicates(t *testing.T) {
	p := startPipeline(t)
	p.log.failPayloads.Store(2)
	td, _ := inboxRequest()
	consumeQuickly(t, p.signer, td)
	waitFor(t, "the events to reach the log", func() bool { return p.log.added.Load() == 3 })
	p.shutdown(t)

	if n := p.logs.FilterMessageSnippet("Will retry").Len(); n != 2 {
		t.Errorf("%d retries logged, want 2", n)
	}
	if size := logSize(t, p.cfg); size != "3" {
		t.Fatalf("log size = %s, want each event once", size)
	}
}

func TestEventTooLargeWithoutPayloadIsDroppedAlone(t *testing.T) {
	p := startPipeline(t)
	td := ptrace.NewTraces()
	spans := td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty().Spans()
	addSDKEvent(spans, "e1", "model_call", map[string]any{"output": "fine"})
	huge := addSDKEvent(spans, "e2", "model_call", map[string]any{"output": "fine"})
	parents := huge.Attributes().PutEmptySlice("wake.parents")
	for range 2000 {
		parents.AppendEmpty().SetStr("01890a5d-ac96-774b-bcce-b302099a8057")
	}
	consumeQuickly(t, p.signer, td)
	waitFor(t, "the small event to reach the log", func() bool { return p.log.added.Load() == 1 })
	p.shutdown(t)

	if attempts := p.log.attempts.Load(); attempts != 1 {
		t.Errorf("%d append attempts, want 1", attempts)
	}
	if size := logSize(t, p.cfg); size != "1" {
		t.Errorf("log size = %s, want 1", size)
	}
	dropped := p.logs.FilterMessageSnippet("Dropping an event").All()
	if len(dropped) != 1 || dropped[0].ContextMap()["wake.event.id"] != "e2" {
		t.Fatalf("drop log = %v", dropped)
	}
	metric, err := p.telemetry.GetMetric("wake_log_dropped_events")
	if err != nil {
		t.Fatal(err)
	}
	if sum := metric.Data.(metricdata.Sum[int64]); sum.DataPoints[0].Value != 1 {
		t.Errorf("dropped events counted = %d, want 1", sum.DataPoints[0].Value)
	}
}

func TestStructureSpansAndSmallPayloadsStayInline(t *testing.T) {
	p := startPipeline(t)
	td := events(1)
	addSDKEvent(td.ResourceSpans().At(0).ScopeSpans().At(0).Spans(), "e1", "tool_call", map[string]any{"output": "short"})
	consumeQuickly(t, p.signer, td)
	waitFor(t, "the events to reach the log", func() bool { return p.log.added.Load() == 2 })
	p.shutdown(t)

	files, err := os.ReadDir(filepath.Join(p.cfg.Dir, "payload", "sha256"))
	if err != nil {
		t.Fatal(err)
	}
	if len(files) != 0 {
		t.Errorf("%d payload files written for payloads that fit", len(files))
	}
}
