# Flow policies everywhere an agent acts

Decided October 30, 2026, by the founder, building ahead into Contain.

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

4. **One set of test vectors decides the same calls everywhere.** `sdk/testdata/flow_policy.json` holds policies and calls (caller, lineage, tool or receiver, origins) with the decision each must get. wake-proxy's tests, the Python SDK's and the TypeScript SDK's all read it, so the three can't drift apart.

5. **The proxy becomes the only way out of the reference fleet.** The agents' networks are `internal`, so the only places an agent reaches are agentgateway, the collector, the identity provider and the fleet's own stand-ins. The end-to-end test checks that no agent reaches the internet, the Docker host, or a port it shouldn't. `deploy/README.md` gains a "Before production" item for the same egress rule, with an example Kubernetes NetworkPolicy.

## Why the SDK may enforce this

The architecture's rule is that anything an agent can switch off may only capture. That still holds for every decision here: the policy is set and served out of band, and wake-proxy goes on enforcing it where an agent can't reach. The SDK's check is a second layer for the calls wake-proxy never sees, as the SDKs' halts already are. A compromised agent can switch it off, which is why the proxy and the network stay the backstop: a refusal in the process stops a well-behaved agent fooled by what it read, and the egress rule stops a compromised one from going around the proxy.

## Not in this change

- **Coding agents' hooks.** `wake-hook` can refuse a tool call, so it can apply the same policy once [#74](https://github.com/usewake/wake/pull/74) merges.
- **Rules for memory writes.** Refusing untrusted content into trusted memory stays the write gate's job, in code, until declarative rules for stores are designed.
- **Hosted tools**, such as the OpenAI Agents SDK's web search, run at the provider, out of the process's reach.
- **Model calls in the fleet's `real` mode.** They need a route through agentgateway to a model provider before the agents' networks can close in that mode too.
