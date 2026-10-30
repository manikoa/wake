/**
 * A fake Claude Code for the Claude Agent SDK's spawnClaudeCodeProcess: it plays a scripted
 * run over stream-json the way the CLI does, so the SDK's own code parses every message,
 * calls Wake's hooks and runs the in-process tools.
 */

import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";

type Json = Record<string, unknown>;

/**
 * Who makes a tool call, and how the call comes: parent is the Agent tool use whose subagent
 * makes it, agent its type and agentId the id its hooks name it by; ask, as for allowed().
 */
export interface Called {
  parent?: string;
  agent?: string;
  agentId?: string;
  ask?: () => Promise<void> | void;
}

/** What a hook says of the subagent making a call: nothing, for the main agent. */
export function caller(options: Called): Json {
  return options.agent ? { agent_id: options.agentId ?? `agent-${options.agent}`, agent_type: options.agent } : {};
}

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

  /** Calls the hooks registered for event. A hook about no tool use, such as SubagentStop, has no toolUseId. */
  async hook(event: string, toolUseId: string | undefined, fields: Json): Promise<Json[]> {
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

  /** Says a subagent has stopped, as Claude Code does before it hands its answer on: the SubagentStop hooks. */
  async stopped(agentId: string, agentType: string): Promise<void> {
    await this.hook("SubagentStop", undefined, { agent_id: agentId, agent_type: agentType, agent_transcript_path: "", stop_hook_active: false });
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

  /** result is the tool's own output, beside the text the model reads. */
  user(blocks: Json[], parent: string | null = null, result?: Json): void {
    const message: Json = { type: "user", parent_tool_use_id: parent, session_id: this.sessionId, message: { role: "user", content: blocks } };
    if (result !== undefined) message.tool_use_result = result;
    this.emit(message);
  }

  /**
   * Asks the PreToolUse hooks whether a tool may run. With ask, the message asking for it is
   * read only after they have answered, as when the code reading the stream lags behind the
   * hooks.
   */
  async allowed(toolUseId: string, name: string, args: Json, context: Json, options: Called = {}): Promise<boolean> {
    const answers = await this.hook("PreToolUse", toolUseId, { tool_name: name, tool_input: args, ...context });
    await options.ask?.();
    const refused = answers
      .map((a) => (a.hookSpecificOutput ?? {}) as Json)
      .filter((o) => o.permissionDecision === "deny")
      .map((o) => o.permissionDecisionReason as string);
    if (!refused.length) return true;
    // As Claude Code does: the tool doesn't run, and the model is told why.
    this.refused.push(toolUseId);
    this.user([{ type: "tool_result", tool_use_id: toolUseId, content: refused[0], is_error: true }], options.parent ?? null);
    return false;
  }

  async useTool(toolUseId: string, name: string, args: Json, run: () => Promise<string>, options: Called = {}): Promise<void> {
    const context = caller(options);
    if (!(await this.allowed(toolUseId, name, args, context, options))) return;
    const output = await run();
    await this.hook("PostToolUse", toolUseId, { tool_name: name, tool_input: args, tool_response: output, ...context });
    this.user([{ type: "tool_result", tool_use_id: toolUseId, content: output }], options.parent ?? null);
  }

  /**
   * Starts a subagent with the Agent tool, unless a hook refuses it. work plays the subagent, its
   * hooks naming it subagentId, and returns its answer, the tool's result. In the background, the
   * result says it was launched, and a task_notification says when it has finished.
   */
  async spawn(
    toolUseId: string,
    args: { description: string; prompt: string; subagent_type?: string },
    work: () => Promise<string>,
    options: Called & { subagentId: string; background?: boolean },
  ): Promise<void> {
    const context = caller(options);
    if (!(await this.allowed(toolUseId, "Agent", args, context, options))) return;
    const parent = options.parent ?? null;
    if (!options.background) {
      const answer = await work();
      const done = { status: "completed", agentId: options.subagentId, content: [{ type: "text", text: answer }], prompt: args.prompt };
      await this.hook("PostToolUse", toolUseId, { tool_name: "Agent", tool_input: args, tool_response: done, ...context });
      this.user([{ type: "tool_result", tool_use_id: toolUseId, content: [{ type: "text", text: answer }] }], parent, done);
      return;
    }
    const launched = {
      status: "async_launched",
      agentId: options.subagentId,
      description: args.description,
      prompt: args.prompt,
      outputFile: `/tmp/${options.subagentId}.output`,
    };
    await this.hook("PostToolUse", toolUseId, { tool_name: "Agent", tool_input: args, tool_response: launched, ...context });
    this.user([{ type: "tool_result", tool_use_id: toolUseId, content: [{ type: "text", text: `Async agent launched successfully. agentId: ${options.subagentId}` }] }], parent, launched);
    await work();
    this.emit({
      type: "system",
      subtype: "task_notification",
      task_id: options.subagentId,
      tool_use_id: toolUseId,
      status: "completed",
      output_file: launched.outputFile,
      summary: `Agent "${args.description}" completed`,
      uuid: `${toolUseId}-done`,
      session_id: this.sessionId,
    });
  }

  /** Calls an in-process MCP tool the way Claude Code does, and returns its text. Only the first call initializes. */
  async callTool(server: string, name: string, args: Json, first = true): Promise<string> {
    if (first) {
      await this.mcp(server, "initialize", { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "claude-code", version: "2" } }, 1);
      await this.mcp(server, "notifications/initialized", {});
    }
    const response = await this.mcp(server, "tools/call", { name, arguments: args }, first ? 2 : 3);
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
