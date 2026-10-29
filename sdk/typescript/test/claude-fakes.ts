/**
 * A fake Claude Code for the Claude Agent SDK's spawnClaudeCodeProcess: it plays a scripted
 * run over stream-json the way the CLI does, so the SDK's own code parses every message,
 * calls Wake's hooks and runs the in-process tools.
 */

import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";

type Json = Record<string, unknown>;

export class FakeClaudeCode {
  readonly #out: PassThrough;
  readonly #waiting = new Map<string, (response: Json) => void>();
  readonly #hooks = new Map<string, string[]>();
  #requests = 0;
  readonly sessionId: string;
  readonly refused: string[] = []; // tool uses a PreToolUse hook denied

  constructor(out: PassThrough, sessionId: string) {
    this.#out = out;
    this.sessionId = sessionId;
  }

  _initialize(request: Json): void {
    for (const [event, matchers] of Object.entries((request.hooks as Record<string, { hookCallbackIds: string[] }[]>) ?? {})) {
      this.#hooks.set(event, matchers.flatMap((m) => m.hookCallbackIds));
    }
  }

  _answered(response: Json): void {
    const resolve = this.#waiting.get(response.request_id as string);
    this.#waiting.delete(response.request_id as string);
    resolve?.(response);
  }

  emit(message: Json): void {
    this.#out.write(`${JSON.stringify(message)}\n`);
  }

  async request(request: Json): Promise<Json> {
    const requestId = `cli_${++this.#requests}`;
    const answered = new Promise<Json>((resolve) => this.#waiting.set(requestId, resolve));
    this.emit({ type: "control_request", request_id: requestId, request });
    const response = await answered;
    if (response.subtype !== "success") throw new Error(`control request failed: ${JSON.stringify(response)}`);
    return (response.response as Json) ?? {};
  }

  async hook(event: string, toolUseId: string, fields: Json): Promise<Json[]> {
    const answers: Json[] = [];
    for (const callbackId of this.#hooks.get(event) ?? []) {
      answers.push(await this.request({
        subtype: "hook_callback",
        callback_id: callbackId,
        tool_use_id: toolUseId,
        input: { hook_event_name: event, session_id: this.sessionId, transcript_path: "", cwd: "/", tool_use_id: toolUseId, ...fields },
      }));
    }
    return answers;
  }

  async mcp(server: string, method: string, params: Json, id?: number): Promise<Json> {
    const message: Json = { jsonrpc: "2.0", method, params };
    if (id !== undefined) message.id = id;
    const response = await this.request({ subtype: "mcp_message", server_name: server, message });
    return (response.mcp_response as Json) ?? {};
  }

  /** Claude Code sends each content block of a response as its own message. */
  assistant(messageId: string, blocks: Json[], parent: string | null = null, model = "claude-opus-5-5"): void {
    for (const block of blocks) {
      this.emit({
        type: "assistant",
        parent_tool_use_id: parent,
        session_id: this.sessionId,
        uuid: `${messageId}-${Math.random()}`,
        message: { id: messageId, type: "message", role: "assistant", model, content: [block], usage: { input_tokens: 100, output_tokens: 20 } },
      });
    }
  }

  user(blocks: Json[], parent: string | null = null): void {
    this.emit({ type: "user", parent_tool_use_id: parent, session_id: this.sessionId, message: { role: "user", content: blocks } });
  }

  async useTool(
    toolUseId: string,
    name: string,
    args: Json,
    run: () => Promise<string>,
    options: { parent?: string; agent?: string; agentId?: string } = {},
  ): Promise<void> {
    const context = options.agent ? { agent_id: options.agentId ?? `agent-${options.agent}`, agent_type: options.agent } : {};
    const answers = await this.hook("PreToolUse", toolUseId, { tool_name: name, tool_input: args, ...context });
    const refused = answers
      .map((a) => (a.hookSpecificOutput ?? {}) as Json)
      .filter((o) => o.permissionDecision === "deny")
      .map((o) => o.permissionDecisionReason as string);
    if (refused.length) {
      // As Claude Code does: the tool doesn't run, and the model is told why.
      this.refused.push(toolUseId);
      this.user([{ type: "tool_result", tool_use_id: toolUseId, content: refused[0], is_error: true }], options.parent ?? null);
      return;
    }
    const output = await run();
    await this.hook("PostToolUse", toolUseId, { tool_name: name, tool_input: args, tool_response: output, ...context });
    this.user([{ type: "tool_result", tool_use_id: toolUseId, content: output }], options.parent ?? null);
  }

  /** Calls an in-process MCP tool the way Claude Code does, and returns its text. */
  async callTool(server: string, name: string, args: Json): Promise<string> {
    await this.mcp(server, "initialize", { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "claude-code", version: "2" } }, 1);
    await this.mcp(server, "notifications/initialized", {});
    const response = await this.mcp(server, "tools/call", { name, arguments: args }, 2);
    return ((response.result as { content: { text: string }[] }).content ?? []).map((c) => c.text).join("\n");
  }
}

/**
 * A spawnClaudeCodeProcess that plays the script once the SDK sends the prompt, and exits
 * when the SDK closes its input.
 */
export function fakeClaudeCode(script: (cli: FakeClaudeCode) => Promise<void>, sessionId = "session-1") {
  return () => {
    const stdin = new PassThrough();
    const stdout = new PassThrough();
    const events = new EventEmitter();
    const cli = new FakeClaudeCode(stdout, sessionId);
    let played: Promise<void> | undefined;
    let buffered = "";
    const process = {
      stdin,
      stdout,
      killed: false,
      exitCode: null as number | null,
      kill() {
        process.killed = true;
        exit(143);
        return true;
      },
      on: events.on.bind(events),
      once: events.once.bind(events),
      off: events.off.bind(events),
    };
    const exit = (code: number) => {
      if (process.exitCode !== null) return;
      process.exitCode = code;
      stdout.end();
      setImmediate(() => events.emit("exit", code, null));
    };
    stdin.on("data", (chunk: Buffer) => {
      buffered += chunk.toString("utf8");
      let newline: number;
      while ((newline = buffered.indexOf("\n")) >= 0) {
        const line = buffered.slice(0, newline).trim();
        buffered = buffered.slice(newline + 1);
        if (!line) continue;
        const message = JSON.parse(line) as Json;
        if (message.type === "control_request") {
          const request = message.request as Json;
          if (request.subtype === "initialize") cli._initialize(request);
          cli.emit({ type: "control_response", response: { subtype: "success", request_id: message.request_id, response: {} } });
        } else if (message.type === "control_response") {
          cli._answered(message.response as Json);
        } else if (message.type === "user" && played === undefined) {
          played = script(cli);
        }
      }
    });
    stdin.on("finish", () => void (played ?? Promise.resolve()).then(() => exit(0), () => exit(1)));
    return process;
  };
}
