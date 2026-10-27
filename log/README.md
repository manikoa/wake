# Wake signed log

The self hosted signed log: an append only, hash chained [tlog-tiles](https://c2sp.org/tlog-tiles) log built on [Trillian Tessera](https://github.com/transparency-dev/tessera), with checkpoints signed by the log's key. The collector's `wakelog` exporter writes to it.

It stores the log on local disk for now. Writing to a customer's bucket with object lock comes when a design partner deploys it.

Generate a key pair with:

```bash
go run ./cmd/wake-keygen -name wake-log -out keys/log
```

## Reading entries

Each entry holds an event's exact signed bytes, its signature and signer, and the collector's check at append time (the format is in [docs/events.md](../docs/events.md)). `ParseEntry` reads one back without touching the signed bytes, `Verify` checks the signature again with the signer's public key, and `Decode` reads the event's fields:

```go
entry, err := wakelog.ParseEntry(data)
ok := entry.Verify(trusted) // don't rely on entry.Valid
event, err := entry.Decode()
```

The collector writes entries with the same code, so the format is defined in one place.
