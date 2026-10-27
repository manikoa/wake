# Where events are signed

Decided October 26, 2026.

## The question

The roadmap says events are "signed at the source" and also that "agents never hold signing keys". The SDK and the memory adapter run inside the agent's process, so they can't do both.

## The decision

Events are signed at the **first hop outside the agent's process**:

- **SDK and memory adapter events** are sent unsigned over TLS to the out of band collector. The collector's Go processor signs them on arrival.
- **Proxy events** are signed by the proxy itself. It runs as a separate process with its own short lived SPIFFE identity, so it is a source agents can't reach.
- The collector checks the proxy's signatures, then appends every signed event to the log.

No component that runs inside an agent's process ever holds a signing key.

## Why

A key inside the agent's process is one a compromised agent can use to forge signed events for as long as the key lives. That breaks the first principle: agents never hold the keys to their own record. Signing at the first hop the agent can't control keeps the guarantee that matters: once an event leaves the agent, nobody can change it.

## The trade-off

An SDK event is only as honest as the agent process that emitted it. That's true of any capture that runs inside the agent, signed or not. The proxy's own signed record of the same MCP and A2A calls is the independent check: calls the proxy saw but the SDK didn't report, or reported differently, are a signal worth raising.

## What it means for each component

- **SDK and memory adapter:** never sign anything and never load key material. Send over TLS to the collector.
- **Proxy:** signs each event it captures with its SPIFFE identity's key before sending it.
- **Collector:** signs unsigned SDK and memory adapter events, verifies proxy signatures, and records events whose signatures fail instead of dropping them.
- **Keys:** the collector's signing key lives in cloud KMS, or keys the customer holds. A self hosted log can use a local key file.

The exact bytes that get signed are defined in [events.md](events.md).
