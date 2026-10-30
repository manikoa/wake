# Recording coding agents through their hooks

Decided October 29, 2026.

## The question

Coding agents such as Claude Code, Codex, Cursor, Grok Build, Gemini CLI and Antigravity are products, not frameworks: no SDK runs inside them. Their own tools, the shell, file reads and file edits, never touch MCP. The proxy sees only the MCP calls routed through it, and their MCP clients can't present an SVID, so the proxy as shipped turns them away. So Wake sees none of what they do.

Each of them, though, runs a command of the user's choosing on its hook events: before and after each tool call, and when a prompt is submitted. That's the one place each lets a program see what its agent does, and refuse it.

## The decision

`wake-hook` ([`hook/`](../hook)) is a capture component, like an SDK. The agent's host runs it on each hook event:

- It records the event as an unsigned Wake event with `wake.capture` `hook`. The collector signs it on arrival, as it signs SDK events ([signing.md](signing.md)).
- Given the brake's rules, it refuses the calls a halt covers, as the SDKs do in an agent's own process (Denied calls in [events.md](events.md)), and those wake-proxy's flow policy holds back ([below](#the-flow-policy)).

One command covers all six, since they've converged on Claude Code's contract: a JSON event on stdin, and a decision on stdout. Cursor and Grok Build can even load Claude Code's hooks. Each still names its events and fields its own way, so the host is the command's first argument: `wake-hook claude-code`, `codex`, `cursor`, `grok`, `gemini` or `antigravity`.

Antigravity's input doesn't say which event fired, so each of its hooks is told on the command line, as in `wake-hook antigravity --event PreToolUse`. A hook that isn't told can't tell what it's answering. It records nothing, and where it would check halts, it refuses.

## Why it's capture only

It runs on the agent's machine, under the user's account, where an agent with a shell can reach it. So it holds no key, and a hook event is only as honest as that machine, as an SDK event is only as honest as the agent's process. The proxy's own record of MCP calls stays the independent check. The halt and flow checks, too, hold only while the machine does: what entered each agent is kept in files an agent with a shell could remove. The proxy deny, the proxy's own flow check and the token levers don't depend on them.

## Recording never blocks

A hook runs once per event and exits, so it can't send in the background as an SDK does. Instead it writes the event to a spool directory and answers at once. `wake-hook serve`, a long running process on the same machine, sends the spool to the collector over OTLP/HTTP.

The spool holds at most 10,000 events or 512 MiB. Past that, an event is dropped, and `serve` counts and logs the drop. It does the same for every hook event `wake-hook` doesn't handle, rather than let it pass silently.

## The brake fails closed

With `--rules`, wake-proxy's address, `serve` fetches the brake's rules from its `/v1/memory` every 2 seconds for each agent. It names the agent and its own process, as the SDKs do, so the brake can confirm a halt reached the machine. The agents are those given with `--agent`, those a hook asks for, and, after a restart, each whose rules it finds on disk.

With `--stop`, the hook reads the rules `serve` last wrote. It refuses a call when a halt covers the agent, the session or the subagent making it. It refuses every call while the rules are missing or older than 30 seconds (`stale-rules`), or can't be read (`halt-check-failed`), as when `halts` is `null`. Only a wake-proxy that sends no `halts` at all, being older than stops in the agent's process, leaves nothing to check. While the rules are missing, stale or can't be read, the hook asks `serve` to fetch them.

The hosts fail open on a hook that crashes or times out: Claude Code, Codex and Grok Build always, Cursor unless `failClosed` is set, and Gemini CLI on any exit code but 0 and 2. Antigravity's docs don't say. So, answering an event, `wake-hook` exits 0, says each refusal in the host's own words, and refuses the call when anything goes wrong while checking. The one exception is an event it can't read at all: one that isn't JSON, or is over 64 MiB, as a huge tool input can make it. It can't tell what's being asked, or how the host wants a refusal said, so with `--stop` it exits 2, the reason on stderr, which each host but Antigravity documents as a refusal. A hook that's missing altogether still fails open, which is why it's installed where agents can't change it.

## The flow policy

wake-proxy serves the flow policy it enforces with the brake's rules ([flow-everywhere.md](flow-everywhere.md)). With `--stop`, the hook applies it, after the halts, to the calls its host makes, which never pass wake-proxy. It decides as wake-proxy and the SDKs do: the same patterns, the rules in file order, the first that refuses named as `policy:<rule id>`. `sdk/testdata/flow_policy.json` pins the decisions all of them make.

