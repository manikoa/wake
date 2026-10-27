---
name: wake-review
description: Reviews a Wake change, branch, pull request or design against the product roadmap, covering phase scope, the six principles, the stack picks and overengineering. Use when asked to review, check or sanity check Wake code or a design; before opening a pull request in the Wake repo; or when someone asks "does this match the roadmap", "is this overengineered" or "did we drift".
---

# Wake review

The goal is to catch drift from the roadmap while it's still cheap to fix: building the wrong phase, breaking a principle that the product's credibility rests on, or adding machinery nobody needs yet. Style nits are out of scope; linters handle those.

## Gather context

1. Read the "Current phase" section of `CLAUDE.md`.
2. Get the change. Use the diff or pull request the user named, or `git diff main...HEAD` plus any uncommitted changes.
3. Skim the relevant skill for the area touched: `wake-record`, `wake-brake` or `wake-architecture`.

## What to check, most severe first

**Blockers.** Each of these breaks a promise the product makes:

- Product code while `CLAUDE.md` doesn't record the Validate gate as cleared.
- An agent can reach the keys, config or credentials for its own record or brake. Look for signing keys in agent environment variables, brake credentials in the agent stack, or proxy config an agent can write.
- Recording can block, slow or crash the agent: synchronous sends on the agent's call path, unbounded buffers, or exceptions that can escape into agent code.
- The brake reports success without positive verification, or treats a timeout or error as stopped instead of live.
- Wake's own services, or agents, can modify or delete evidence: no object lock, mutable log storage, or update and delete paths on recorded events.
- Brake logic running inside an agent's process.

**Scope:**

- Features from a later phase. Name the phase and the gate.
- Items the roadmap says to skip: Kafka, a graph database, your own identity provider, your own policy language, self hosted ClickHouse or Temporal before a customer needs it.
- A new language, database, queue or framework beyond the stack table.
- Code that settles an open roadmap question without a decision note.

**Overengineering:**

- Interfaces, registries or plugin layers with one or two implementations.
- Config options, flags or settings for cases nobody has asked for.
- Generic multi-cloud, multi-tenant or high availability scaffolding ahead of need.
- Reimplementing something the stack already provides, such as batching and retries the OpenTelemetry Collector does, durability Temporal gives, or chaining Tessera handles.

For each finding, suggest the concrete deletion or simplification.

**Shipping safely**, for proxy and release changes:

- New parsing paths in the proxy have fuzz targets.
- Release workflows keep Sigstore signing, SLSA provenance and SBOMs.

**Language:** detection and UI wording describes risk scores and anomalies and never calls an agent malicious ("score, don't convict").

## Output

List the findings, most severe first. For each one, give:

- `file:line`
- What's wrong, in one sentence
- The roadmap section or principle it conflicts with
- The smallest fix

If nothing turns up, say so in one line. Don't pad the review with minor points to look thorough.
