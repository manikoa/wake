---
name: wake-architecture
description: Wake's architecture boundaries, six principles and stack picks, taken from the product roadmap. Use this whenever designing or changing any Wake component; choosing a language, library, database, queue or cloud service; deciding where code runs (in the agent stack or out of band); handling keys, identities or signing; deciding what happens when something fails; or writing a design note for Wake, even if the user doesn't say "architecture".
---

# Wake architecture

Wake is a thin capture layer inside the customer's agent stack plus an out of band service the agents can't touch. That service records, traces, brakes, contains and proves. Every rule below protects one promise: **agents never hold the keys to their own record or brake.** A recorder or brake that an agent can reach is one it can switch off, which is exactly what happened when OpenAI's internal agents disabled their own monitoring.

## The two zones

**In the agent stack (capture only):**
- An SDK in each agent that records model calls, tool calls and handoffs.
- An MCP and A2A proxy that sees every call between agents and their tools.
- A memory adapter that records every read and write and tags each write with its origin.

**Out of band (agents hold no keys here):**
- The signed log: append only and hash chained, kept in the customer's bucket or a transparency log.
- The provenance graph: what influenced what, with origin labels that follow data across agents and memory.
- Detection: rules, risk scores and monitors that read handoffs.
- The actions: Brake, Contain and Prove.

To place a new piece of code, ask: **could an agent, with the credentials and access it has, change or stop this?** If yes, it can only capture. Recording, judging and stopping live where agents can't write. The proxy runs inside the agent stack, but as a separate process. Agents call through it and hold no credentials to its config or keys.

## The six principles, and what they mean in code

1. **Out of band, always.** Capture components get short lived SPIFFE identities from SPIRE. Signing keys live in cloud KMS (or keys the customer holds), never in agent environment variables or memory. The brake's credentials for each identity provider and sandbox API never pass through the agent stack.

2. **The customer holds the evidence.** The log writes to the customer's own bucket with object lock (S3 Object Lock, GCS retention lock, Azure immutable storage) or is anchored in a transparency log. Wake's own services must not be able to rewrite history, and anyone must be able to check the log without trusting Wake.

3. **Orchestrate, don't replace.** Pull the levers platforms already expose: proxy deny, OAuth token revocation, Shared Signals, identity provider disable APIs, sandbox APIs. Wake never becomes another identity provider or gateway. The proxy is agentgateway with Wake's additions, not a new gateway.

4. **Recording never blocks; the brake fails closed.**
   - Recording: SDK and adapter code never raises into agent code and never adds blocking work to the agent's call path. Use a bounded buffer and send asynchronously. If the recorder is down or events are dropped, the agent keeps working and a health signal pages someone.
   - Brake: a lever counts as fired only when it is positively verified. If the brake can't confirm a lever fired, it treats that path as live: it shows it as live, keeps retrying, and never reports the fleet as stopped.

5. **Open source at the core.** The open source recorder is the SDKs, the proxy, the memory adapter and a self hosted signed log, all under Apache 2.0 (the same license as agentgateway). Only the recorder lives in this repo. The hosted log, the dashboard (provenance and downstream views), the brake and the control plane live in a separate private repository. See `docs/repo-layout.md` for the folders and rules.

6. **Score, don't convict.** Persistent problem solving and malice look alike. Detection raises a risk score and pages a human. It brakes automatically only when the customer's policy says to stop. The UI and API describe agents as risky or anomalous, never as malicious.

## Stack picks

Follow the architecture from top to bottom. Each pick comes with the roadmap's reason; reach for something else only when a design partner hits a real limit.

| Part | Pick | Why |
|---|---|---|
| SDK in each agent | Python and TypeScript, built on OpenTelemetry | Where agent frameworks live; standard spans plus Wake's provenance fields |
| MCP and A2A proxy | Rust, built on agentgateway | Apache licensed Rust proxy in the Agentic AI Foundation that already speaks MCP and A2A; Wake adds signing and the brake |
| Memory adapter | Thin wrappers in the SDKs for common memory stores and memory files | Every write gets an origin label before it lands |
| Ingestion | OpenTelemetry Collector with a custom Go processor | Batching, retries and exporters for free; signatures checked and provenance extracted there |
| Signed log | Trillian Tessera, writing to the customer's bucket with object lock | The tile based log library under Sigstore's Rekor v2; cheap and checkable by third parties |
| Provenance graph | Postgres for metadata and causal edges, ClickHouse for raw events | Recursive queries cover the graph until a graph database is clearly needed; ClickHouse handles trace volume |
| Detect | Cedar policies in the proxy, rules over ClickHouse, out of band monitors from more than one lab | Cedar is analyzable and is what AWS AgentCore Policy uses |
| Brake | Temporal workflows with Go workers calling each lever | Durable fan out with retries and verification, so a revocation never fails silently |
| Identity and keys | SPIFFE/SPIRE plus cloud KMS, with customer held keys as an option | Short lived identities for capture components; agents never hold signing keys |
| Dashboard | Next.js and React Flow in a Turborepo | Familiar from Clavr; React Flow draws the provenance graph |
| Deployment | Containers, Helm and Terraform; hosted control plane on GCP (Cloud Run, Cloud SQL) | Customers run the data plane in AWS, Azure or GCP |
| Shipping safely | Sigstore signed releases, SLSA provenance, SBOMs, a fuzzed proxy, an early SOC 2 | Wake becomes the most attractive target in every customer's stack |

That's four languages, each where it's strongest: Rust for the proxy, Go for the collector and the brake, TypeScript and Python for the SDKs and the dashboard. Don't add a fifth. Use managed ClickHouse and Temporal until customers need everything self hosted. Skip Kafka, a graph database, your own identity provider and your own policy language.

For OpenTelemetry, use the GenAI semantic conventions where they exist (they're still marked Development) and keep Wake's provenance fields in their own attribute namespace, so they can move onto the standard when it settles.

## Questions the roadmap leaves open

The roadmap doesn't settle these. Don't resolve them silently in code; write a short decision note in `docs/` and confirm it with the user first.

- **Where signing happens.** The roadmap says events are "signed at the source" and also that "agents never hold signing keys". The SDK runs inside the agent's process, so decide how both can hold, for example by signing in a capture component outside the agent's process or with a per component SPIFFE identity, and write down why.
- **Whether the log defaults to Wake's hosting or the customer's bucket.** This is an open question for design partners. Support the customer's bucket from the start, because principle 2 depends on it.
