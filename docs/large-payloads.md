# Payloads larger than a log entry

Decided October 26, 2026: the recommendation below, built in manikoa/wake#13.

## The problem

A tlog-tiles entry bundle stores each entry's length in two bytes, so one entry holds at most 65,535 bytes. Tessera v1.0.4 enforces this (`DefaultEntrySizeLimit`) and has no option to raise it for a tlog-tiles log. Before this change each log entry held the event's signed bytes, and those included `wake.payload`: the email body, the web page, the model's whole context. Real payloads are often larger. The limit applies to the entry, not the payload, and the payload is escaped again inside the signed JSON, so a payload full of quotes or non-ASCII text hits it well before 64 KB.

What happened, shown by a test with a tool call that read a 100 KB email, sent in one SDK request with two small model calls. The same test, `TestLargePayloadLandsInTheLog` in `collector/exporter/wakelogexporter/large_payload_test.go`, now checks the fix:

1. The receiver answered once the queue accepted the request, so the SDK counted every event as sent.
2. Tessera rejected the large entry on every attempt. The exporter returned that error for the whole request, so the retry sent the whole request again.
3. The other events in the request were appended again on every attempt. With the default retry, which gives up after 5 minutes, that's about a dozen copies of each.
4. When the retry gave up, the request was dropped and the large event was lost. The only traces were a collector error log line and `otelcol_exporter_send_failed_spans`, which counted every span in the request, including the ones that reached the log. Nothing in Wake raised a health signal.
5. The queue wasn't blocked, but each such request held one of its 10 consumers for up to 5 minutes. An agent that read many large pages could hold all 10; the queue would then fill and the receiver start refusing events.

## Options

### (a) Keep large payloads next to the log, committed to by digest

The signed event always carries `wake.payload.sha256`. A payload that fits goes in the log entry; one that doesn't is written as a content addressed file next to the log's tiles, in the same bucket under the same object lock, and the entry points to it through the signed digest.

### (b) Raise the limit, or split an event across entries

**Raising the limit** means forking Tessera and leaving tlog-tiles. Tessera's fsck, Sigstore's tooling and any third party tlog-tiles client could no longer read the log, which breaks "anyone can check the log without trusting Wake". Tessera's one larger limit, 256 KiB, comes only with the static-ct layout, which holds certificate transparency entries. And no fixed limit covers a model's whole context, which can run to megabytes.

**Splitting** keeps the format but loses more:

- An entry is no longer checkable on its own. The signature covers the whole event, so a verifier has to find every chunk and put the event back together first.
- The exporter's 10 consumers append at the same time, so chunks of different events interleave. Every reader needs reassembly state.
- A failure halfway leaves stray chunks in an append only log forever, and a retry appends them again.
- Payloads stay inside the hash tree for good, so a payload can never be erased without breaking the log. (Whichever way they're stored, that's why credentials are redacted before an event is signed: see Secrets in [events.md](events.md).)

## Recommendation: (a), with the payload always outside the signed bytes

One change from (a) as first framed: the payload is **never** part of the signed bytes, rather than signed when it fits and left out when it doesn't. The signer computes `wake.payload.sha256` and signs it with the rest of the event. Where the payload ends up is decided later, by the exporter, and doesn't touch what was signed:

- If the entry still fits in 65,535 bytes with the payload in it, the payload goes in the entry as its own field, next to the signed bytes.
- Otherwise the exporter writes the payload to `payload/sha256/<hex>` next to the tiles, then appends the entry without it. The file is always written before the entry, so an entry never points to a file that was never written.
- A payload that isn't valid UTF-8 always goes to a file, because a JSON string can't carry it byte for byte.

Why keep the payload out of the signed bytes even when it fits:

- Whether a payload fits is a storage detail of this log. If it decided the signed bytes, every signer, the collector in Go and the proxy in Rust, would have to repeat the log's size arithmetic, and verifiers would face two shapes of signed event. This way there is one shape, and the proxy never needs to know the log's limit.
- The placement can change later, to a lower threshold, to every payload in a file, or to payloads kept only in the customer's ClickHouse, without changing what's signed.
- Signing the digest is as strong as signing the payload: the signature covers the digest, and SHA-256 ties the digest to exactly one payload. Sigstore, in-toto and OCI sign artifacts the same way.

How anyone checks an entry on its own:

