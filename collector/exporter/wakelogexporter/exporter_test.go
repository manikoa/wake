package wakelogexporter

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"go.opentelemetry.io/collector/pdata/ptrace"

	"github.com/manikoa/wake/collector/event"
	wakelog "github.com/manikoa/wake/log"
)

func TestAppendsEventsButNotStructureSpans(t *testing.T) {
	dir := t.TempDir()
	private, _, err := wakelog.GenerateKey("test-log")
	if err != nil {
		t.Fatal(err)
	}
	keyFile := filepath.Join(dir, "log.key")
	if err := os.WriteFile(keyFile, []byte(private), 0o600); err != nil {
		t.Fatal(err)
	}
	logDir := filepath.Join(dir, "log")
	e := &logExporter{cfg: &Config{Dir: logDir, KeyFile: keyFile}}
	ctx := context.Background()
	if err := e.start(ctx, nil); err != nil {
		t.Fatal(err)
	}

	td := ptrace.NewTraces()
	spans := td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty().Spans()
	spans.AppendEmpty() // a structure span
	for range 2 {
		spans.AppendEmpty().Attributes().PutStr(event.Kind, "model_call")
	}
	if err := e.push(ctx, td); err != nil {
		t.Fatal(err)
	}
	if err := e.shutdown(ctx); err != nil {
		t.Fatal(err)
	}

	checkpoint, err := os.ReadFile(filepath.Join(logDir, "checkpoint"))
	if err != nil {
		t.Fatal(err)
	}
	if size := strings.Split(string(checkpoint), "\n")[1]; size != "2" {
		t.Fatalf("log size = %s, want 2", size)
	}
}
