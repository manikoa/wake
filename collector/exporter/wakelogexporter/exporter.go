// Package wakelogexporter appends signed Wake events to the self hosted signed log.
// Spans that only group events aren't appended.
package wakelogexporter

import (
	"context"
	"errors"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/config/configretry"
	"go.opentelemetry.io/collector/exporter"
	"go.opentelemetry.io/collector/exporter/exporterhelper"
	"go.opentelemetry.io/collector/pdata/ptrace"

	"github.com/manikoa/wake/collector/event"
	wakelog "github.com/manikoa/wake/log"
)

// Config for the wakelog exporter.
type Config struct {
	// Dir is where the log's tiles and checkpoint are written.
	Dir string `mapstructure:"dir"`
	// KeyFile holds the note format private key that signs the log's checkpoints.
	KeyFile string `mapstructure:"key_file"`
}

// Validate checks the config.
func (c *Config) Validate() error {
	if c.Dir == "" || c.KeyFile == "" {
		return errors.New("dir and key_file are required")
	}
	return nil
}

// NewFactory returns the factory for the wakelog exporter.
func NewFactory() exporter.Factory {
	return exporter.NewFactory(
		component.MustNewType("wakelog"),
		func() component.Config { return &Config{} },
		exporter.WithTraces(createTraces, component.StabilityLevelDevelopment),
	)
}

func createTraces(ctx context.Context, set exporter.Settings, cfg component.Config) (exporter.Traces, error) {
	e := &logExporter{cfg: cfg.(*Config)}
	return exporterhelper.NewTraces(ctx, set, cfg, e.push,
		exporterhelper.WithStart(e.start),
		exporterhelper.WithShutdown(e.shutdown),
		exporterhelper.WithRetry(configretry.NewDefaultBackOffConfig()),
	)
}

type logExporter struct {
	cfg *Config
	log *wakelog.Log
}

func (e *logExporter) start(context.Context, component.Host) error {
	l, err := wakelog.Open(wakelog.Config{Dir: e.cfg.Dir, KeyFile: e.cfg.KeyFile})
	if err != nil {
		return err
	}
	e.log = l
	return nil
}

func (e *logExporter) shutdown(ctx context.Context) error {
	if e.log == nil {
		return nil
	}
	return e.log.Close(ctx)
}

func (e *logExporter) push(ctx context.Context, td ptrace.Traces) error {
	var entries [][]byte
	for _, rs := range td.ResourceSpans().All() {
		for _, ss := range rs.ScopeSpans().All() {
			for _, span := range ss.Spans().All() {
				if !event.IsEvent(span) {
					continue
				}
				entry, err := event.Entry(span)
				if err != nil {
					return err
				}
				entries = append(entries, entry)
			}
		}
	}
	if len(entries) == 0 {
		return nil
	}
	_, err := e.log.Add(ctx, entries)
	return err
}
