---
name: wake-record
description: Guide for building Wake's Record phase exactly as the roadmap describes it. Covers the open source Python and TypeScript SDKs for the Claude Agent SDK, OpenAI Agents SDK and LangGraph; the MCP and A2A proxy on agentgateway; the memory adapter and origin labels; OpenTelemetry Collector ingestion; the Trillian Tessera signed log; the Postgres and ClickHouse provenance graph; and the provenance view. Use this whenever working on capture, instrumentation, event fields, signing, trace stitching across agents, origin labels, the signed log, provenance queries or the provenance view in Wake.
---

# Building the Record phase

Read the `wake-architecture` skill first for the boundaries and principles. Check the `wake-scope` skill to confirm the Validate gate is cleared before writing product code.

## What ships, and what counts as done

**Ships (months 1 to 3):** open source SDKs for the Claude Agent SDK, OpenAI Agents SDK and LangGraph; an MCP and A2A proxy built on agentgateway; the memory adapter; the signed log; the provenance view.

**Gate:** live in 3 partners' staging, traces stitched across agents, and one real issue caught.

**Metric:** over 95% of agent actions have complete provenance across hops for design partners.

Everything below serves that gate. If a task doesn't move a partner closer to running Wake in staging with stitched traces, it probably belongs to a later phase.

## Build a thin end to end slice first

The gate is about partners running the whole loop, so get one path working end to end before widening it:

1. One SDK, for the framework and language the first design partner uses.
2. The collector with the Go processor that checks signatures and extracts provenance.
3. The signed log in a bucket with object lock.
4. Postgres edges and ClickHouse events.
5. The provenance view, showing what led to one action.

Then add the proxy (it covers agents without an SDK and every MCP and A2A call), the memory adapter, and the remaining frameworks and languages, in the order partners need them.

## What to capture

Capture every model call, tool call, message between agents, and memory read or write. Start each event with only the fields the provenance view and the gate need:

- Event id, plus OpenTelemetry trace and span ids.
- The agent's identity and which capture component recorded the event (SDK, proxy or memory adapter).
- The kind of event: model call, tool call, agent message, memory read or memory write.
- A timestamp.
- The payload, or a digest of it with the raw content kept in ClickHouse.
- The origin label: where the content came from, such as an email, a web page, another agent or a memory entry.
- Causal parents: the ids of the events whose output fed this one.

Add a field when the view or a partner needs it, not before.

## Origin labels and causal edges

The roadmap's promise for Record is: click any action and see the email, page or agent message behind it. Coarse edges are enough for that:

- Everything in a model call's context is a parent of that call.
- A tool call is a child of the model call that produced it. A tool result, such as an email read or a page fetch, carries an origin label for its source.
- A message from agent A to agent B is a child of the step in A that produced it and a parent of B's next step.
- A memory write is tagged with the origin labels of its parents **before it lands in the store**, so a later read, even in another session or by another agent, knows where the entry came from without asking the graph. A memory read links to the write that produced the entry.

An event's origins are the union of its ancestors' labels, computed with a Postgres recursive query. Tracking individual pieces of data and enforcing flow policies on them is Contain work, not Record work.

## Stitching traces across agents

Nothing standard carries trace context across A2A handoffs yet (A2A issue 2103), so Wake stitches traces itself. The SDK and the proxy both inject W3C trace context and the parent event id into outgoing A2A message metadata and extract them on receipt. The proxy does this for agents that have no SDK. Switch to the standard once one lands.

Build the 95% metric early as a query: the share of action events with an unbroken chain back to their origins. It's the gate metric, and it shows where stitching breaks.

## Signing and the log

- Sign each event where it's captured, then append it to a Trillian Tessera log that writes tiles to the customer's bucket with object lock. Tessera's Merkle tree and signed checkpoints provide the append only, hash chained property. Don't build a second chain next to it.
- The collector's Go processor checks signatures before anything reaches Postgres or ClickHouse, and records events that fail the check instead of silently dropping them.
- Before writing signing code, settle the "where signing happens" question in the `wake-architecture` skill, because the SDK runs inside the agent's process and agents must never hold signing keys.
- The open source package includes a self hosted signed log, so a team can run the recorder with no Wake account.

## Recording never blocks

SDK and memory adapter code wraps framework hooks so a recorder failure can never break or slow the agent. Send events asynchronously from a bounded buffer. When the buffer fills or the collector is unreachable, drop events, count the drops, and raise a health signal that pages someone. Never raise exceptions into agent code. Test this by killing the collector during an agent run.

## The proxy

Build on agentgateway rather than writing a gateway. Wake adds event signing and capture in Record, and the deny lever in Brake. Prefer agentgateway's extension points, or contributing upstream, over a long lived fork, and check its current docs for how extensions work. Fuzz the proxy's parsing of MCP and A2A traffic from the start, because it's where attackers will poke.

## The memory adapter

Write thin wrappers in the SDKs for the memory stores and memory files the design partners actually use. Memory files matter: the July 2026 Bad Memory study showed planted memory files attacking later Claude Code and Codex sessions. Every write gets an origin label before it lands, and every read is recorded.

## The provenance view

Use Next.js and React Flow in a Turborepo. The view does one thing: starting from an action, show the chain of events and origins behind it, down to the email, page or agent message. Showing what an action affected downstream is the Brake phase's downstream view; don't build it yet.

## Releases

The recorder is open source and free forever. From the first public release, sign releases with Sigstore and publish SLSA provenance and an SBOM. Security teams need to read exactly what runs in their stack, and Wake will be a target.

## Not in Record

The brake and the deny lever, the downstream view, detection rules and risk scores, evidence packs, flow policies, memory rollback and policy compilation all come later. If a partner asks for one, note the request for that phase's planning and offer what Record can show today.

## Gate checklist

- [ ] Running in 3 design partners' staging environments
- [ ] Traces stitched across at least one agent to agent hop for each partner
- [ ] Over 95% of actions with complete provenance, measured by the query above
- [ ] One real issue caught and written up with the provenance view
- [ ] Signed releases, SLSA provenance and SBOMs published for the open source recorder
