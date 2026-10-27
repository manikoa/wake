# Wake collector

An OpenTelemetry Collector with two Wake components:

- **`wakesign` processor:** signs SDK and memory adapter events as they arrive and checks the signatures of events that arrive signed, such as the proxy's. See [docs/signing.md](../docs/signing.md).
- **`wakelog` exporter:** appends each signed event to the self hosted signed log in [`log/`](../log).

## Run it locally

```bash
make keys    # once: writes keys/collector.* and keys/log.*
make run     # builds and listens for OTLP over HTTP on localhost:4318
```

`config.yaml` is for local development: plain HTTP, and keys in local files. A real deployment puts TLS on the receiver and keeps the signing keys in KMS.

## The queue in front of the log

The `wakelog` exporter uses the collector's standard `sending_queue`, so the receiver answers the SDK as soon as an event is queued and a slow or failing log never holds an SDK's request open. `config.yaml` keeps the queue on disk with the `file_storage` extension in `data/queue`, because the SDK treats an answered event as delivered and won't send it again: an in memory queue would lose those events in a crash. When the collector restarts, it appends whatever was still queued, including appends that were waiting to retry. An event being appended at the moment of a crash can land twice, as two identical entries. Appends that still fail after the retry window (`retry_on_failure`, 5 minutes by default) are dropped and logged. When the queue is full, the receiver turns new events away at once with a retryable error instead of waiting.

## Checking the log

The log is a [tlog-tiles](https://c2sp.org/tlog-tiles) log in `data/log`, with checkpoints signed by `keys/log.pub`. Any tlog-tiles tool can check it, including Tessera's `fsck`. Each entry can be checked on its own with `keys/collector.pub`; the entry format is in [docs/events.md](../docs/events.md).

## Development

```bash
make test
```

Building needs the OpenTelemetry Collector Builder: `go install go.opentelemetry.io/collector/cmd/builder@v0.164.0`.
