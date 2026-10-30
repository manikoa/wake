# Wake

Wake is security software for AI agents. It keeps what an agent reads from deciding what it does, stops a fleet or a single sub-agent and checks that it stopped, finds what got through, contains what followed, and proves what happened in a log no agent can rewrite.

This repository is Wake's open source core: the part that runs in your stack, under Apache 2.0. It runs on its own, with no Wake account, and gives you flow policies, run charters, the deny at the gateway, memory controls and the signed record.

## What's here

| Part | Where | What it does |
|---|---|---|
| wake-proxy | [`proxy/`](proxy) | Holds MCP and A2A calls to your flow policies, run charters and halts at the gateway, and model calls to your flow policies and halts. Signs its record of MCP and A2A calls, and of what it refuses. In Rust, on [agentgateway](https://agentgateway.dev). |
| SDKs | [`sdk/python/`](sdk/python), [`sdk/typescript/`](sdk/typescript) | In Python and TypeScript, for the Claude Agent SDK, OpenAI Agents SDK, LangChain and LangGraph. They record what the agent does, and hold halts and flow policies over local tools and handoffs. |
| Memory adapter | in the SDKs | Labels each write with where it came from, and gates, quarantines and rolls back memory. |
| wake-hook | [`hook/`](hook) | Records Claude Code, Codex, Cursor, Grok Build, Gemini CLI and Antigravity through the hooks each one documents, and refuses the tool calls a halt or flow policy covers. |
| Collector | [`collector/`](collector) | Replaces recognized credentials, signs what the SDKs and wake-hook send, and checks what the proxy signed. |
| Signed log | [`log/`](log) | An append only log on Trillian Tessera that you host. Anyone with its public keys can check it with `wake-log`, without trusting Wake. |
| Deploy | [`deploy/`](deploy) | A Compose file that runs the collector, wake-proxy and agentgateway in one place, for staging. |
| Reference fleet | [`examples/fleet/`](examples/fleet) | Four agents on three frameworks, talking over A2A and MCP through agentgateway. CI runs it end to end. |

## What isn't here

Wake Platform, which isn't open source: the brake, the provenance graph, the monitor, the dashboard and the evidence pack. It builds on this core. Finding what got through, the levers beyond the gateway and the check that a stop took are all in Wake Platform.

## Try it

The reference fleet needs Docker and Python 3. Its agents run on scripted models, so there are no API keys to set.

```bash
git clone https://github.com/usewake/wake
cd wake/examples/fleet
./run.sh
```

A poisoned web page gets a $500 payment made, and the signed log traces it back to the page. Then the same request runs under a flow policy, and wake-proxy refuses that payment and the $80 refund the customer asked for, since a rule goes by what entered the agent, not by which part of a message asked for what.

## Before you rely on it

- **You write the rules, and none ship.** They go by where content came from, not by reading it. Wake doesn't scan for malicious instructions or stop a model from being fooled. It refuses what your rules cover.
- **Agents need no way around the gateway.** wake-proxy decides only on what passes it, so in your own stack an egress rule has to leave agents no other way out ([Before production](deploy/README.md#before-production)). The SDKs and wake-hook apply the same rules in the agent's own process, a second layer a compromised agent can switch off.
- **Recording fails open, and losses are counted.** Recording never blocks an agent, so a call can go unrecorded. If wake-proxy can't answer, though, the gateway denies the call.
- **Some of it hasn't met the real products.** wake-hook is built to each vendor's documented hooks and tested on fixtures. wake-proxy's checks on model calls have run against a stand-in provider, not the real ones.
- **No release has been cut.** The SDKs aren't on PyPI or npm, and there are no published images or binaries, so build from this repository.

## Docs

[getwake.co/docs](https://getwake.co/docs/) has the quickstart, the SDKs, the proxy and how events are signed. The design notes behind them are in [`docs/`](docs), with the [repository layout](docs/repo-layout.md), [how releases work](docs/releases.md) and the [product roadmap](docs/product-roadmap.md).

## License

[Apache 2.0](LICENSE)
