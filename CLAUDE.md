# Wake

Wake is an independent recorder and brake for multiagent AI systems. The source of truth for what to build is [docs/product-roadmap.md](docs/product-roadmap.md). When code and the roadmap disagree, the roadmap wins until someone changes it on purpose.

## Current phase

- Phase: Validate (months 0 to 1)
- Validate gate: not recorded as cleared. The gate is a written IP release from Microsoft (or an exit), 5 teams with a real incident or stalled deal, and 3 design partners committed.

Update these two lines whenever a gate is passed. The `wake-scope` skill reads them.

## Repository

This public repository holds only the open source recorder. The paid parts live in the private repository `manikoa/wake-platform`, checked out next to this one at `../wake-platform`. Folders and rules are in [docs/repo-layout.md](docs/repo-layout.md).

## Skills

- `wake-scope`: what may be built now and what waits. Use before starting any feature.
- `wake-architecture`: boundaries, the six principles and the stack picks.
- `wake-record`: building the Record phase (SDKs, proxy, memory adapter, signed log, provenance view).
- `wake-brake`: building the Brake phase (levers, verification, downstream view, game days).
- `wake-review`: reviewing a change or design against the roadmap.
