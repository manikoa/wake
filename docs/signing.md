# Where events are signed

Decided October 26, 2026.

## The question

The roadmap says events are "signed at the source" and also that "agents never hold signing keys". The SDK and the memory adapter run inside the agent's process, so they can't do both.

## The decision

Events are signed at the **first hop outside the agent's process**:

- **SDK and memory adapter events** are sent unsigned over TLS to the out of band collector. The collector's Go processor signs them on arrival. So are the events `wake-hook` captures through a coding agent's hooks, on the agent's machine ([hooks.md](hooks.md)).
- **Proxy events** are signed by the proxy itself. It runs as a separate process with its own short lived SPIFFE identity, so it is a source agents can't reach.
- The collector checks the proxy's signatures, then appends every signed event to the log.

No component that runs inside an agent's process ever holds a signing key.

## Why

A key inside the agent's process is one a compromised agent can use to forge signed events for as long as the key lives. That breaks the first principle: agents never hold the keys to their own record. Signing at the first hop the agent can't control keeps the guarantee that matters: once an event leaves the agent, nobody can change it.

## The trade-off

An SDK event is only as honest as whatever sent it. That's true of any capture that runs inside the agent, signed or not, and it goes further here: the collector authenticates no sender, so anything that reaches it can have an unsigned event signed in any agent's name, and logged valid. A compromised agent can send one as another agent. So the collector's signature on an SDK, memory adapter or hook event says it received those bytes, and that they haven't changed since. It doesn't say who sent them, or that it happened. The proxy's own signed record of the same MCP and A2A calls is the independent check: calls the proxy saw but the SDK didn't report, or reported differently, are a signal worth raising, and only the proxy's record can stop an agent.

Binding a sender to the agent it names isn't built: the collector would need each sender's identity, which the SDKs, `wake-hook` and the proxy don't present to it today.

## What it means for each component

- **SDK, memory adapter and `wake-hook`:** never sign anything and never load key material. Send over TLS to the collector.
- **Proxy:** signs each event it captures with its SPIFFE identity's key before sending it. "The proxy" means agentgateway plus the Wake processor it calls; the processor holds the key.
- **Collector:** signs unsigned SDK, memory adapter and hook events, verifies proxy signatures, and records events whose signatures fail instead of dropping them. Before signing an event, it redacts the credentials it recognizes in its payload (see Secrets in [events.md](events.md)), and it redacts those of the events it records as invalid too. The proxy does the same before signing its own: the collector can't change an event the proxy validly signed without breaking its signature.
- **Keys:** the collector's signing key lives in cloud KMS, or keys the customer holds. A self hosted log can use a local key file.
- **Proxy keys:** the proxy signs with its own X.509-SVID key, ECDSA P-256. The SVID travels with each event and into its log entry. The collector checks it against the SPIRE trust bundles in its `trust_bundles` config, as of when it received the event, and only accepts the SPIFFE IDs its `trusted_signers` config lists for the event's capture: agents' SVIDs chain to the same bundle. See [proxy.md](proxy.md).

The exact bytes that get signed are defined in [events.md](events.md).
