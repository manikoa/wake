# Repository layout

Decided October 26, 2026.

## Two repositories

**This repository** is public and Apache 2.0. It holds only the open source core, which the roadmap's pricing table lists as free forever: the SDKs, the proxy, the memory adapter and a self hosted signed log.

**The private repository, `usewake/wake-platform`,** holds the paid parts: the hosted log, the provenance and downstream views (the dashboard), the brake and its Temporal workers, and the hosted control plane and its deployment.

The test for where code goes: if it's part of the open source core, it goes here. Everything else goes in `wake-platform`.

## This repository

```
sdk/python/       Python SDK: Claude Agent SDK, OpenAI Agents SDK and LangGraph support, plus the memory adapter
sdk/typescript/   TypeScript SDK: the same coverage
proxy/            Rust MCP and A2A proxy on agentgateway
collector/        Go processor for the OpenTelemetry Collector
log/              Go self hosted signed log on Trillian Tessera
hook/             Go wake-hook: records coding agents with no SDK (Claude Code, Codex, Cursor, Grok Build, Gemini CLI, Antigravity) through their hooks
deploy/           Compose file that runs the core's services: collector, proxy and agentgateway
examples/fleet/   Reference fleet: agents on three frameworks over A2A and MCP, the end to end test and game day target
docs/
```

- Each folder uses its own language's standard tooling: cargo for `proxy/`, a separate Go module each for `collector/`, `log/` and `hook/`, uv for `sdk/python/`, and pnpm for `sdk/typescript/`. Don't add a cross-language build system. Each component's Dockerfile lives in its folder; `deploy/` only puts the images together.
- The memory adapter lives inside the SDKs, as the roadmap describes, not in a folder of its own.
- `examples/` holds runnable examples that use the core, not parts of it. The reference fleet is a uv project that depends on `sdk/python` by path, and its TypeScript agent (`examples/fleet/notify/`) a pnpm project that depends on `sdk/typescript` by path.
- Create a folder when its first code lands, not before.

## Rules

- **Dependencies point one way.** `wake-platform` uses released versions of the SDKs, proxy and log from here. Nothing here depends on `wake-platform`, and the core must run on its own with no Wake account.
- **Turborepo is for the dashboard**, which lives in `wake-platform`, as the roadmap's stack table says.
- **Proxy exception.** If agentgateway's extension points cover Wake's signing and deny, the proxy stays in `proxy/`. If Wake has to fork agentgateway, the fork gets its own public repository so it stays easy to sync with upstream. Check this at the start of Record.
