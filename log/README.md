# Wake signed log

The self hosted signed log: an append only, hash chained [tlog-tiles](https://c2sp.org/tlog-tiles) log built on [Trillian Tessera](https://github.com/transparency-dev/tessera), with checkpoints signed by the log's key. The collector's `wakelog` exporter writes to it.

It stores the log on local disk for now. Writing to a customer's bucket with object lock comes when a design partner deploys it.

An entry appended again while the log still remembers it (Tessera's in memory antispam) lands once, so a request the collector retries after part of it was appended doesn't duplicate anything. The log forgets on restart, so an entry the collector's queue replays after a crash can still land twice, as an identical copy.

Generate a key pair with:

```bash
go run ./cmd/wake-keygen -name wake-log -out keys/log
```

## Checking a log

Anyone with a log's public keys can check it without trusting Wake. `wake-log verify` checks the checkpoint's signature, that every entry is in the signed tree, and every event's signature and payload. It checks one checkpoint, so it can't see a log cut back to an earlier one, or rewritten with the log's own key: that takes an earlier checkpoint to compare. `-entries` also prints each entry as a JSON line.

```bash
go run ./cmd/wake-log verify -log ./data/log -log-key keys/log.pub \
  -key keys/collector.pub -bundle identity/bundle.pem -signer proxy=spiffe://wake.local/proxy
```

`-log` is the log's directory, or an http(s) URL it's served from. `-key` names an event signer's public key (the collector's), `-bundle` a trust bundle that X.509-SVID signers (the proxy) must chain to, and `-signer` who may sign a capture, as `<capture>=<SPIFFE ID>` or a prefix ending in `/`. A bundle vouches for every workload in its trust domain, agents included, so an SVID signature only counts for a capture `-signer` lists it under, and `-bundle` needs `-signer`. All three repeat. In Go, `wakelog.Verify` does the same with a `DirReader` or Tessera's `client.HTTPFetcher`; both fall back to a full entry bundle when Tessera has deleted the partial one a checkpoint points to.

Each entry is compared with the collector's check when it appended it, and reported in one of three groups:

- **Logged invalid, and still failing:** evidence the collector recorded, such as an event signed by someone not allowed to. Reported; the log is fine.
- **Logged valid, but failing now:** the entry or its payload changed after it was logged, a payload file is missing, or this check trusts differently from the collector.
- **Logged invalid, but passing now:** this check trusts more than the collector did.

It exits 1 if the log doesn't hold together or any entry is in the last two groups.

## Reading entries

Each entry holds an event's exact signed bytes, its signature and signer, the signer's certificate if it signs with an X.509-SVID (the proxy), the collector's check at append time, and the event's payload when it fits (the format is in [docs/events.md](../docs/events.md)). A payload too large for its entry is stored next to the tiles at `payload/sha256/<digest>`. `ParseEntry` reads an entry back without touching the signed bytes, `Check` checks the signature again, with the signer's public key or, for an SVID signer, its certificate against the SPIRE trust bundles as of when the collector received the event (`CheckSignature` says why it fails), `ReadPayload` returns the payload checked against its signed digest, and `Decode` reads the event's fields:

```go
entry, err := wakelog.ParseEntry(data)
ok := entry.Check(wakelog.Trust{Keys: keys, Bundles: bundles,
	Signers: wakelog.Signers{"proxy": {"spiffe://wake.local/proxy"}}}) // don't rely on entry.Valid
payload, err := entry.ReadPayload(func(path string) ([]byte, error) {
	return os.ReadFile(filepath.Join(logDir, path)) // or read the bucket
})
event, err := entry.Decode()
```

`ReadPayload` only calls the fetch function when the payload isn't in the entry. It returns `ErrNoPayload` for an event without one and `ErrPayloadMismatch` when the payload doesn't match the digest.

The collector writes entries with the same code (`Encode` and `Log.PutPayload`), so the format is defined in one place.