- **What's checked:** each tool call a hook can refuse, by the tool's own name, and each start of a subagent, as reaching it. A Claude Code `Agent` call is both: a tool call, and reaching `<agent>/<subagent_type>`, `general-purpose` when it names none. Cursor's `subagentStart` reaches `<agent>/<subagent_type>`. Rules naming `path_prefixes` or `stores` never apply here. A prompt is never checked: it isn't a call, and the user's prompt isn't an origin.
- **Who:** the agent, or `<agent>/<agent type>` for a subagent Claude Code names or wake-hook places for Cursor (below). A rule naming the agent holds its sessions and subagents too.
- **What enters:** each tool call recorded, and each Cursor read let through, enters the agent that made it as `tool:<name>`, failed calls and calls whose output the host doesn't send included. An MCP tool enters by its own name, as wake-proxy names it: Claude Code's and Codex's `mcp__crm__lookup_order`, and Cursor's `MCP:lookup_order`, enter as `tool:lookup_order`. A Claude Code or Cursor subagent starts with what had entered the agent starting it, and its answer brings back what entered it, each as `agent:<sender>`. A Claude Code subagent left in the background answers when `SubagentStop` names it. A Cursor subagent answers when the `Task` call that started it returns, and again each time `subagentStop` names it. Every hook notes this, `--stop` or not, so the hooks that check see what the others noted.
- **Cursor's subagents:** Cursor runs each subagent in a conversation of its own. From Cursor 3.22, the subagent's tool hooks name the `Task` call that started it (`parent_tool_call_id`), as `subagentStart` does (`tool_call_id`). So once `subagentStart` is let through, wake-hook places the subagent's events in the session of the agent that started it, as `<agent>/<subagent_type>`. Once one is placed, so is the rest of its conversation: its shell, MCP and file hooks, which name no call, and the subagents it starts. Where it can't follow a subagent, it fails safe:
  - a call naming a `Task` call whose start wake-hook didn't let through counts as having every origin, since what the subagent started with isn't known;
  - when none of a subagent's events was placed, as before Cursor 3.22, or a `Task` call whose start wake-hook didn't see returns, the agent the answer reaches counts as having every origin for a window from then, since what the subagent brings back isn't known.
- **Where it's kept:** in `flow/` in the hooks' directory, a file per origin for each agent of each session, saying when it last entered. Only what entered within the policy's window counts. `serve` removes a session nothing has entered for a day, the longest window a policy can set. Cursor's subagents are kept there too, by the `Task` call that started each and by the conversation it runs in, and forgotten the same way.
- **Limits:** past 256 origins within the window, an agent counts as having every origin until the window passes, so any rule that applies to it refuses. So does an agent whose event names no session, since nothing is known of what entered it.
- **Fails closed:** a policy wake-proxy can't use, one that isn't valid, or a check that fails refuses every tool call and subagent start, under `policy:unreadable-policy`.
- **Recorded** as a `denied` event under `policy:<rule id>`. Its payload adds `origins`, the origins the rule keeps from the call, or `["*"]`, and `every_origin`, why the agent counted as having every origin, when it did.

## Installed where agents can't change it

A project's hooks file is one the agent can edit, and an agent that can edit it can remove its own hook, as OpenAI's agent removed its own monitor. So install `wake-hook` in each host's managed settings, which take an administrator to change and which user and project settings can't override:

| Host | Managed hooks |
|---|---|
| Claude Code | `managed-settings.json` in `/Library/Application Support/ClaudeCode/` (macOS), `/etc/claude-code/` (Linux and WSL) or `C:\Program Files\ClaudeCode\` (Windows). `allowManagedHooksOnly` keeps other hooks from running |
| Codex | `requirements.toml`, with `[hooks]` and `allow_managed_hooks_only` |
| Cursor | `hooks.json` in `/Library/Application Support/Cursor/` (macOS), `/etc/cursor/` (Linux and WSL) or `C:\ProgramData\Cursor\` (Windows), or the team's hooks in Cursor's dashboard. Set `failClosed` on the hooks that refuse |
| Grok Build | Its organization managed hooks |
| Gemini CLI | `/etc/gemini-cli/settings.json`, its system settings. Its docs rank these below user and project settings, and let a user turn hooks off with `/hooks disable`. So this holds only as far as each user's own settings do |
| Antigravity | None documented. Its global hooks file, `~/.gemini/config/hooks.json`, is in the user's home, where the agent can write. Until it has a managed one, its record and stop are only as strong as that file |

`serve` runs as a service for each user, from the same installer. The proxy notes its rule fetches by agent and process, the same signal the brake already uses for the SDKs' stops, so a machine whose `serve` goes quiet drops out of what the brake counts as confirmed. A hook removed from a host's settings isn't something `wake-hook` can notice. The census of agents seen against the inventory, in Brake, is where that shows.

## Events

| Host | Recorded | Refusable |
|---|---|---|
| Claude Code | `PostToolUse` and `PostToolUseFailure` as `tool_call`; `UserPromptSubmit` as an `agent_message` from the user | `PreToolUse`, `UserPromptSubmit` |
| Codex | `PostToolUse` as `tool_call`; `UserPromptSubmit` as an `agent_message` | `PreToolUse`, `UserPromptSubmit` |
| Cursor | `postToolUse`, `postToolUseFailure`, `afterShellExecution`, `afterMCPExecution`, `afterFileEdit` and `beforeReadFile` as `tool_call`; `beforeSubmitPrompt` as an `agent_message` | `preToolUse`, `beforeShellExecution`, `beforeMCPExecution`, `beforeReadFile`, `subagentStart`, `beforeSubmitPrompt` |
| Grok Build | `PostToolUse` and `PostToolUseFailure` as `tool_call`; `UserPromptSubmit` as an `agent_message` | `PreToolUse` |
| Gemini CLI | `AfterTool` as `tool_call`, failed calls included; `BeforeAgent` as an `agent_message` | `BeforeTool`, `BeforeAgent` |
| Antigravity | `PostToolUse` as `tool_call` | `PreToolUse` |

A call is recorded once it's done, with its input and output. A refused call is recorded as a `denied` event instead, as the SDKs record theirs. Cursor's general tool hooks and its shell and MCP hooks can describe the same call, so register one set: the examples use the general ones. Claude Code's and Codex's `SubagentStop` records nothing: it tells the flow policy that a subagent left in the background has answered. Cursor's `subagentStop` records nothing either: it tells the flow policy that a subagent has answered.

- **Who:**
  - `wake.agent.id` is the name the brake halts the agent by, given with `--agent`. It defaults to the host's name.
  - Each session is an instance, `<agent>#<session id>`, since one agent runs as many sessions at once.
  - A subagent that Claude Code or Codex names on its events (`agent_id`) is an instance of its own, `<agent>/<agent type>#<agent id>`, started by its session.
  - So is a Cursor subagent wake-hook places by the `Task` call that started it (see the flow policy), `<agent>/<subagent type>#<call id>`, started by the session, or the subagent, that started it. Its events share that session's trace, and keep the subagent's own `conversation_id` under `hook`.
