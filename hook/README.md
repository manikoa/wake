# wake-hook

Records what a coding agent does through its hooks, and refuses what Wake's brake halts or its flow policy holds back. It reads Claude Code, Codex, Cursor, Grok Build, Gemini CLI and Antigravity. Why it works this way, and what it can't see: [docs/hooks.md](../docs/hooks.md).

- **The hook:** the agent's host runs `wake-hook <host>` on each hook event. It writes the event to a spool, and answers at once.
- **The service:** `wake-hook serve` runs beside it, as a service for each user. It sends the spool to the collector. With `--rules`, it also fetches the brake's rules for the hooks to check.

```bash
go build -o wake-hook ./cmd/wake-hook
```

The standard library only, Go 1.27.

## Run serve

```bash
wake-hook serve --collector https://collector.example.com:4318 --ca /etc/wake/ca.pem \
  --rules http://wake-proxy:9003 --agent claude-code
```

| Flag | What it does |
|---|---|
| `--collector` | The collector's OTLP/HTTP endpoint (`WAKE_ENDPOINT`) |
| `--ca` | A PEM file of roots to trust for the collector and wake-proxy (`WAKE_CA_FILE`) |
| `--rules` | wake-proxy's address. serve fetches each agent's rules from its `/v1/memory`, as the SDKs do. Hooks run with `--stop` need it |
| `--agent` | An agent to fetch rules for from the start; a hook asks for any other. After a restart, serve also fetches for each agent whose rules it finds. Repeatable |
| `--dir` | Where the hooks spool events and note what entered each agent: `WAKE_HOOK_DIR`, or `wake-hook` in the user's cache directory. The hooks and serve must agree |
| `--interval` | How often to send and fetch, every 2 seconds by default |
| `--once` | Make one pass, then exit |

It never goes through the environment's HTTP proxy. It forgets what entered the agents of a session idle for a day. It logs three things:
- Events dropped because the spool was full.
- Hook events `wake-hook` doesn't handle.
- Events the collector refused. These are kept under the directory's `rejected/`.

## Wire up each host

The hook takes four flags:
- `--agent`: the name the brake halts the agent by, the host's name by default (`WAKE_AGENT`).
- `--dir`: where it spools events.
- `--stop`: refuse what the brake halts or its flow policy holds back.
- `--event`: the hook's event, for Antigravity, whose input doesn't say.

Every hook notes what a tool's result brings into its agent, `--stop` or not, and the flow policy decides on what they noted. So wire the hooks after each call as well as those before it. Answering an event, a hook exits 0 and speaks in its host's words, but for one thing: with `--stop`, an event it can't read, such as one over 64 MiB, is refused with exit code 2, the reason on stderr.

Install it in each host's managed settings, so an agent can't remove it (see [docs/hooks.md](../docs/hooks.md)).

**Claude Code**, in `managed-settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "wake-hook claude-code --stop"}]}],
    "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "wake-hook claude-code"}]}],
    "PostToolUseFailure": [{"matcher": "*", "hooks": [{"type": "command", "command": "wake-hook claude-code"}]}],
    "SubagentStop": [{"hooks": [{"type": "command", "command": "wake-hook claude-code"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "wake-hook claude-code --stop"}]}]
  }
}
```

Add `"allowManagedHooksOnly": true` to stop other hooks running. `SubagentStop` records nothing, but says when a subagent left in the background has answered, for the flow policy.

**Codex** reads the same events and answers as Claude Code. Run `wake-hook codex` from its `hooks.json` or `config.toml`, and put managed hooks in `requirements.toml` ([Codex's hooks](https://developers.openai.com/codex/hooks)).

**Cursor**, in its enterprise `hooks.json` or the team's hooks:

```json
{
  "version": 1,
  "hooks": {
    "preToolUse": [{"command": "wake-hook cursor --stop", "failClosed": true}],
    "postToolUse": [{"command": "wake-hook cursor"}],
    "postToolUseFailure": [{"command": "wake-hook cursor"}],
    "subagentStart": [{"command": "wake-hook cursor --stop", "failClosed": true}],
    "subagentStop": [{"command": "wake-hook cursor"}],
    "beforeSubmitPrompt": [{"command": "wake-hook cursor --stop", "failClosed": true}]
  }
}
```

`subagentStop` records nothing, but says when a subagent has answered, for the flow policy. From Cursor 3.22, a subagent's tool hooks name the `Task` call that started it, which ties its calls to the agent that started it (see [docs/hooks.md](../docs/hooks.md)). Cursor's shell, MCP and file hooks are read too. They can describe the same calls as `preToolUse` and `postToolUse`, though, so wire one set.

**Grok Build**, in its organization's hooks or `~/.grok/hooks/wake.json`:

```json
{
  "hooks": {
    "PreToolUse": [{"hooks": [{"type": "command", "command": "wake-hook grok --stop"}]}],
    "PostToolUse": [{"hooks": [{"type": "command", "command": "wake-hook grok"}]}],
    "PostToolUseFailure": [{"hooks": [{"type": "command", "command": "wake-hook grok"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "wake-hook grok"}]}]
  }
}
```

**Gemini CLI**, in `/etc/gemini-cli/settings.json`. Its docs rank this below each user's own settings, so see [docs/hooks.md](../docs/hooks.md):

```json
{
  "hooks": {
    "BeforeTool": [{"matcher": ".*", "hooks": [{"type": "command", "command": "wake-hook gemini --stop"}]}],
    "AfterTool": [{"matcher": ".*", "hooks": [{"type": "command", "command": "wake-hook gemini"}]}],
    "BeforeAgent": [{"hooks": [{"type": "command", "command": "wake-hook gemini --stop"}]}]
  }
}
```

**Antigravity**, in `~/.gemini/config/hooks.json`, with each hook naming its event. It has no managed location yet:

```json
{
  "wake": {
    "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "wake-hook antigravity --event PreToolUse --stop"}]}],
    "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "wake-hook antigravity --event PostToolUse"}]}]
  }
}
```

Cursor runs Claude Code's hooks, and Grok Build runs both Claude Code's and Cursor's, by default. Where another host's hooks are installed on the same machine, turn that off. Grok Build has `GROK_CLAUDE_HOOKS_ENABLED` and `GROK_CURSOR_HOOKS_ENABLED`. Otherwise one call is recorded twice, the second time read as the wrong host's.

## Test

```bash
go test ./...
```

The events in `testdata/events/` come from each vendor's documented examples. `go test -update` rewrites `testdata/otlp.json`, which the collector's tests sign. The flow policy's decisions are checked against `../sdk/testdata/flow_policy.json`, which wake-proxy's and the SDKs' tests read too.
