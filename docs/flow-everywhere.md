# Flow policies everywhere an agent acts

Decided October 30, 2026, by the founder, building ahead into Contain. What that first left out was added the same day, by the same decision ([below](#what-it-left-out-added-the-same-day)).

## The question

A flow policy keeps content from an untrusted origin, such as a page a tool fetched or another agent's message, from triggering a privileged call ([proxy.md](proxy.md), Flow policies). wake-proxy enforces it, so it covers only the calls that pass wake-proxy, and decides only on the origins wake-proxy saw. Three ways around it are open today:

- **Calls that never reach the proxy.** An agent's own tools run in its process: the shell, file edits, a fetch tool, any function the framework calls. So do handoffs and sub-agents started in the same process. A page fetched by a local tool, then a payment made by another, meets no rule.
- **Memory.** The memory adapter labels each entry with the origins behind it, but reads don't pass the proxy (proxy.md, "Memory reads aren't seen"). An agent that reads an entry another agent wrote after fetching a page gets the page with no origin noted.
- **The network.** Neither the reference fleet nor `deploy/` limits egress: an agent can call an MCP server or an API on the internet directly, and in the fleet's `real` mode its model calls go straight to the provider.

## The decision

1. **wake-proxy serves the policy it enforces.** The rules SDKs already fetch from the read-only port (`GET /v1/memory`) gain a `flow` field: the parsed policy, `{"version", "window_seconds", "rules"}`, with `version` a SHA-256 of the rules and window, so an SDK can tell a new policy from the one it has. A file wake-proxy can't use is served as `{"unreadable": true}`. Without `--policy-file` the field is absent. So every SDK holds the policy wake-proxy holds, set where agents can't write it, and SDKs that don't know the field ignore it, as they ignore any field they don't know.

2. **The SDKs apply it in the agent's own process, with the origins they saw enter the agent.** Each SDK keeps, per agent, the origins that entered it and when, over the policy's window, as wake-proxy does:
   - a tool's result enters as `tool:<name>`. An MCP tool's origin uses its MCP name, as wake-proxy's does, so `mcp__crm__lookup_order` enters as `tool:lookup_order`;
   - a message, a handoff or a sub-agent's result from another agent enters as `agent:<name>`, and what had entered the sender goes with it, as wake-proxy carries origins across an A2A hop. A sub-agent starts with what had entered the agent that started it;
   - a memory read enters the origins its label carries, at the time of the read. Content with no label, or changed since its label was written, enters as `memory:unlabeled`, which a rule can name;
   - the user's own prompt isn't an origin, as it isn't for wake-proxy.

   Before each tool call, handoff and agent start, the SDK decides as wake-proxy does: the same patterns, the rules in file order, and the first rule that denies is the one named, `policy:<id>`.
   - A tool call meets the rules wake-proxy would apply to an MCP call: first those without `tools` that name no `receivers` or `path_prefixes` (wake-proxy's ext_authz applies them to every call), then those whose `tools` match the tool.
   - A handoff or an agent start meets the rules without `tools` whose `receivers` are empty or name the agent being started. Rules with `path_prefixes` only ever meet HTTP requests at the proxy.
   - `callers` match the agent's name or any agent in its lineage, as wake-proxy holds a sub-agent to the rules that name its chain.

   A refusal looks like a halt's in each integration (a denied `PreToolUse`, a rejected guardrail, an error `ToolMessage`, or `AgentHalted`), and is recorded as a `denied` event under `policy:<id>` with the origins behind it, as wake-proxy records its own.

3. **It fails closed where the proxy does.** Rules gone stale already refuse every call (`stale-rules`), and the flow policy comes with them. An unreadable policy refuses every call as `policy:unreadable-policy`. Past 256 origins within the window, an agent counts as having every origin, and any rule that applies denies.

4. **One set of test vectors decides the same calls everywhere.** `sdk/testdata/flow_policy.json` holds policies and calls (caller, lineage, tool, receiver or store, origins) with the decision each must get. wake-proxy's tests, the Python SDK's, the TypeScript SDK's and `wake-hook`'s all read it, so they can't drift apart.

5. **The proxy becomes the only way out of the reference fleet.** The agents' networks are `internal`, so the only places an agent reaches are agentgateway, the collector, the identity provider and the fleet's own stand-ins. The end-to-end test checks that no agent reaches the internet, the Docker host, or a port it shouldn't. `deploy/README.md` gains a "Before production" item for the same egress rule, with an example Kubernetes NetworkPolicy.

## Why the SDK may enforce this

The architecture's rule is that anything an agent can switch off may only capture. That still holds for every decision here: the policy is set and served out of band, and wake-proxy goes on enforcing it where an agent can't reach. The SDK's check is a second layer for the calls wake-proxy never sees, as the SDKs' halts already are. A compromised agent can switch it off, which is why the proxy and the network stay the backstop: a refusal in the process stops a well-behaved agent fooled by what it read, and the egress rule stops a compromised one from going around the proxy.

## What it left out, added the same day

6. **Rules for memory writes.** A rule may name `stores`, each exact or a prefix ending in `*`: a store as the memory adapter names it, such as a LangGraph namespace joined with `/`, or `file` for memory files. Such a rule holds every write, delete and rollback in a matching store, by a matching caller, and decides on what had entered the caller, as for a tool call. The memory adapter checks it after the brake's quarantines and before the app's write gate, and a refusal is the adapter's own (`MemoryLocked`), recorded as a `denied` event under `policy:<id>` with the origins behind it. A rule naming stores can't name tools, receivers or paths, and a rule naming no stores never holds a memory change, so a policy written before stores keeps its meaning. A policy that can't be used refuses every memory change too. wake-proxy never sees these changes: it reads and serves the rules, and holds none of its own calls to them.

7. **Hosted tools.** A tool the provider runs inside the model call, such as the OpenAI Agents SDK's web search, can't be refused one call at a time in the agent's process. So, in the OpenAI Agents SDK integrations:
   - What one brings in enters the agent as `tool:<name>`, by the hosted tool's name (`tool:web_search`), once the response is in and before anything that follows from it. When the trace leaves the response out, Wake can't see which ran, so it notes every hosted tool the agent offered that call. The TypeScript SDK hands Wake a response's end a moment after the run goes on, so there each check first waits, up to 5 seconds, for the end of every response of the agent's run, and refuses past that.
   - Before each model call, `guard()` checks each hosted tool the agent offers, as a tool call by that agent: the halts, then the flow policy. If any is held back, the model call isn't made, and the run stops with the refusal, since the provider would run the tool unchecked. Each is recorded as a `denied` event.
   - A hosted MCP tool is checked per call: `guard()` makes it ask before every call, and answers from the halts and the flow policy, then from the tool's own approval callback if it has one. A call it lets through enters the agent as the MCP tool's own name. One whose calls the app approves through the run's interruptions is left as it is: Wake can't answer for a person.

8. **Coding agents.** `wake-hook` applies the policy to the calls its host makes, after the halts, on #74's branch (`docs/hooks.md`, "The flow policy"). Each hook is a process of its own, so what entered each agent of a session is kept in files, one per origin. MCP tools go by their own names, as wake-proxy names them. A Claude Code subagent starts with what had entered the agent starting it, and its answer brings back what entered it, as in the Claude Agent SDK integration. A Codex session and its subagents count as one agent, since its hooks don't say when a subagent starts or answers, and a Cursor subagent's start is checked, though nothing ties its own events to the agent that started it. The same change fixes `wake-hook serve` fetching the rules from wake-proxy's address rather than `/v1/memory`, and a restart leaving hooks on stale rules for good.

9. **Model calls through agentgateway.** The reference fleet's `real` mode sends research's and billing's model calls to agentgateway's `openai` and `anthropic` routes, which add the provider's key (`examples/fleet/models.yaml`), so no agent holds one, and every agent's network stays internal in that mode too. The gateway's deny lever covers those calls: a halted agent's model calls are denied. `deploy/README.md` says to route model calls the same way.

10. **A flow policy in the reference fleet's scenario.** The staging recorder now runs wake-proxy with `--policy-file`, a policy with no rules unless `WAKE_POLICY` names another. `run.sh` sends the customer's request as before, then the same request with `examples/fleet/policy.json` in force, once wake-proxy has watched for a whole window. Its rule keeps content from `fetch_page` from triggering billing's `refund` calls, and `check.py` checks that wake-proxy refused both, the $500 the page asked for and the $80 the customer did, which came in the same message, and traces the refused payment back to the page.

## Not covered yet

- **Claude's server tools**, which Anthropic runs inside the model call. Claude Code has no hook before a model call, so they can't be checked in the agent's process, and what they bring in is noted as the stream shows it, which can be after the agent's next call.
- **What a model call asks for.** wake-proxy decides on who makes a model call through the gateway, not on what it asks the model, such as the hosted tools it offers.
- **A hosted MCP tool approved through interruptions**, as above, and in the TypeScript SDK one the app approved for good, or one found through deferred tool search.
- **Cursor's subagents:** what enters one isn't carried back to the agent that started it.
- **Carrying every origin.** When a message comes from an agent with every origin, an origin that entered the sender later isn't carried with it: wake-proxy, the SDKs and `wake-hook` alike.
