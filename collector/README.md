# Wake collector

An OpenTelemetry Collector with two Wake components:

- **`wakesign` processor:** signs SDK, memory adapter and hook events as they arrive and checks the signatures of events that arrive signed, such as the proxy's. An event signed with an X.509-SVID only counts when `trusted_signers` lists its SPIFFE ID for the event's `wake.capture`, since agents' SVIDs chain to the same trust bundle as the proxy's, and its SVID is checked as of when the event arrived. An event that fails is kept, marked invalid with the reason in `wake.signature.error`; one bad event never fails the rest of its request. See [docs/signing.md](../docs/signing.md).
- **`wakelog` exporter:** appends each signed event to the self hosted signed log in [`log/`](../log).

## Run it locally

```bash
make keys    # once: writes keys/collector.* and keys/log.*
make run     # builds and listens for OTLP over HTTP on localhost:4318
```

`config.yaml` is for local development: plain HTTP, and keys in local files. A real deployment puts TLS on the receiver and keeps the signing keys in KMS.

## The queue in front of the log

The `wakelog` exporter uses the collector's standard `sending_queue`, so the receiver answers the SDK as soon as an event is queued and a slow or failing log never holds an SDK's request open. `config.yaml` keeps the queue on disk with the `file_storage` extension in `data/queue`, because the SDK treats an answered event as delivered and won't send it again: an in memory queue would lose those events in a crash. When the collector restarts, it appends whatever was still queued, including appends that were waiting to retry. An event being appended at the moment of a crash can land twice, as two identical entries. A failed append is retried until it lands (`retry_on_failure` has no `max_elapsed_time` by default), and each attempt has `timeout` (30 seconds by default). While the log is down the queue fills, and then the receiver turns new events away at once with a retryable error instead of waiting, so the SDKs see it and count what they drop. If you set `max_elapsed_time`, the events a retry gives up on are dropped, logged and counted in `wake_log_dropped_events` with reason `retries_exhausted`.

## Health and metrics

The `health_check` extension answers on port 13133 while the collector is up. The collector's own metrics are on port 8888 at `/metrics` for Prometheus: the `otelcol_*` pipeline metrics, and `wake_log_dropped_events`, the events that never reached the signed log, by `reason` (`unloggable` or `retries_exhausted`). Alert on any increase in it, and on `otelcol_receiver_refused_spans`.

## Checking the log

The receiver authenticates no sender: TLS proves the collector to whoever calls it, not the other way round. So anything that reaches port 4318 can have an unsigned event signed in any agent's name, and logged valid. The signature on an SDK, memory adapter or hook event says the collector received it. Only the proxy's own signature says more, which is why [docs/signing.md](../docs/signing.md) keeps the two apart.

The log is a [tlog-tiles](https://c2sp.org/tlog-tiles) log in `data/log`, with checkpoints signed by `keys/log.pub`. Any tlog-tiles tool can check it, including Tessera's `fsck`. Each entry can be checked on its own with `keys/collector.pub`; payloads too large for their entries are in `data/log/payload/sha256/`. The entry format is in [docs/events.md](../docs/events.md).

## Development

```bash
make test
```

Building needs the OpenTelemetry Collector Builder: `go install go.opentelemetry.io/collector/cmd/builder@v0.164.0`.
