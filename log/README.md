# Wake signed log

The self hosted signed log: an append only, hash chained [tlog-tiles](https://c2sp.org/tlog-tiles) log built on [Trillian Tessera](https://github.com/transparency-dev/tessera), with checkpoints signed by the log's key. The collector's `wakelog` exporter writes to it.

It stores the log on local disk for now. Writing to a customer's bucket with object lock comes when a design partner deploys it.

Generate a key pair with:

```bash
go run ./cmd/wake-keygen -name wake-log -out keys/log
```
