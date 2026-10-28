# Reference fleet

Four agents on three frameworks, in Python and TypeScript, talking over A2A and MCP through agentgateway, recorded by Wake's SDKs in both languages. It's the recorder's end to end test, run in CI, and the target for game days. It doesn't depend on any design partner's product.

| Agent | Framework | Does | Talks to |
|---|---|---|---|
| support | LangGraph | Handles a customer's refund request, keeps a note in a memory file | CRM (MCP), research, billing and notify (A2A) |
| research | OpenAI Agents SDK | Reads the refund policy page | the web (MCP) |
| billing | Claude Agent SDK | Issues refunds with an in-process tool, keeps a ledger in a memory file, and keeps a sandbox | payments (MCP), its sandbox (E2B) |
| notify | LangGraph.js, on Wake's TypeScript SDK | Emails the customer what support asks it to, keeps an outbox in a memory file | email (MCP) |

Notify is the TypeScript agent (`notify/`, a pnpm project with an image of its own), so the fleet checks that the two SDKs link to each other across agents. The tools are one MCP server (`fleet/tools.py`) with a CRM, the web, payments and email. The refund policy page is poisoned. Below the policy, it tells AI agents to also refund $500 to an account that has nothing to do with the order, so the run pays it out. That payment is the issue the game day catches. Following its parents in the signed log leads back through billing, support and research to the page.

## Run it

```bash
./run.sh
```

It starts the staging recorder (`deploy/compose.yaml`) with this directory's `agentgateway.yaml`, plus the stand-ins, the tools and the agents. Then it sends one request through support, checks that nothing of the fleet's can be reached around the gateway (see [Network](#network)), checks the signed log with `wake-log`, and checks provenance with `check.py`:

- Every agent and every hop is recorded: by the SDKs, the memory adapter, and the proxy for A2A and MCP.
- Everything is in one trace, and every parent is in the log.
- Every action traces back to where its content came from (the gate's completeness metric).
- The $500 payment traces back to the page, and the chain is printed.

Or run the pieces yourself:

```bash
docker compose up -d --build --wait collector wake-proxy agentgateway tools research billing notify
docker compose run --rm support "Customer jane@example.com wants a refund on order 1234."
```

## Models

By default each agent runs on a scripted model, so the fleet needs no API keys and behaves the same every run. Each scripted model plugs in where its framework takes a model and reports itself the way the real one does, so Wake records exactly what it would. For Claude, a stand-in for Claude Code speaks the SDK's control protocol.

With `FLEET_MODELS=real`, research runs on the OpenAI Agents SDK's default model when `OPENAI_API_KEY` is set, and billing runs on Claude Code when `ANTHROPIC_API_KEY` is set. Support and notify stay scripted, so every run walks the same steps.

## Something for every lever

The fleet holds something for each of the brake's levers, so a game day times them all:

- **Proxy deny.** Every call between agents and to the tools goes through agentgateway, which asks wake-proxy.
- **Token revocation and Okta sign-out.** Each agent signs in as its own user at a stand-in identity provider (`fleet/standins/oauth.py`) and holds a refresh token, which it swaps for access tokens. The tools server asks the provider about the caller's access token on every call (RFC 7662), so a revoked token stops a call that goes around the gateway as well as one through it. Revoking a refresh token revokes the access tokens swapped for it.
- **Sandbox freeze.** Billing keeps a sandbox at a stand-in E2B (`fleet/standins/e2b.py`), found by its metadata, `agent=billing`. It auto-resumes on traffic, so the brake freezes it with its filesystem only.
- **Memory.** Billing runs as long as the fleet does, and its memory adapter fetches the brake's quarantines from wake-proxy (through `memory-rules`, see [Network](#network)), so the brake can confirm the adapter has a halt's memory lock. Support's adapter doesn't: it's a one-shot process, gone before a halt.

The stand-ins stand in for a design partner's identity provider, such as Okta, and E2B. Each implements what the agents and the brake call, and nothing more. Real ones come later, from a partner, or an Okta developer org and an E2B account.

Each credential goes into a volume only its holder mounts. Each agent gets its own refresh token, billing also an E2B key, and the tools server a client for introspection. The brake gets the rest in `brake-secrets`, which no fleet service mounts: an OAuth client that may revoke and introspect, an API token for Okta's global token revocation, an E2B key, and its copy of each agent's refresh token. The brake's credentials never pass through the agents, and no agent can read another's.

## Identity

agentgateway names each caller from its SPIFFE ID or JWT subject, as in production. Failing those, it uses the `x-fleet-agent` header the agents send. That header is a stand-in so the example needs no identity provider, and any caller can set it, so don't trust it outside the example; `deploy/agentgateway.yaml` requires each agent's SVID instead. `agents.json` lists the names wake-proxy accepts, so a call naming anyone else, or no one, is denied while any agent is halted. The agents get the CA certificate for the collector's TLS, and nothing else from the recorder: no keys and no identity.

## Network

A compromised agent can reach the rest of the fleet only through agentgateway, where wake-proxy records each call and the brake can deny it. Each agent and the tools server has a network of its own, shared with agentgateway and with what that one container calls directly: the collector for the agents, which send it their events, and the stand-ins:

| Network | On it |
|---|---|
| `support`, `research`, `notify` | that agent, agentgateway, the collector, the identity provider |
| `billing` | billing, agentgateway, the collector, the identity provider, E2B, `memory-rules` |
| `tools` | the tools server, agentgateway, the identity provider |
| `default` | agentgateway, wake-proxy, the collector, the stand-ins, `memory-rules`, and the brake and Temporal when they run next to the recorder |

No agent is on `default`, so none can reach wake-proxy's hooks or its control API (9002), or the brake and Temporal, any of which could undo a halt, as long as none of them publishes a port (below). The collector and the stand-ins join each network they serve rather than one network all the agents share, since on a shared network every agent could reach every other one. None of them forwards anything: each listens on its own port. The stand-ins are on `default` for the brake, which pulls its levers there. The tools server records nothing, so it has no way to the collector. `networks.yaml` attaches the recorder's services from `deploy/compose.yaml`, which is unchanged for staging.

wake-proxy needs a network namespace of its own, not agentgateway's: in the gateway's, its control API would be at the gateway's address on every agent's network. And nothing an agent mustn't reach may publish a port, even on `127.0.0.1`: Docker takes a published port at the container's own address from every network. The fleet publishes only agentgateway's, and `networks.yaml` drops the collector's.

**Memory rules.** Billing's memory adapter reads its quarantine rules from wake-proxy's memory endpoint (9003). wake-proxy can't join billing's network for that, since its control API listens on the same addresses. So `memory-rules` (`fleet/relay.py`), on `default` and billing's network, passes connections to `wake-proxy:9003` and nowhere else. The rules don't go through agentgateway: a halted agent's calls are denied there, and its adapter must still fetch them, or the brake could never confirm the halt's memory lock. A route on agentgateway without the deny would put the rules on every agent's network, and any agent could fetch them in billing's name, which is what the brake counts as billing's adapter having the lock. If another agent's adapter reads rules, `memory-rules` joins that agent's network too.

`run.sh` checks this from each agent and the tools server, right after the request: agentgateway connects, and so do the collector, the stand-ins and `memory-rules` from the containers they serve. Nothing else does, by name or by any of its IP addresses. A listener in another container's network namespace is tried at that container's addresses.
