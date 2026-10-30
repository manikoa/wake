---
name: wake-scope
description: Decides what may be built in the Wake repo right now and what has to wait, based on the current roadmap phase and its gate. Use this before starting any feature, component, integration, refactor or infrastructure work in Wake; whenever someone says "let's add", "should we build", or proposes a new dependency, service or abstraction; when planning a sprint or breaking a phase into tasks; and whenever work seems to reach past the current phase (evidence packs, detection rules, flow policies, rollback, policy compilation, Kafka, a graph database, multi-region, plugin systems).
---

# Wake scope

The roadmap in `docs/product-roadmap.md` is staged on purpose: each phase is a cheap test before the next one spends real money and time. Building ahead of the current phase spends that money before the test result is in. The most useful thing this skill does is say "not yet" early, name the gate that would unlock the work, and offer the smallest version that fits now.

## Step 1: Find the current phase

Read the "Current phase" section of `CLAUDE.md` at the repo root. If it's missing or unclear, ask the user which phase they're in instead of guessing, because the answer changes what's allowed.

## Step 2: No product code before the IP release

The first gate includes a written release of IP ownership from Microsoft, or an exit. Until then the company may not own what it builds: Washington's RCW 49.44.140 doesn't protect inventions that relate directly to the employer's business, and Microsoft sells agent governance (Agent 365, Entra Agent ID, the Agent Governance Toolkit). The roadmap ranks this as the first risk.

If `CLAUDE.md` doesn't record the IP release as in hand and the task is product code (SDKs, proxy, collector, signed log, brake, dashboard), say so plainly and ask whether the release is in hand before writing it. The release makes product code legally safe to write; it doesn't start the Record phase. Record work still waits for the whole Validate gate (step 3). Work that isn't product code is fine during Validate: docs, research notes, interview scripts, the Clavr game day plan, design partner material and the trademark check.

The roadmap also says to use no Microsoft devices, accounts or tools for Wake work. Raise it if a task would.

## Step 3: Check the task against the phase

| Phase | Months | What ships | Gate to move on |
|---|---|---|---|
| Validate | 0 to 1 | Written release from Microsoft or an exit plan, 20 interviews, a game day on Clavr, a trademark check on Wake, 3 design partners chosen | Release in hand; 5 teams with a real incident or stalled deal; 3 design partners committed |
| Record | 1 to 3 | Open source SDKs for the Claude Agent SDK, OpenAI Agents SDK and LangGraph; MCP and A2A proxy on agentgateway; hooks for coding agents with no SDK (Claude Code, Codex, Cursor, Grok Build, Gemini CLI, Antigravity); memory adapter; signed log; provenance view; sub-agent instances and what started them; the agent behind a delegated token named; secrets stripped before signing | Live in 3 partners' staging; traces stitched across agents; one real issue caught |
| Brake | 3 to 6 | Brake with three levers (proxy deny, token revocation at the top providers, sandbox freeze on E2B or Modal), downstream view, memory quarantine, first paid pilots; halting one sub-agent while its run goes on, saying so when it can't be stopped alone; an owner for every agent and a census of agents seen against the inventory | A partner's fleet stopped and verified in under a minute during a game day; 2 to 5 paid pilots |
| Prove | 6 to 12 | Evidence packs (OWASP Agentic Top 10, EU AI Act, AIUC-1, AI questionnaires), trust pages, 10 or more levers, detection rules (including credentials used by the wrong agent and sensitive data in messages, tool calls and memory), a leak game day, self serve Team plan, SOC 2 Type II | 20 paying customers; about $500K ARR; a security review closed with an evidence pack |
| Contain | 12 to 18 | Origin labels and flow policies that stop untrusted content triggering privileged actions, carried to the sub-agents an agent starts; memory integrity with gated writes and rollback; blast radius replay; run charters (never touch, budget, checks that say it's done, then credentials retired); separation of duty checks over observed influence; private deployment; SIEM export | 50 customers; about $3M ARR; an enterprise contract over $150K; a financial services customer through an AI vendor |
| Platform | 18 to 36 | Policy compiled to each platform (Cedar, Agent 365, Google); consent and delegation for long running agents, with token lineage from the clouds' audit logs; limits on the authority an agent reaches through the agents it influences; insurer and auditor integrations; detection trained on incidents across customers | An insurer or auditor accepts the records as evidence; $10M or more ARR |

Put the task in one of these buckets:

- **In phase.** It's named in "What ships" for the current phase. Build it.
- **Needed for the gate, not named.** For example, a minimal deploy so a partner can run Wake in staging during Record. Build the smallest version that serves the gate.
- **Later phase.** Say which phase owns it and which gate unlocks it. Offer the in-phase slice if there is one. For example, during Record a partner who wants "audit evidence" gets a raw export of their signed log, not an evidence pack.
- **Not on the roadmap.** Say so, and don't build it unless the user decides to change the plan. If they do, suggest updating `docs/product-roadmap.md` in the same change so the doc stays the source of truth.

## Things the roadmap says to skip

The stack section says to skip Kafka, a graph database, your own identity provider or token service, and your own policy language for now, and never to let the brake run inside an agent's own process. It says to use the managed versions of ClickHouse and Temporal until customers need everything self hosted, and to rely on Postgres recursive queries for the provenance graph until a graph database is clearly needed.

"Clearly needed" means a design partner hit a measured limit, not that it might matter at scale. When someone reaches for a skipped item, answer with the roadmap's pick and its reason (see the `wake-architecture` skill).

## Signs of overengineering

These usually mean the work has drifted past what the phase needs:

- Abstractions for frameworks, levers, memory stores or clouds nobody has asked for. Record ships three framework SDKs and Brake ships three levers; the fourth comes when a partner needs it.
- Plugin systems, generic adapter layers or config languages built before there are three concrete cases to generalize from.
- Multi-tenancy, multi-region, high availability or self hosting work before a customer asks for it.
- Writing your own gateway, log, policy language, workflow engine, identity system, token service or credential broker. The stack table names a proven pick for each; the customer's identity provider issues every agent's and sub-agent's credentials.
- Screens beyond the one view the phase ships: the provenance view in Record and the downstream view in Brake.
- Detection, risk scoring or machine learning before Prove.

## Leave open questions to design partners

The roadmap lists questions to answer with design partners instead of guessing: who owns the budget, whether the log lives with Wake or in the customer's bucket by default, which three levers matter most, which pricing meter to use, whether platforms allow fleet scale revocation, which evidence formats auditors accept, how much detection Wake should own, and when a setup needs containment across agents.

When a task depends on one of these, don't settle it in code. Build the smallest thing that keeps both answers possible, and say which question is still open.

## Output

Answer in a few lines: the current phase, the verdict (in phase, needed for the gate, later phase, not on the roadmap, or blocked by the IP release), the reason with the roadmap section it comes from, and the smallest in-phase version if there is one.