1. Check the signature over `event` with the signer's public key. This needs nothing else, payload or not.
2. Take the payload from the entry's `payload` field, or from `payload/sha256/<wake.payload.sha256>` next to the log.
3. Check that its SHA-256 matches the signed `wake.payload.sha256`.
4. Prove the entry is in the log against a signed checkpoint, as today.

Against the principles:

- **The customer holds the evidence.** Payload files sit with the tiles, in the customer's bucket, and the bucket's default object lock retention covers them like every other new object. The self hosted log writes them under its own directory.
- **Anyone can check the log.** Files are named by their digest, so checking one needs no index and no Wake service.
- **Recording never blocks.** The SDK doesn't change. The file is written behind the queue, where the append already happens, and a failed write is retried with the request. Writing the same payload twice writes the same file.
- **Agents never hold keys.** Unchanged. The collector computes the digest from the bytes it received and overwrites the SDK's value, so an agent can't get a digest signed for a payload it didn't send. File names always come from hashing the bytes, never from the event, so one event can't take the name of another's payload.

The trade-offs:

- A payload in a file isn't in the Merkle tree, so the log alone doesn't prove the file exists. Object lock keeps it from being deleted, as it does for the tiles. If it's lost anyway, the log stays valid and the signed digest says exactly what's missing.
- The flip side helps with retention, still open in `wake-platform/docs/provenance-storage.md`: a file can be deleted for an erasure request once its retention ends, and the log stays intact. A payload inside an entry can't be. If erasure becomes a requirement, sending every payload to a file needs no signing change.
- Readers make one extra fetch for each large payload.
- A crash between the file and the entry leaves a file nothing points to. That's harmless, and the retry appends the entry.

## What changes in the signed bytes

`attributes` holds every `wake.*` and `gen_ai.*` attribute except `wake.signer`, any attribute starting with `wake.signature`, and now `wake.payload`. When the event has a payload, `wake.payload.sha256` is in the signed bytes, set by the signer from the payload's UTF-8 bytes.

The log entry gains an optional field:

```json
{"event":{...signed bytes...},"payload":"<the payload, when it fits>","signature":"<base64>","signer":"<name>","valid":true}
```

For each component:

- **Collector:** sets the digest before signing SDK and memory adapter events. For events that arrive signed, a payload that doesn't match the signed digest makes the event invalid, the same as a bad signature.
- **Proxy (not built):** signs the same shape. It sets the digest, leaves the payload out of what it signs, and still sends the payload on the span. How the bytes of a proxy payload that doesn't match its digest are linked from the entry is settled when the proxy is built; they're kept, under their own digest, either way.
- **SDK:** no change. It still sends `wake.payload` and its own digest.
- **Ingester (manikoa/wake-platform#2):** its signature check doesn't change, since it checks the entry's `event` bytes as they are. The payload now comes from the entry or the file instead of the event's attributes, and is checked against the signed digest before it reaches ClickHouse.
- **Existing logs:** only test logs exist, so nothing is migrated.

## Not covered

- **The receiver's 20 MiB request limit.** A batch of events with large payloads, such as many model calls each carrying a long context, can exceed it. The receiver refuses the request and the SDK counts the events as dropped, so it's lossy but counted and never blocks. A follow-up: cap the SDK's batches by bytes, or raise `max_request_body_size`.
- **Bucket storage.** The log writes to local disk today. The `payload/sha256/` layout carries over to the bucket when that lands.

## What was built

- **`log`** (additive, needs a `log/v0.2.0` tag): `Entry.Payload`; `NewEntryWithPayload`, which puts the payload inline or reports that it needs a file; `Log.PutPayload`, which writes `payload/sha256/<hex>` the way Tessera writes tiles (a synced temporary file, linked into place, then the directory synced); and for readers `ReadPayload`, `PayloadPath`, `MaxEntrySize`, `ErrNoPayload`, `ErrPayloadMismatch` and `ErrEntryTooLarge`. `NewEntry` now refuses an entry over the limit.
- **`collector/event`**: `wake.payload` is out of the signed bytes, `Sign` sets the digest from the payload received, `Verify` checks the payload against it, and `Entry` reports when the payload needs a file.
- **`wakelogexporter`**: writes the payload file before appending. An event too large for an entry even without its payload is dropped alone: logged with its `wake.event.id` and counted in `wake_log_dropped_events`, while the rest of the request is appended once.
