# Recording coding agents through their hooks

Decided October 29, 2026.

## The question

Coding agents such as Claude Code, Codex, Cursor, Grok Build, Gemini CLI and Antigravity are products, not frameworks: no SDK runs inside them. Their own tools, the shell, file reads and file edits, never touch MCP. The proxy sees only the MCP calls routed through it, and their MCP clients can't present an SVID, so the proxy as shipped turns them away. So Wake sees none of what they do.

Each of them, though, runs a command of the user's choosing on its hook events: before and after each tool call, and when a prompt is submitted. That's the one place each lets a program see what its agent does, and refuse it.

## The decision

`wake-hook` ([`hook/`](../hook)) is a capture component, like an SDK. The agent's host runs it on each hook event:

- It records the event as an unsigned Wake event with `wake.capture` `hook`. The collector signs it on arrival, as it signs SDK events ([signing.md](signing.md)).
- Given the brake's rules, it refuses the calls a halt covers, as the SDKs do in an agent's own process (Denied calls in [events.md](events.md)).

One command covers all six, since they've converged on Claude Code's contract: a JSON event on stdin, and a decision on stdout. Cursor and Grok Build can even load Claude Code's hooks. Each still names its events and fields its own way, so the host is the command's first argument: `wake-hook claude-code`, `codex`, `cursor`, `grok`, `gemini` or `antigravity`.

Antigravity's input doesn't say which event fired, so each of its hooks is told on the command line, as in `wake-hook antigravity --event PreToolUse`. A hook that isn't told can't tell what it's answering. It records nothing, and where it would check halts, it refuses.

## Why it's capture only

It runs on the agent's machine, under the user's account, where an agent with a shell can reach it. So it holds no key, and a hook event is only as honest as that machine, as an SDK event is only as honest as the agent's process. The proxy's own record of MCP calls stays the independent check. The halt check, too, holds only while the machine does. The proxy deny and the token levers don't depend on it.

## Recording never blocks

A hook runs once per event and exits, so it can't send in the background as an SDK does. Instead it writes the event to a spool directory and answers at once. `wake-hook serve`, a long running process on the same machine, sends the spool to the collector over OTLP/HTTP.

The spool holds at most 10,000 events or 512 MiB. Past that, an event is dropped, and `serve` counts and logs the drop. It does the same for every hook event `wake-hook` doesn't handle, rather than let it pass silently.

## The brake fails closed

With `--rules`, `serve` fetches the brake's rules from wake-proxy every 2 seconds for each agent. It names the agent and its own process, as the SDKs do, so the brake can confirm a halt reached the machine.

With `--stop`, the hook reads the rules `serve` last wrote. It refuses a call when a halt covers the agent, the session or the subagent making it. It refuses every call while the rules are missing or older than 30 seconds (`stale-rules`), or can't be read (`halt-check-failed`).

The hosts fail open on a hook that crashes or times out: Claude Code, Codex and Grok Build always, and Cursor unless `failClosed` is set. Gemini CLI's and Antigravity's docs don't say. So `wake-hook` never exits with an error, and anything that goes wrong while checking a halt refuses the call. A hook that's missing altogether still fails open, which is why it's installed where agents can't change it.

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

A call is recorded once it's done, with its input and output. A refused call is recorded as a `denied` event instead, as the SDKs record theirs. Cursor's general tool hooks and its shell and MCP hooks can describe the same call, so register one set: the examples use the general ones.

- **Who:**
  - `wake.agent.id` is the name the brake halts the agent by, given with `--agent`. It defaults to the host's name.
  - Each session is an instance, `<agent>#<session id>`, since one agent runs as many sessions at once.
  - A subagent that Claude Code or Codex names on its events (`agent_id`) is an instance of its own, `<agent>/<agent type>#<agent id>`, started by its session.
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
- **Tools the vendor runs**, such as hosted web search, and anything the agent does outside its tools.
- **Cursor:**
  - Tab completions. On cloud agents, the MCP and session hooks, and user hooks.
  - Its docs don't say whether `{}` from a permission hook means no opinion. `wake-hook` answers `{}` rather than `allow`, which might skip Cursor's own approval. Check this on a real Cursor before rolling out.
- **Grok Build:** its docs name `hookEventName`, `sessionId`, `cwd`, `workspaceRoot`, `toolName` and `toolInput`, but no field for a tool's result. So its tool calls keep the result under `hook`, wherever Grok Build puts it, rather than in `output`.
- **Gemini CLI:** its events name no subagent, so a subagent's calls are recorded as its session's.
- **Antigravity:**
  - Its docs name no field for a tool's result, only an error when a call fails, so a call's output is recorded only when it failed.
  - Its hooks see no prompt, so its tool calls have none for a parent, and nothing it's asked can be refused before it starts.
  - Its SDK's own hooks, for agents built on it, are a framework's, for an SDK integration rather than `wake-hook`.

The test fixtures are built from each vendor's documented examples, checked October 29, 2026, not recorded from the real CLIs. Next comes running each CLI headless against the collector.
