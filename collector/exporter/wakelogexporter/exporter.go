// Package wakelogexporter appends signed Wake events to the self hosted signed log.
// Spans that only group events aren't appended. A payload too large for its entry is
// stored next to the log first, and the entry commits to it by its signed digest.
package wakelogexporter

import (
	"context"
	"errors"
	"sync/atomic"
	"time"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/config/configoptional"
	"go.opentelemetry.io/collector/config/configretry"
	"go.opentelemetry.io/collector/exporter"
	"go.opentelemetry.io/collector/exporter/exporterhelper"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/metric/noop"
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

	// TimeoutConfig bounds each attempt to append a request (`timeout`). An append waits
	// for Tessera to sequence its batch and write it to disk, and one that times out is
	// retried, so leave it room.
	TimeoutConfig exporterhelper.TimeoutConfig `mapstructure:",squash"`

	// QueueConfig is the collector's standard sending queue. The receiver answers once
	// an event is queued, and appending to the log happens behind the queue, so a slow
	// or failing log never holds an SDK's request open. Set `storage` to a file_storage
	// extension to keep the queue on disk: events the collector has already acknowledged
	// to the SDK then survive a crash or restart, where an in memory queue loses them and
	// nothing upstream knows to send them again.
	QueueConfig configoptional.Optional[exporterhelper.QueueBatchConfig] `mapstructure:"sending_queue"`
	// RetryConfig is the collector's standard retry for failed appends. By default it
	// never gives up (max_elapsed_time 0): while the log is down the queue fills, then the
	// receiver refuses new events, so the SDKs see it and count what they drop. Events a
	// retry with a time limit gives up on are counted in wake_log_dropped_events.
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
	timeout := exporterhelper.NewDefaultTimeoutConfig()
	timeout.Timeout = 30 * time.Second
	retry := configretry.NewDefaultBackOffConfig()
	retry.MaxElapsedTime = 0
	return &Config{
		TimeoutConfig: timeout,
		QueueConfig:   configoptional.Some(exporterhelper.NewDefaultQueueConfig()),
		RetryConfig:   retry,
	}
}

func createTraces(ctx context.Context, set exporter.Settings, cfg component.Config) (exporter.Traces, error) {
	return newTraces(ctx, set, cfg.(*Config), openLog)
}

func newTraces(ctx context.Context, set exporter.Settings, cfg *Config, open func(wakelog.Config) (appender, error)) (exporter.Traces, error) {
	dropped, err := set.MeterProvider.Meter("github.com/manikoa/wake/collector/exporter/wakelogexporter").Int64Counter(
		"wake_log_dropped_events",
		metric.WithDescription("Events that never reached the signed log, by reason: unloggable (too large for an entry even without their payload, or no canonical bytes) or retries_exhausted (retry_on_failure gave up)"),
		metric.WithUnit("{event}"),
	)
	if err != nil {
		return nil, err
	}
	e := &logExporter{cfg: cfg, open: open, logger: set.Logger, dropped: dropped}
	// The retry runs in an exporterhelper of its own, behind the queue's, so the exporter
	// sees when it gives up and can count what's lost. Its own telemetry is off; the
	// queue's reports for both.
	retrySet := set
	retrySet.MeterProvider = noop.NewMeterProvider()
	retry, err := exporterhelper.NewTraces(ctx, retrySet, cfg, e.push,
		exporterhelper.WithTimeout(cfg.TimeoutConfig),
		exporterhelper.WithRetry(cfg.RetryConfig),
	)
	if err != nil {
		return nil, err
	}
	noTimeout := exporterhelper.NewDefaultTimeoutConfig()
	noTimeout.Timeout = 0
	queue, err := exporterhelper.NewTraces(ctx, set, cfg, e.sendThrough(retry),
		exporterhelper.WithStart(e.start),
		exporterhelper.WithShutdown(e.shutdown),
		exporterhelper.WithQueue(cfg.QueueConfig),
		exporterhelper.WithTimeout(noTimeout),
	)
	if err != nil {
		return nil, err
	}
	return &traces{Traces: queue, retry: retry, stopping: &e.stopping}, nil
}

// traces is the queue in front of the retry, started and stopped in the order a single
// exporterhelper uses.
type traces struct {
	exporter.Traces
	retry    exporter.Traces
	stopping *atomic.Bool
}

func (t *traces) Start(ctx context.Context, host component.Host) error {
	if err := t.retry.Start(ctx, host); err != nil {
		return err
	}
	return t.Traces.Start(ctx, host)
}

// Shutdown stops retrying first, so the queue's consumers return, and a persistent queue
// keeps what they held for the next start.
func (t *traces) Shutdown(ctx context.Context) error {
	t.stopping.Store(true)
	err := t.retry.Shutdown(ctx)
	return errors.Join(err, t.Traces.Shutdown(ctx))
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
	cfg      *Config
	open     func(wakelog.Config) (appender, error)
	log      appender
	logger   *zap.Logger
	dropped  metric.Int64Counter
	stopping atomic.Bool
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

// sendThrough returns the queue's consumer: it hands a request to the retry, and counts
// the events in it when the retry gives up, since the queue then drops them. Errors
// while shutting down aren't drops: a persistent queue keeps the request.
func (e *logExporter) sendThrough(retry exporter.Traces) func(context.Context, ptrace.Traces) error {
	return func(ctx context.Context, td ptrace.Traces) error {
		err := retry.ConsumeTraces(ctx, td)
		if err == nil || e.stopping.Load() {
			return err
		}
		n := 0
		for _, rs := range td.ResourceSpans().All() {
			for _, ss := range rs.ScopeSpans().All() {
				for _, span := range ss.Spans().All() {
					if event.IsEvent(span) {
						n++
					}
				}
			}
		}
		e.logger.Error("Dropping events the signed log never took", zap.Int("events", n), zap.Error(err))
		e.dropped.Add(ctx, int64(n), metric.WithAttributes(attribute.String("reason", "retries_exhausted")))
		return err
	}
}

// drop logs and counts an event that can't be logged, such as one too large for an
// entry even without its payload.
func (e *logExporter) drop(ctx context.Context, span ptrace.Span, err error) {
	id, _ := span.Attributes().Get("wake.event.id")
	e.logger.Error("Dropping an event that can't go in the signed log",
		zap.String("wake.event.id", id.Str()), zap.Error(err))
	e.dropped.Add(ctx, 1, metric.WithAttributes(attribute.String("reason", "unloggable")))
}
