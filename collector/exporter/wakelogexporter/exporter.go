// Package wakelogexporter appends signed Wake events to the self hosted signed log.
// Spans that only group events aren't appended. A payload too large for its entry is
// stored next to the log first, and the entry commits to it by its signed digest.
package wakelogexporter

import (
	"context"
	"errors"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/config/configoptional"
	"go.opentelemetry.io/collector/config/configretry"
	"go.opentelemetry.io/collector/exporter"
	"go.opentelemetry.io/collector/exporter/exporterhelper"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.opentelemetry.io/otel/metric"
	"go.uber.org/zap"

	"github.com/manikoa/wake/collector/event"
	wakelog "github.com/manikoa/wake/log"
)

// Config for the wakelog exporter.
type Config struct {
	// Dir is where the log's tiles and checkpoint are written.
	Dir string `mapstructure:"dir"`
	// KeyFile holds the note format private key that signs the log's checkpoints.
	KeyFile string `mapstructure:"key_file"`

	// QueueConfig is the collector's standard sending queue. The receiver answers once
	// an event is queued, and appending to the log happens behind the queue, so a slow
	// or failing log never holds an SDK's request open. Set `storage` to a file_storage
	// extension to keep the queue on disk: events the collector has already acknowledged
	// to the SDK then survive a crash or restart, where an in memory queue loses them and
	// nothing upstream knows to send them again.
	QueueConfig configoptional.Optional[exporterhelper.QueueBatchConfig] `mapstructure:"sending_queue"`
	// RetryConfig is the collector's standard retry for failed appends.
	RetryConfig configretry.BackOffConfig `mapstructure:"retry_on_failure"`
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
		createDefaultConfig,
		exporter.WithTraces(createTraces, component.StabilityLevelDevelopment),
	)
}

// createDefaultConfig queues in memory, because a persistent queue needs a storage
// extension the config has to name. config.yaml turns on the persistent queue.
func createDefaultConfig() component.Config {
	return &Config{
		QueueConfig: configoptional.Some(exporterhelper.NewDefaultQueueConfig()),
		RetryConfig: configretry.NewDefaultBackOffConfig(),
	}
}

func createTraces(ctx context.Context, set exporter.Settings, cfg component.Config) (exporter.Traces, error) {
	return newTraces(ctx, set, cfg.(*Config), openLog)
}

func newTraces(ctx context.Context, set exporter.Settings, cfg *Config, open func(wakelog.Config) (appender, error)) (exporter.Traces, error) {
	dropped, err := set.MeterProvider.Meter("github.com/manikoa/wake/collector/exporter/wakelogexporter").Int64Counter(
		"wake_log_dropped_events",
		metric.WithDescription("Events that can't go in the signed log, even without their payload, and were dropped"),
		metric.WithUnit("{event}"),
	)
	if err != nil {
		return nil, err
	}
	e := &logExporter{cfg: cfg, open: open, logger: set.Logger, dropped: dropped}
	return exporterhelper.NewTraces(ctx, set, cfg, e.push,
		exporterhelper.WithStart(e.start),
		exporterhelper.WithShutdown(e.shutdown),
		exporterhelper.WithQueue(cfg.QueueConfig),
		exporterhelper.WithRetry(cfg.RetryConfig),
	)
}

// appender is the part of the signed log the exporter uses.
type appender interface {
	Add(ctx context.Context, entries [][]byte) ([]uint64, error)
	PutPayload(ctx context.Context, payload []byte) error
	Close(ctx context.Context) error
}

func openLog(cfg wakelog.Config) (appender, error) {
	return wakelog.Open(cfg)
}

type logExporter struct {
	cfg     *Config
	open    func(wakelog.Config) (appender, error)
	log     appender
	logger  *zap.Logger
	dropped metric.Int64Counter
}

func (e *logExporter) start(context.Context, component.Host) error {
	l, err := e.open(wakelog.Config{Dir: e.cfg.Dir, KeyFile: e.cfg.KeyFile})
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
				entry, external, err := event.Entry(span)
				if err != nil {
					// Retrying can't fix it, and failing the request would append the
					// rest of it again on every retry.
					e.drop(ctx, span, err)
					continue
				}
				if external {
					// Stored before the entry is appended, so no entry points to a
					// payload that was never written. A failure retries the request.
					payload, _ := event.PayloadOf(span)
					if err := e.log.PutPayload(ctx, payload); err != nil {
						return err
					}
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

// drop logs and counts an event that can't be logged, such as one too large for an
// entry even without its payload.
func (e *logExporter) drop(ctx context.Context, span ptrace.Span, err error) {
	id, _ := span.Attributes().Get("wake.event.id")
	e.logger.Error("Dropping an event that can't go in the signed log",
		zap.String("wake.event.id", id.Str()), zap.Error(err))
	e.dropped.Add(ctx, 1)
}