- **The session's trace:** every event of a session shares one trace, whose id is derived from the host and the session id, so the session's events are found together.
- **Parents and origins:** a tool call's parent is the session's latest prompt. Its origin is `tool:<name>`, since that's where its result enters. A prompt's origin is `user`.
- **Payload:**
  - A tool call's is `{"input", "output"}`, as the SDKs record one. A prompt's is `{"from": "user", "to", "content"}`.
  - The fields of the host's event that neither uses, such as the working directory and the permission mode, are kept under `hook`, so nothing the host said is lost.
  - A payload over 4 MiB is cut, as the SDKs cut theirs.
- **`wake.hook.host`** names the host.

## What it can't see

- **Model calls.** Hooks show no model input or output, so a tool call's parent is the prompt, not the model call that chose it.
- **A call that never finishes.** A call is recorded when its after-event fires, so one whose session dies first isn't recorded, unless it was refused. Pairing each before-event with its after-event in `serve`, as the proxy pairs requests with responses, would close this.
- **Tools the vendor runs**, such as hosted web search, and anything the agent does outside its tools. What they bring in enters the agent as no origin.
- **Codex's subagents:** it starts one with `spawn_agent`, whose input and result its docs don't give, and a subagent's hooks name its session's id. So the flow policy holds a Codex session and its subagents as one agent: what enters either holds both back, at once. That's stricter than the SDKs, which carry it only when a subagent starts or answers.
- **Cursor:**
  - Tab completions. On cloud agents, the MCP and session hooks, and user hooks.
  - Its docs don't say whether `{}` from a permission hook means no opinion. `wake-hook` answers `{}` rather than `allow`, which might skip Cursor's own approval. Check this on a real Cursor before rolling out.
  - Subagents before Cursor 3.22. A subagent's own events name a conversation of their own, and nothing ties them to the one that started it, so they can't be told from a new conversation's. Such a subagent doesn't start with what had entered the agent starting it, so its calls aren't held to that. What it brings back can't be followed either, so the agent its answer reaches counts as having every origin for a window from the answer.
  - From 3.22, only a subagent's tool hooks name the `Task` call that started it. Its shell, MCP and file hooks are placed once one of those has been, and not before.
  - Its users report that `subagentStop` doesn't fire for a subagent in the background. Such a subagent's answer is followed only as far as it had got when its `Task` call returned.
  - Its hooks reference doesn't list `parent_tool_call_id`, `child_conversation_id` or `parent_agent_id` yet. Its release notes for 3.22 and 3.27 do, checked October 30, 2026.
- **Grok Build:** its docs name `hookEventName`, `sessionId`, `cwd`, `workspaceRoot`, `toolName` and `toolInput`, but no field for a tool's result. So its tool calls keep the result under `hook`, wherever Grok Build puts it, rather than in `output`.
- **Gemini CLI:** its events name no subagent, so a subagent's calls are recorded as its session's, and the flow policy holds them as its session's agent.
- **Antigravity:**
  - Its docs name no field for a tool's result, only an error when a call fails, so a call's output is recorded only when it failed.
  - Its hooks see no prompt, so its tool calls have none for a parent, and nothing it's asked can be refused before it starts.
  - Its SDK's own hooks, for agents built on it, are a framework's, for an SDK integration rather than `wake-hook`.

The test fixtures are built from each vendor's documented examples, checked October 29, 2026, not recorded from the real CLIs, and Cursor's subagent fields from its release notes, checked October 30, 2026. Next comes running each CLI headless against the collector.
