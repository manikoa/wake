# Repository layout

Decided October 26, 2026.

## Two repositories

**This repository** is public and Apache 2.0. It holds only the open source recorder, which the roadmap's pricing table lists as free forever: the SDKs, the proxy, the memory adapter and a self hosted signed log.

**The private repository, `manikoa/wake-platform`,** holds the paid parts: the hosted log, the provenance and downstream views (the dashboard), the brake and its Temporal workers, and the hosted control plane and its deployment.

The test for where code goes: if it's part of the open source recorder, it goes here. Everything else goes in `wake-platform`.

## This repository

```
sdk/python/       Python SDK: Claude Agent SDK, OpenAI Agents SDK and LangGraph support, plus the memory adapter
sdk/typescript/   TypeScript SDK: the same coverage
proxy/            Rust MCP and A2A proxy on agentgateway
collector/        Go processor for the OpenTelemetry Collector
log/              Go self hosted signed log on Trillian Tessera
deploy/           Compose file that runs the recorder: collector, proxy and agentgateway
docs/
```

- Each folder uses its own language's standard tooling: cargo for `proxy/`, a separate Go module each for `collector/` and `log/`, uv for `sdk/python/`, and pnpm for `sdk/typescript/`. Don't add a cross-language build system. Each component's Dockerfile lives in its folder; `deploy/` only puts the images together.
- The memory adapter lives inside the SDKs, as the roadmap describes, not in a folder of its own.
- Create a folder when its first code lands, not before.

## Rules

- **Dependencies point one way.** `wake-platform` uses released versions of the SDKs, proxy and log from here. Nothing here depends on `wake-platform`, and the recorder must run on its own with no Wake account.
- **Turborepo is for the dashboard**, which lives in `wake-platform`, as the roadmap's stack table says.
- **Proxy exception.** If agentgateway's extension points cover Wake's signing and deny, the proxy stays in `proxy/`. If Wake has to fork agentgateway, the fork gets its own public repository so it stays easy to sync with upstream. Check this at the start of Record.
