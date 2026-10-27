# Wake signed log

The self hosted signed log: an append only, hash chained [tlog-tiles](https://c2sp.org/tlog-tiles) log built on [Trillian Tessera](https://github.com/transparency-dev/tessera), with checkpoints signed by the log's key. The collector's `wakelog` exporter writes to it.

It stores the log on local disk for now. Writing to a customer's bucket with object lock comes when a design partner deploys it.

Generate a key pair with:

```bash
go run ./cmd/wake-keygen -name wake-log -out keys/log
```

## Checking a log

Anyone can check a log without trusting Wake or whoever stores it. `wake-log verify` checks the checkpoint's signature, that every entry is in the signed tree, and every event's signature, and exits 1 if anything doesn't hold. `-entries` also prints each entry as a JSON line, with its payload checked against the event's signed digest.

```bash
go run ./cmd/wake-log verify -log ./data/log -log-key keys/log.pub \
  -key keys/collector.pub -bundle identity/bundle.pem
```

`-log` is the log's directory, or an http(s) URL it's served from. `-key` names an event signer's public key (the collector's), and `-bundle` a trust bundle that X.509-SVID signers (the proxy) must chain to; both repeat. In Go, `wakelog.Verify` does the same with a `DirReader` or Tessera's `client.HTTPFetcher`.

## Reading entries

Each entry holds an event's exact signed bytes, its signature and signer, the signer's certificate if it signs with an X.509-SVID (the proxy), the collector's check at append time, and the event's payload when it fits (the format is in [docs/events.md](../docs/events.md)). A payload too large for its entry is stored next to the tiles at `payload/sha256/<digest>`. `ParseEntry` reads an entry back without touching the signed bytes, `Check` checks the signature again, with the signer's public key or, for an SVID signer, its certificate against the SPIRE trust bundles, `ReadPayload` returns the payload checked against its signed digest, and `Decode` reads the event's fields:

```go
entry, err := wakelog.ParseEntry(data)
ok := entry.Check(wakelog.Trust{Keys: keys, Bundles: bundles}) // don't rely on entry.Valid
payload, err := entry.ReadPayload(func(path string) ([]byte, error) {
	return os.ReadFile(filepath.Join(logDir, path)) // or read the bucket
})
event, err := entry.Decode()
```

`ReadPayload` only calls the fetch function when the payload isn't in the entry. It returns `ErrNoPayload` for an event without one and `ErrPayloadMismatch` when the payload doesn't match the digest.

The collector writes entries with the same code (`Encode` and `Log.PutPayload`), so the format is defined in one place.
