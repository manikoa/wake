---
name: wake-brake
description: Guide for building Wake's Brake phase exactly as the roadmap describes it. Covers halting an agent, a workflow or a whole fleet with Temporal workflows and Go workers; the three levers (proxy deny, token revocation at the top identity providers, sandbox freeze on E2B or Modal); verifying each lever fired; memory quarantine; the downstream view; and game days that time a verified stop. Use this whenever working on the kill switch, halting, revocation, lever integrations, verification, quarantine, the downstream or blast radius view, or game day tooling in Wake.
---

# Building the Brake phase

Read the `wake-architecture` skill first for the boundaries and principles, and confirm with the `wake-scope` skill that the project is in the Brake phase.

The brake, its Temporal workers and the downstream view are paid parts, so they're built in the private repository (see `docs/repo-layout.md`). Only the proxy's deny support and the memory adapter's quarantine support go in this public repo, because they live in the open source recorder.

## What ships, and what counts as done

**Ships (months 3 to 6):** a brake with three levers (proxy deny, token revocation at the top providers, sandbox freeze on E2B or Modal), the downstream view, memory quarantine, and the first paid pilots.

**Gate:** a partner's fleet stopped and verified in under a minute during a game day, and 2 to 5 paid pilots.

**Metric:** time to stop a fleet, verified. Under 60 seconds by this gate, and under 10 seconds by Contain.

The promise is a stop that reaches every vendor, with proof that it worked. Reaching vendors comes from the levers; proof comes from verification. Speed is the number the gate measures.

## The shape of a halt

- A halt has a scope: one agent, one workflow, or the whole fleet.
- In this phase a human starts it: the on-call engineer, from the dashboard or an API call. Detection rules that trigger the brake arrive in Prove, and even then they brake automatically only when the customer's policy says stop.
- Each halt is one Temporal workflow. It works out which lever applies to which target in scope, then fans out one activity per lever and target. Each activity pulls its lever and then verifies it. Go workers run the activities.
- The result is per path: **confirmed stopped** or **live**. A path is confirmed only after positive verification. A timeout, an error or an unclear answer means live. The workflow keeps retrying live paths and shows them as live until they're confirmed or a human steps in. The brake never reports a fleet as stopped while any path is unconfirmed.
- Record the timestamps for halt issued, each lever pulled and each lever verified. Time to a verified stop is the core metric, so measure it on every halt, not just during game days.
- The brake runs out of band and never inside an agent's process. Its credentials for identity providers and sandbox APIs never pass through the agent stack. Prefer credentials the customer owns.

## The three levers

Which identity providers and sandbox platforms come first is an open question for design partners. Build the ones the first partners use, and don't add a generic lever framework until there are more than three levers. Ten or more levers is Prove work.

**1. Proxy deny.** The brake tells the proxy to deny calls in scope. To verify, confirm the proxy acknowledged the deny for that scope, then check that no call in scope was allowed after the halt time. The signed log shows that. The proxy deny is also the backstop for tokens that outlive revocation.

**2. Token revocation at the identity provider.** Use the levers providers already expose: OAuth token revocation (RFC 7009), Shared Signals and CAEP events, and provider disable APIs such as Okta Universal Logout for AI agents and Entra's agent identity disable. To verify, use token introspection (RFC 7662) or a test call that must fail. Remember the roadmap's warning that tokens already issued may live on: revoking a refresh token doesn't kill a self contained access token before it expires. If revocation can't be confirmed for a token, treat that path as live, show the token's remaining lifetime, and rely on the proxy deny for that path.

**3. Sandbox freeze on E2B or Modal.** Use the platform's own API to stop the sandbox. "Freeze" means stopping the sandbox while keeping its state for investigation where the platform supports that, rather than deleting it. Check the platform's current docs for what's available. To verify, query the sandbox's status until it reports stopped.

## Memory quarantine

Lock memory and quarantine entries so agents stop reading poisoned memory. Two moves are enough here: the memory adapter refuses writes in scope, and entries matching a compromised origin label or agent are no longer served to agents. Keep quarantined entries rather than deleting them, since they're evidence. Verify by attempting a read and a write in scope. Gated writes and rollback are Contain work.

## The downstream view

Starting from a compromised agent or a poisoned memory entry, show everything downstream: every event, agent and memory entry it influenced. It's the same Postgres causal edges as the provenance view, walked in the other direction with a recursive query, drawn in React Flow. Automating quarantine, rotation and undo across everything downstream, and blast radius replay, are Contain work.

## Game days

Game days prove the gate, and the roadmap uses them to win paid pilots.

1. In a partner's staging environment, simulate a compromised agent. The first game day happens on Clavr during Validate.
2. Start a fleet halt and start the clock.
3. Record when each lever was pulled and verified, and keep trying to make an agent act after the halt. Every attempt should fail.
4. Stop the clock when every path is confirmed. Any path still live counts against the result. Don't round it down.
5. Write a one page report: time to a verified stop, the timing of each lever, anything still live and why, and what to fix.

## Pricing test

The roadmap says to test the pricing meter during this phase: per protected agent for some design partners and per million events for others. Count protected agents and events per partner from the start. That's all the metering this phase needs; billing is Prove work.

## Not in Brake

More than three levers, detection rules, evidence packs, the self serve Team plan, flow policies, memory rollback, blast radius replay and policy compilation all belong to later phases.

## Gate checklist

- [ ] Three levers working: proxy deny, token revocation at the partners' identity providers, sandbox freeze on E2B or Modal
- [ ] Every lever verified, with unconfirmed paths reported as live
- [ ] Memory quarantine and the downstream view working
- [ ] A game day where a partner's fleet is stopped and verified in under 60 seconds, with a written report
- [ ] 2 to 5 paid pilots, split between the two pricing meters
