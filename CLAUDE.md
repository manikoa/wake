# Wake

Wake is security software for AI agents. The source of truth for what to build is [docs/product-roadmap.md](docs/product-roadmap.md). When code and the roadmap disagree, the roadmap wins until someone changes it on purpose.

## Current phase

- Phase: Record (months 1 to 3), started October 26, 2026
- IP release from Microsoft: in hand (recorded October 26, 2026)
- Record started early, by founder decision, before the rest of the Validate gate was met. Still open from Validate: 5 teams with a real incident or stalled deal, and 3 design partners committed. Keep running interviews alongside Record.
- First slice: Python and LangChain, with Clavr as the first test bed. Clavr builds its agents as plain Python around LangChain models and tools, not LangGraph (checked October 26, 2026), so the SDK records it through LangChain callbacks with named agent runs.
- LangGraph stays in the slice, with a design partner running LangGraph as its test bed. Still to find.
- Building ahead, by founder decision (October 27, 2026): finish the rest of Record's code (SDK coverage, proxy loose ends, release safety), then build Brake, Prove and Contain, before the Record gate is met. The gates still stand as the business tests: partners live in staging, paid pilots and customers. Deferred for now: the log in the customer's bucket, keys in KMS and SVIDs from SPIRE, and Clavr's wiring.
- Roadmap updated October 28, 2026, by founder decision, after reviewing the Least Autonomy and AgentLeak papers: sub-agent instances, the agent behind delegated tokens and secrets stripped before signing join Record; stopping one sub-agent, agent owners and a census join Brake; credential misuse and leak detection join Prove; run charters and separation of duty checks join Contain. No gate moved. Built ahead and merged (October 29, 2026): the Record items, and with them halting one sub-agent, in its own process too in every integration, owners paged and the census (Brake); credential and leak detection, and a leak game day (Prove); and run charters, and separation of duty checks over the influence seen between agents, with the paper's autonomy bound reported from what agents were seen doing (Contain). Enforcing limits on reachable authority waits for Platform.
- Added to Record by founder decision (October 29, 2026): `wake-hook` (`hook/`), which records coding agents that have no SDK, namely Claude Code, Codex, Cursor, Grok Build, Gemini CLI and Antigravity, through their hooks, and refuses what a halt covers. See `docs/hooks.md`.
- Built ahead, by founder decision (October 30, 2026): flow policies applied in the SDKs' own process too, to the calls that never reach wake-proxy, with the origins the SDKs see enter the agent, memory reads included, from the policy wake-proxy serves; rules for memory stores, which the memory adapters apply to writes; hosted tools noted, and checked before the model calls that offer them, hosted MCP calls the app approves included; model calls through the gateway read by wake-proxy, which checks the tools each lets the provider run and notes what ran, Claude's server tools included; what entered an agent with every origin carried on with its messages; the same check in `wake-hook` for coding agents, Cursor's subagents followed from 3.22; and the reference fleet's agents kept off the internet, model calls included, so agentgateway is their only way out, with a flow policy in its scenario (`docs/flow-everywhere.md`).
- Test bed: Clavr isn't in production, so don't rely on it alone. A reference fleet in `examples/` (agents on several frameworks, talking over A2A and MCP through agentgateway) is the end-to-end test and the game day target.

Update these lines as each part of a gate is met and when the phase changes. The `wake-scope` skill reads them.

## Repository

This public repository holds only the open source core. The paid parts live in the private repository `usewake/wake-platform`, checked out next to this one at `../wake-platform`. Folders and rules are in [docs/repo-layout.md](docs/repo-layout.md).

## Skills

- `wake-scope`: what may be built now and what waits. Use before starting any feature.
- `wake-architecture`: boundaries, the six principles and the stack picks.
- `wake-record`: building the Record phase (SDKs, proxy, memory adapter, signed log, provenance view).
- `wake-brake`: building the Brake phase (levers, verification, downstream view, game days).
- `wake-review`: reviewing a change or design against the roadmap.
