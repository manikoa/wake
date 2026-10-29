/**
 * Records Claude Agent SDK runs: model calls, tool calls and subagents.
 *
 *     import { createSdkMcpServer, query } from "@anthropic-ai/claude-agent-sdk";
 *     import { WakeClaudeAgent } from "wake-sdk/claude-agent-sdk";
 *
 *     const claude = new WakeClaudeAgent(new Recorder({ agent: "support" }));
 *     const crm = createSdkMcpServer({ name: "crm", tools: claude.tools([lookupOrder]) });
 *     const options = claude.options({ mcpServers: { crm } });
 *     for await (const message of claude.record(query({ prompt, options }), { prompt })) {
 *       ...
 *     }
 *
 * The SDK runs Claude Code in a subprocess and streams its messages back, and Wake reads that
 * stream as it passes. Each assistant message is a model call, and each tool use and its
 * result a tool call. A subagent started with the Agent tool is an agent of its own, named
 * <agent>/<subagent type>: one agent_message hands it the task once it has started, and
 * another carries its answer back, when the tool returns or, for a subagent left running in
 * the background, when Claude Code says it has finished. Several of one type can run at once,
 * so each is also an instance, <agent>/<subagent type>#<the Agent tool use that started it>,
 * and its events name the instance and the lineage that started it. Links are exact, from the
 * SDK's ids: a tool call's parent is the model call that asked for it, and a model call's
 * parents are the call before it and the tool results and messages new in its context.
 *
 * options() adds hooks that open each tool call before it runs, and tools() wraps in-process
 * tools so they run with their event current: an MCP or A2A call they make through the
 * proxy, or a memory write, links back to the tool call. Tools Claude Code runs itself, such
 * as Bash, WebFetch or an external MCP server, are recorded from the stream but can't carry
 * the link.
 *
 * Given the brake's rules (new WakeClaudeAgent(recorder, { quarantine })), the hooks also stop
 * the agent, or one subagent instance of it, in its own process: a tool call a halt covers is
 * refused before it runs, and recorded as denied, an Agent tool call too, which then starts no
 * subagent. That reaches what the proxy deny can't, such as Bash, and subagents that call with
 * their parent's credentials, which the proxy can't tell from their parent. It's a cooperative
 * stop: it holds only while the agent's code does, and a compromised agent can leave it out.
 */

import { randomUUID } from "node:crypto";

import { ROOT_CONTEXT, SpanStatusCode, trace, type Context, type Span } from "@opentelemetry/api";

import * as events from "./events.ts";
import { contextOf, EventGraph, failed, Recent, unique } from "./graph.ts";
import * as links from "./links.ts";
import { log } from "./log.ts";
import type { Quarantine } from "./quarantine.ts";
import { now, type Recorder } from "./recorder.ts";
import { refusal, unchecked, type Refusal } from "./stop.ts";

/** The tool Claude Code starts a subagent with; older versions call it Task. */
export const SUBAGENT_TOOLS = new Set(["Agent", "Task"]);

/** What the Agent tool's result says of a subagent it left running in the background. */
const LAUNCHED = new Set(["async_launched", "remote_launched"]);

type Json = Record<string, unknown>;

function canonical(value: unknown): string {
  return events.dumps(value);
}

/** An in-process tool's own name: Claude sees it as mcp__<server>__<name>. */
function shortName(name: string): string {
  const parts = name.split("__");
  return parts.length >= 3 && parts[0] === "mcp" ? parts.slice(2).join("__") : name;
}

function text(content: unknown): string {
  if (content === null || content === undefined) return "";
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map(text).filter((t) => t).join("\n");
  if (typeof content === "object") {
    const t = (content as Json).text;
    return typeof t === "string" ? t : "";
  }
  return "";
}

function under(span: Span | undefined): Context | undefined {
  return span !== undefined ? trace.setSpan(ROOT_CONTEXT, span) : undefined;
}


/** One agent's conversation in a run: the main agent's, or a subagent's. */
class Thread {
  readonly agent: string;
  readonly instance: string | undefined; // a subagent's own instance; the main agent has none
  readonly lineage: readonly string[]; // the agent and instances that started it, outermost first
  span: Span | undefined; // its events nest under this span
  call: Call | undefined; // the model call streaming now
  lastCall: string | undefined;
  parents: string[]; // what feeds its next model call
  origins: string[];
  given: Json[]; // what's new in its next model call's context
  waitingSince = now(); // when it last handed control to the model
  readonly prompt: unknown;
  spawn: Spawn | undefined; // a subagent's start, until it's known to have started
  background = false; // a subagent the Agent tool left running in the background

  constructor(
    agent: string,
    span: Span | undefined,
    parents: string[],
    origins: string[],
    given: Json[],
    instance?: string,
    lineage: readonly string[] = [],
  ) {
    this.agent = agent;
    this.instance = instance;
    this.lineage = lineage;
    this.span = span;
    this.parents = parents;
    this.origins = origins;
    this.given = given;
    this.prompt = given[0]?.content;
  }

  /** The thread of an instance this one starts. */
  started(agent: string, instance: string, span: Span | undefined, parents: string[], given: Json[]): Thread {
    return new Thread(agent, span, parents, [], given, instance, [...this.lineage, this.instance ?? this.agent]);
  }
}

/**
 * An Agent tool use, until its subagent is known to have started. One refused before then, as
 * by a halt, starts no subagent, and is recorded as the refused tool call it is.
 */
class Spawn {
  readonly starter: Thread; // the thread that asked for it
  readonly tool: string;
  readonly args: unknown;
  readonly parents: string[]; // the model call that asked for it, and what fed it
  readonly eventId = randomUUID(); // the agent_message handing the subagent its task
  readonly at = now();

  constructor(starter: Thread, tool: string, args: unknown, parents: string[]) {
    this.starter = starter;
    this.tool = tool;
    this.args = args;
    this.parents = parents;
  }
}

class Call {
  readonly eventId: string;
  readonly messageId: string | undefined;
  readonly span: Span;
  model = "";
  output: Json[] = [];
  usage: Json | undefined;
  parents: string[] = [];
  origins: string[] = [];
  given: Json[] = [];
  found: string[] = []; // server tool events earlier in this response
  serverTools: string[] = []; // server tool uses still waiting for a result
  error: string | undefined;
  endedAt = now();

  constructor(eventId: string, messageId: string | undefined, span: Span) {
    this.eventId = eventId;
    this.messageId = messageId;
    this.span = span;
  }
}

class Tool {
  readonly eventId: string;
  readonly span: Span;
  readonly link: links.Link;
  readonly name: string;
  readonly args: unknown;
  agent: string;
  instance: string | undefined;
  lineage: readonly string[] = [];
  readonly run: Run | undefined;
  parents: string[] = [];
  error: string | undefined;
  endedAt: number | undefined;
  frame: links.Frame | undefined; // the in-process tool's link frame, once it runs
  halted: string | undefined; // the halt its call was refused under

  constructor(eventId: string, span: Span, name: string, args: unknown, agent: string, run: Run | undefined) {
    this.eventId = eventId;
    this.span = span;
    const ctx = span.spanContext();
    this.link = new links.Link(eventId, ctx.traceId, ctx.spanId);
    this.name = name;
    this.args = args;
    this.agent = agent;
    this.run = run;
  }
}

/** One record() call: a prompt and everything the agent and its subagents did for it. */
class Run {
  readonly root: Span;
  readonly threads = new Map<string | null, Thread>(); // by the Agent tool use that started each
  readonly handoffs = new Map<string, string>(); // Agent tool use id -> its handoff event
  readonly tools = new Set<string>(); // tool use ids opened in this run
  sessionId: string | undefined;

  constructor(root: Span, main: Thread) {
    this.root = root;
    this.threads.set(null, main);
  }
}

/** An in-process tool as createSdkMcpServer takes it. */
export interface SdkTool {
  name: string;
  handler: (args: never, extra: unknown) => Promise<unknown>;
  [key: string]: unknown;
}

type HookCallback = (input: Json, toolUseId: string | undefined, options: { signal: AbortSignal }) => Promise<Json>;
type HookMatchers = Record<string, { matcher?: string; hooks: HookCallback[]; timeout?: number }[]>;

/** Records one Claude agent's runs, and the subagents it starts. */
export class WakeClaudeAgent extends EventGraph {
  readonly #tools = new Recent<string, Tool>(); // tool use id -> its open tool call
  readonly #waiting = new Recent<string, string[]>(); // (tool name, arguments) -> tool use ids an in-process tool may be
  readonly #toolAgents = new Recent<string, string>(); // tool call event id -> the agent making it
  readonly #toolInstances = new Recent<string, [string, readonly string[]]>(); // tool call event id -> the subagent instance making it, and its lineage
  readonly #sessions = new Recent<string, Run>(1024); // session id -> its run now
  readonly #lastCalls = new Recent<string, string>(1024); // session id -> its last model call, for the next turn
  readonly #active: Run[] = [];
  readonly #agentIds = new Recent<string, string>(); // tool use id -> the agent_id its hook named, for a subagent's
  /** A hook's agent_id -> the names of the instance it is: itself, then its lineage. */
  readonly _subagents = new Recent<string, readonly string[]>();
  readonly #spawned = new Recent<string, [string | undefined, string, string, readonly string[]]>(); // Agent tool use id -> its session, and the agent, instance and lineage it started
  readonly #halted = new Recent<string, string>(); // tool use id -> the halt a hook refused it under
  readonly #ambiguous = new Recent<string, true>(); // tool call event id -> an in-process call Wake can't tell from another
  readonly #quarantine: Pick<Quarantine, "halted"> | undefined;

  /**
   * With quarantine, the brake's rules for this agent, a tool call by the agent, or by a
   * subagent instance of it, that a halt covers is refused before it runs, and recorded as
   * denied. It trusts the agent's code: see cooperative stops in the README.
   */
  constructor(recorder: Recorder, { quarantine }: { quarantine?: Pick<Quarantine, "halted"> } = {}) {
    super(recorder);
    this.#quarantine = quarantine;
  }

  // Setup.

  /** These options, with hooks that open each tool call before it runs. */
  options<T extends object>(options?: T): T {
    const hooks: HookMatchers = {};
    for (const [event, matchers] of Object.entries(((options as { hooks?: HookMatchers } | undefined)?.hooks) ?? {})) hooks[event] = [...matchers];
    for (const [event, callback] of [
      ["PreToolUse", this.#preToolUse],
      ["PostToolUse", this.#postToolUse],
      ["PostToolUseFailure", this.#postToolUse],
    ] as const) {
      (hooks[event] ??= []).push({ hooks: [callback] });
    }
    return { ...options, hooks } as T;
  }

  /** These in-process tools, made to run with their tool call's event current. */
  tools<T extends SdkTool>(tools: T[]): T[] {
    return tools.map((t) => ({ ...t, handler: this.#linked(t.name, t.handler) }));
  }

  /**
   * Yields the messages unchanged, recording them as they pass. Pass the prompt that started
   * them so the first model call knows what it was given.
   */
  async *record<M>(messages: AsyncIterable<M>, { prompt }: { prompt?: string } = {}): AsyncGenerator<M, void, undefined> {
    const run = this.#safely("startRun", () => this.#startRun(prompt));
    try {
      for await (const message of messages) {
        this.#safely("observe", () => this.#observe(run, message as Json));
        yield message;
      }
    } finally {
      this.#safely("endRun", () => this.#endRun(run));
    }
  }

  // Hooks.

  readonly #preToolUse: HookCallback = async (input, toolUseId) => {
    this.#safely("hookOpened", () => this.#hookOpened(input, toolUseId));
    const refused = this._halting(input, toolUseId);
    if (refused === undefined) return {};
    return { hookSpecificOutput: { hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: refused[1] } };
  };

  readonly #postToolUse: HookCallback = async (input, toolUseId) => {
    this.#safely("hookClosed", () => this.#hookClosed(input, toolUseId));
    return {};
  };

  #hookOpened(input: Json, toolUseId: string | undefined): void {
    const name = typeof input.tool_name === "string" ? input.tool_name : "";
    const agentId = typeof input.agent_id === "string" && input.agent_id ? input.agent_id : undefined;
    if (toolUseId && agentId) this.#agentIds.set(toolUseId, agentId);
    if (!toolUseId || SUBAGENT_TOOLS.has(name)) return;
    const run = this.#runFor(input.session_id as string | undefined);
    if (run === undefined) return; // the stream opens it instead
    const agentType = agentId ? (input.agent_type as string | undefined) : undefined;
    const agent = agentType ? `${this._recorder.agent}/${agentType}` : this._recorder.agent;
    const tool = this.#openTool(run, toolUseId, name, input.tool_input, agent, run.root);
    if (agentId === undefined) return;
    const known = this._subagents.get(agentId);
    if (tool.instance) {
      // The stream said first which instance asked for it.
      this._subagents.set(agentId, [tool.instance, ...tool.lineage]);
    } else if (known !== undefined) {
      // Its agent_id is placed already, by its earlier calls or the Agent tool's result.
      tool.instance = known[0];
      tool.lineage = known.slice(1);
      this.#toolInstances.set(tool.eventId, [tool.instance, tool.lineage]);
    }
  }

  // Cooperative stops.

  /**
   * The halt that stops this tool call, and why, if one covers who's making it: the agent, the
   * subagent type it runs as, and the instance it is and those that started it. A subagent's
   * instance is known by the agent_id its hooks name, from its earlier calls or the Agent tool's
   * result. Before that, Wake can't tell which instance is calling: it could be any the session
   * started, or one started by any of them that the stream hasn't shown yet, since hooks can run
   * ahead of the code reading it. So the call is refused if a halt covers any of them. A check
   * that fails refuses the call too.
   */
  _halting(input: Json, toolUseId: string | undefined): Refusal | undefined {
    if (this.#quarantine === undefined) return undefined;
    let who = this._recorder.agent;
    let refused: Refusal | undefined;
    try {
      const agentId = typeof input.agent_id === "string" && input.agent_id ? input.agent_id : undefined;
      const agentType = agentId ? (input.agent_type as string | undefined) : undefined;
      const agent = agentType ? `${this._recorder.agent}/${agentType}` : this._recorder.agent;
      const known = agentId !== undefined ? this._subagents.get(agentId) : undefined;
      who = known?.[0] ?? agent;
      const unplaced = agentId !== undefined && known === undefined ? this.#instances(input.session_id as string | undefined) : [];
      refused = refusal(this.#quarantine, who, [this._recorder.agent, agent, ...(known ?? [])], { unplaced });
    } catch (error) {
      log.warn("couldn't check the halts for a tool call; refusing it", error);
      refused = unchecked();
    }
    if (refused === undefined) return undefined;
    const [halt] = refused;
    // Refused all the same if this fails; only its record may say less.
    this.#safely("halting", () => {
      if (toolUseId) this.#halted.set(toolUseId, halt);
      const tool = toolUseId ? this.#tools.get(toolUseId) : undefined;
      if (tool !== undefined) tool.halted = halt;
    });
    log.warn(`refused a tool call of ${who}: ${halt}`);
    return refused;
  }

  /** Every instance Wake has seen start in this session, and what started each. */
  #instances(sessionId: string | undefined): string[] {
    return [...this.#spawned.values()]
      .filter(([session]) => session === sessionId || session === undefined)
      .flatMap(([, , instance, lineage]) => [instance, ...lineage]);
  }

  #hookClosed(input: Json, toolUseId: string | undefined): void {
    const tool = toolUseId ? this.#tools.get(toolUseId) : undefined;
    if (tool === undefined) return;
    tool.endedAt = now();
    if (input.hook_event_name === "PostToolUseFailure") tool.error = String(input.error ?? "error");
  }

  // In-process tools.

  #linked(name: string, handler: SdkTool["handler"]): SdkTool["handler"] {
    return async (args: never, extra: unknown) => {
      const frame = this.#safely("enter", () => this.#enter(name, args));
      if (frame === undefined) return handler(args, extra);
      try {
        return await links._run(frame, () => handler(args, extra));
      } finally {
        links._exit(frame);
      }
    };
  }

  #enter(name: string, args: unknown): links.Frame | undefined {
    const waiting = this.#waiting.get(JSON.stringify([name, canonical(args)])) ?? [];
    const alike = waiting.map((id) => this.#tools.get(id)).filter((t): t is Tool => t !== undefined && !t.halted);
    if (alike.length > 1 && !(alike[0].instance && alike.every((t) => t.instance === alike[0].instance))) {
      // Calls alike, by instances that differ or aren't known yet, and Claude Code may run them
      // in either order: which is which is a guess, so none names an instance.
      for (const t of alike) this.#ambiguous.set(t.eventId, true);
    }
    let tool: Tool | undefined;
    while (waiting.length > 0 && tool === undefined) {
      tool = this.#tools.get(waiting.shift()!);
      if (tool?.halted) tool = undefined; // refused, so it never runs
    }
    if (tool === undefined) return undefined;
    tool.frame = links._frame(tool.link);
    return tool.frame;
  }

  // The message stream.

  #startRun(prompt: string | undefined): Run {
    const agent = this._recorder.agent;
    const link = links.current();
    const root = this._recorder.tracer.startSpan(
      `invoke_agent ${agent}`,
      { attributes: { "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": agent, [events.AGENT_ID]: agent }, startTime: now() },
      this._eventContext(),
    );
    const parents: string[] = [];
    const origins: string[] = [];
    if (link !== undefined) {
      // Started inside a recorded tool call, or for a message from another agent, which
      // gave it the prompt.
      parents.push(...link.parents);
    } else if (prompt !== undefined) {
      origins.push(events.ORIGIN_USER);
    }
    if (prompt) parents.push(...this._matchingOutputs(root.spanContext().traceId, prompt));
    const given = prompt !== undefined ? [{ role: "user", content: prompt }] : [];
    const run = new Run(root, new Thread(agent, root, parents, origins, given));
    this.#active.push(run);
    return run;
  }

  #observe(run: Run | undefined, message: Json): void {
    if (run === undefined || message === null || typeof message !== "object") return;
    const sessionId = message.session_id as string | undefined;
    if (sessionId && run.sessionId === undefined) this.#joinSession(run, sessionId);
    if (message.type === "assistant") this.#assistant(run, message);
    else if (message.type === "user") this.#user(run, message);
    else if (message.type === "result") {
      for (const thread of run.threads.values()) this.#endCall(thread);
      run.root.setAttributes({ "wake.claude.session_id": String(message.session_id ?? ""), "wake.claude.turns": Number(message.num_turns ?? 0) });
      if (message.is_error) run.root.setStatus({ code: SpanStatusCode.ERROR, message: String(message.subtype ?? "error") });
    } else if (message.type === "system" && message.subtype === "task_notification") {
      this.#finished(run, message);
    }
  }

  #joinSession(run: Run, sessionId: string): void {
    run.sessionId = sessionId;
    run.root.setAttribute("wake.claude.session_id", sessionId);
    this.#sessions.set(sessionId, run);
    const previous = this.#lastCalls.get(sessionId);
    const main = run.threads.get(null)!;
    // A later turn of the same conversation: the earlier turns are in its context.
    if (previous !== undefined && main.lastCall === undefined && main.call === undefined) main.parents.unshift(previous);
  }

  #assistant(run: Run, message: Json): void {
    const body = (message.message ?? {}) as Json;
    const messageId = body.id as string | undefined;
    const thread = this.#thread(run, message.parent_tool_use_id as string | null | undefined);
    let call = thread.call;
    if (call !== undefined && (messageId === undefined || call.messageId !== messageId)) {
      this.#endCall(thread);
      call = undefined;
    }
    call ??= this.#startCall(thread, messageId);
    call.model = (body.model as string | undefined) || call.model;
    call.usage = (body.usage as Json | undefined) ?? call.usage;
    call.endedAt = now();
    if (message.error) call.error = String(message.error);
    for (const block of (Array.isArray(body.content) ? body.content : []) as Json[]) {
      const type = block.type as string;
      if (type === "text") {
        call.output.push({ type: "text", text: block.text });
      } else if (type === "thinking") {
        call.output.push({ type: "thinking", thinking: block.thinking });
      } else if (type === "tool_use") {
        call.output.push({ type: "tool_use", id: block.id, name: block.name, input: block.input });
        if (SUBAGENT_TOOLS.has(block.name as string)) this.#handOff(run, thread, call, block);
        else this.#requested(run, thread, [call.eventId, ...call.found], block.id as string, block.name as string, block.input);
      } else if (type === "server_tool_use") {
        call.output.push({ type: "server_tool_use", id: block.id, name: block.name, input: block.input });
        this.#requested(run, thread, [call.eventId, ...call.found], block.id as string, block.name as string, block.input, true);
        call.serverTools.push(block.id as string);
      } else if (type === "tool_result" || type?.endsWith("_tool_result")) {
        call.output.push({ type: "tool_result", tool_use_id: block.tool_use_id, content: block.content });
        const tool = this.#finishTool(block.tool_use_id as string, block.content, block.is_error as boolean | undefined);
        if (tool !== undefined) {
          // Run inside this response: it feeds the rest of it, and the next call.
          call.found.push(tool.eventId);
          thread.parents.push(tool.eventId);
        }
      }
    }
  }

  #user(run: Run, message: Json): void {
    const thread = this.#thread(run, message.parent_tool_use_id as string | null | undefined);
    this.#endCall(thread);
    const content = ((message.message ?? {}) as Json).content;
    const blocks: Json[] = typeof content === "string" ? [{ type: "text", text: content }] : Array.isArray(content) ? content : [];
    // The tool's own output, beside the text the model reads, when the message answers one tool
    // use, as Claude Code's do.
    const output = blocks.filter((b) => b.type === "tool_result").length === 1 ? message.tool_use_result : undefined;
    for (const block of blocks) {
      if (block.type === "tool_result") {
        const toolUseId = block.tool_use_id as string;
        const item: Json = { type: "tool_result", tool_use_id: toolUseId, content: block.content };
        if (block.is_error) item.is_error = true;
        thread.given.push(item);
        let sub = run.threads.get(toolUseId);
        if (sub?.spawn !== undefined && (this.#halted.has(toolUseId) || block.is_error)) {
          // Refused before the subagent started, as by a halt: it never ran.
          this.#neverStarted(run, toolUseId);
          sub = undefined;
        }
        if (sub !== undefined) {
          this.#answered(run, thread, sub, toolUseId, block.content, output);
          continue;
        }
        const tool = this.#finishTool(toolUseId, block.content, block.is_error as boolean | undefined);
        // The tool, and whatever its result came from, such as an A2A reply.
        if (tool !== undefined) thread.parents.push(tool.eventId, ...links._returned(tool.frame));
      } else if (block.type === "text") {
        if (block.text === thread.prompt) continue; // the prompt it started with, echoed back
        thread.given.push({ role: "user", content: block.text });
        if (thread === run.threads.get(null) && !thread.origins.includes(events.ORIGIN_USER)) thread.origins.push(events.ORIGIN_USER);
      }
    }
    thread.waitingSince = now();
  }

  #thread(run: Run, parentToolUseId: string | null | undefined): Thread {
    const key = parentToolUseId ?? null;
    let thread = run.threads.get(key);
    if (thread !== undefined) {
      if (thread.spawn !== undefined) this.#deliver(thread); // its first message: it has started
      return thread;
    }
    const spawned = key !== null ? this.#spawned.get(key) : undefined;
    if (spawned !== undefined) {
      // A subagent started earlier, such as in an earlier turn, and running on in the
      // background: still the same instance.
      const [, agent, instance, lineage] = spawned;
      thread = new Thread(agent, undefined, [], [], [], instance, lineage);
      thread.background = true;
    } else {
      // A subagent whose start Wake didn't see.
      const agent = `${this._recorder.agent}/subagent`;
      thread = run.threads.get(null)!.started(agent, `${agent}#${key}`, undefined, [], []);
    }
    thread.span = this.#invoke(thread, run.root);
    run.threads.set(key, thread);
    return thread;
  }

  // Events.

  #startCall(thread: Thread, messageId: string | undefined): Call {
    const eventId = randomUUID();
    const span = this._recorder.tracer.startSpan("chat", { startTime: thread.waitingSince }, under(thread.span));
    const call = new Call(eventId, messageId, span);
    call.parents = unique([...(thread.lastCall ? [thread.lastCall] : []), ...thread.parents]);
    call.origins = thread.origins;
    call.given = thread.given;
    thread.parents = [];
    thread.origins = [];
    thread.given = [];
    thread.call = call;
    this._rememberLineage(eventId, call.parents, call.origins);
    return call;
  }

  #endCall(thread: Thread): void {
    const call = thread.call;
    if (call === undefined) return;
    thread.call = undefined;
    thread.lastCall = call.eventId;
    for (const toolUseId of call.serverTools) {
      // The SDK passes on few server tool results; the response's end is the tool's.
      const tool = this.#finishTool(toolUseId, undefined, undefined);
      if (tool !== undefined) thread.parents.push(tool.eventId);
    }
    const attributes: Record<string, string | number> = {
      "gen_ai.operation.name": "chat",
      "gen_ai.request.model": call.model,
      "gen_ai.response.model": call.model,
    };
    if (call.messageId) attributes["gen_ai.response.id"] = call.messageId;
    const usage = call.usage ?? {};
    for (const [key, attribute] of [
      ["input_tokens", "gen_ai.usage.input_tokens"],
      ["output_tokens", "gen_ai.usage.output_tokens"],
    ]) {
      if (Number.isInteger(usage[key])) attributes[attribute] = usage[key] as number;
    }
    const payload: Json = { model: call.model, input: call.given, output: call.output };
    if (call.error) payload.error = call.error;
    this.#emit(call.span, call.eventId, events.MODEL_CALL, `chat ${call.model}`.trim(), thread.agent, call.parents, call.origins, attributes, payload, call.error, thread);
    this._rememberOutput(call.span.spanContext().traceId, call.eventId, text(call.output));
    call.span.end(call.endedAt);
  }

  #requested(run: Run, thread: Thread, parents: string[], toolUseId: string, name: string, args: unknown, server = false): void {
    const tool = this.#openTool(run, toolUseId, name, args, thread.agent, thread.span, server);
    tool.agent = thread.agent;
    tool.instance = thread.instance;
    tool.lineage = thread.lineage;
    tool.parents = unique(parents);
    this.#toolAgents.set(tool.eventId, thread.agent);
    if (thread.instance) {
      this.#toolInstances.set(tool.eventId, [thread.instance, thread.lineage]);
      // Its hook came first, naming the subagent by agent_id: now it's placed.
      const agentId = this.#agentIds.get(toolUseId);
      if (agentId) this._subagents.set(agentId, [thread.instance, ...thread.lineage]);
    }
    this._rememberLineage(tool.eventId, tool.parents, [`tool:${name}`]);
  }

  #openTool(run: Run | undefined, toolUseId: string, name: string, args: unknown, agent: string, parentSpan: Span | undefined, server = false): Tool {
    const open = this.#tools.get(toolUseId);
    if (open !== undefined) return open; // the hook opened it before the stream got here
    const eventId = randomUUID();
    const span = this._recorder.tracer.startSpan(
      `execute_tool ${name}`,
      { attributes: { "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name, "gen_ai.tool.call.id": toolUseId }, startTime: now() },
      under(parentSpan),
    );
    const tool = new Tool(eventId, span, name, args, agent, run);
    tool.halted = this.#halted.get(toolUseId); // its hook refused it before the stream got here
    this._rememberLineage(eventId, [], [`tool:${name}`]);
    this.#tools.set(toolUseId, tool);
    this.#toolAgents.set(eventId, agent);
    if (!server) {
      const key = JSON.stringify([shortName(name), canonical(args)]);
      const waiting = this.#waiting.get(key) ?? [];
      waiting.push(toolUseId);
      this.#waiting.set(key, waiting.slice(-64));
    }
    run?.tools.add(toolUseId);
    return tool;
  }

  #finishTool(toolUseId: string, output: unknown, isError: boolean | undefined): Tool | undefined {
    const tool = this.#tools.pop(toolUseId);
    if (tool === undefined) return undefined;
    const error = tool.error ?? (isError ? "error" : undefined);
    const attributes: Record<string, string> = { "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": tool.name };
    const parts = tool.name.split("__");
    if (parts[0] === "mcp" && parts.length >= 3) attributes["wake.mcp.server"] = parts[1];
    const payload: Json = { input: tool.args ?? null, output: output ?? null };
    if (error) payload.error = tool.error ?? (text(output) || error);
    let kind = events.TOOL_CALL;
    let origins = [`tool:${tool.name}`];
    if (tool.halted) {
      // Refused in the agent's own process, under the halt: what the agent tried. The tool
      // never ran, so nothing of it entered.
      kind = events.DENIED;
      origins = [];
      attributes["wake.brake.halt_id"] = tool.halted;
    }
    this.#emit(tool.span, tool.eventId, kind, `execute_tool ${tool.name}`, tool.agent, tool.parents, origins, attributes, payload, error, tool);
    this._rememberOutput(tool.span.spanContext().traceId, tool.eventId, text(output));
    tool.span.end(tool.endedAt ?? now());
    return tool;
  }

  #handOff(run: Run, thread: Thread, call: Call, block: Json): void {
    const toolUseId = block.id as string;
    const parents = unique([call.eventId, ...call.found]);
    if (this.#halted.has(toolUseId)) {
      // Its hook refused it before the stream got here: no subagent starts.
      this.#requested(run, thread, parents, toolUseId, block.name as string, block.input);
      return;
    }
    const args = (block.input !== null && typeof block.input === "object" ? block.input : {}) as Json;
    const target = `${this._recorder.agent}/${(args.subagent_type as string | undefined) || "general-purpose"}`;
    const prompt = args.prompt;
    const spawn = new Spawn(thread, block.name as string, block.input, parents);
    const given = typeof prompt === "string" ? [{ role: "user", content: prompt }] : [];
    const sub = thread.started(target, `${target}#${toolUseId}`, undefined, [spawn.eventId], given);
    // The handoff is recorded once the subagent has started, by its first message or the tool's
    // result: a hook may yet refuse it.
    sub.spawn = spawn;
    run.threads.set(toolUseId, sub);
    run.handoffs.set(toolUseId, spawn.eventId);
    this.#spawned.set(toolUseId, [run.sessionId, target, sub.instance!, sub.lineage]);
  }

  /** Records the agent_message handing a subagent its task, now that it has started. */
  #deliver(sub: Thread): void {
    const spawn = sub.spawn;
    if (spawn === undefined) return;
    sub.spawn = undefined;
    const starter = spawn.starter;
    const args = (spawn.args !== null && typeof spawn.args === "object" ? spawn.args : {}) as Json;
    const name = `agent_message ${starter.agent} -> ${sub.agent}`;
    const span = this._recorder.tracer.startSpan(name, { startTime: spawn.at }, under(starter.span));
    this.#emit(span, spawn.eventId, events.AGENT_MESSAGE, name, sub.agent, spawn.parents, [`agent:${starter.agent}`], { "gen_ai.agent.name": sub.agent }, {
      from: starter.agent,
      to: sub.agent,
      description: args.description ?? null,
      prompt: args.prompt ?? null,
    }, undefined, sub);
    span.end(spawn.at);
    sub.span = this.#invoke(sub, starter.span, spawn.at);
  }

  /** The span a subagent's events nest under. */
  #invoke(sub: Thread, parentSpan: Span | undefined, startTime = now()): Span {
    return this._recorder.tracer.startSpan(
      `invoke_agent ${sub.agent}`,
      {
        attributes: {
          "gen_ai.operation.name": "invoke_agent",
          "gen_ai.agent.name": sub.agent,
          [events.AGENT_ID]: sub.agent,
          ...events.instanceAttributes(sub.instance, sub.lineage),
        },
        startTime,
      },
      under(parentSpan),
    );
  }

  /**
   * Opens an Agent tool call whose subagent never started, refused before it did, as the tool
   * call it was: no handoff, and a denied event when a halt refused it.
   */
  #neverStarted(run: Run, toolUseId: string): void {
    const spawn = run.threads.get(toolUseId)!.spawn!;
    run.threads.delete(toolUseId);
    run.handoffs.delete(toolUseId);
    this.#spawned.delete(toolUseId);
    this.#requested(run, spawn.starter, spawn.parents, toolUseId, spawn.tool, spawn.args);
  }

  /** The Agent tool's result: the subagent's answer, or that it runs on in the background. */
  #answered(run: Run, thread: Thread, sub: Thread, toolUseId: string, content: unknown, output: unknown): void {
    const result = (output !== null && typeof output === "object" ? output : {}) as Json;
    // The id its hooks name it by: its calls are placed from here on, whatever the stream has shown.
    if (typeof result.agentId === "string" && result.agentId) this._subagents.set(result.agentId, [sub.instance!, ...sub.lineage]);
    this.#deliver(sub);
    if (LAUNCHED.has(result.status as string)) {
      // It works on, and answers when Claude Code says it has finished.
      sub.background = true;
      return;
    }
    this.#returned(run, thread, toolUseId, content);
  }

  /** A task Claude Code ran in the background has ended: for a subagent, its answer. */
  #finished(run: Run, message: Json): void {
    const toolUseId = typeof message.tool_use_id === "string" ? message.tool_use_id : undefined;
    const sub = toolUseId !== undefined ? run.threads.get(toolUseId) : undefined;
    if (toolUseId === undefined || sub === undefined || !sub.background) return;
    // The answer goes to what started it, or to the agent once that's gone.
    const starter = [...run.threads.values()].find((t) => t.instance && t.instance === sub.lineage.at(-1)) ?? run.threads.get(null)!;
    this.#returned(run, starter, toolUseId, message.summary);
  }

  #returned(run: Run, thread: Thread, toolUseId: string, content: unknown): void {
    const sub = run.threads.get(toolUseId)!;
    run.threads.delete(toolUseId);
    this.#endCall(sub);
    sub.span?.end(now());
    const source = sub.lastCall ?? run.handoffs.get(toolUseId);
    const eventId = randomUUID();
    const name = `agent_message ${sub.agent} -> ${thread.agent}`;
    const span = this._recorder.tracer.startSpan(name, { startTime: now() }, under(thread.span));
    this.#emit(span, eventId, events.AGENT_MESSAGE, name, thread.agent, source ? [source] : [], [`agent:${sub.agent}`], { "gen_ai.agent.name": thread.agent }, {
      from: sub.agent,
      to: thread.agent,
      output: content ?? null,
    }, undefined, thread);
    this._rememberOutput(span.spanContext().traceId, eventId, text(content));
    span.end(now());
    thread.parents.push(eventId);
  }

  #emit(
    span: Span,
    eventId: string,
    kind: string,
    name: string,
    agent: string,
    parents: string[],
    origins: string[],
    attributes: Record<string, string | number>,
    payload: Json,
    error?: string,
    who?: Thread | Tool,
  ): void {
    parents = unique(parents);
    this._rememberLineage(eventId, parents, origins);
    span.updateName(name);
    span.setAttributes({
      [events.EVENT_ID]: eventId,
      [events.EVENT_KIND]: kind,
      [events.AGENT_ID]: agent,
      [events.CAPTURE]: "sdk",
      [events.PARENTS]: parents,
      [events.ORIGINS]: origins,
      ...(who !== undefined ? events.instanceAttributes(who.instance, who.lineage) : {}),
      ...attributes,
    });
    span.setAttributes(events.payloadAttributes(payload));
    if (error) span.setStatus({ code: SpanStatusCode.ERROR, message: error });
    const ctx = span.spanContext();
    links._note(new links.Link(eventId, ctx.traceId, ctx.spanId));
  }

  #endRun(run: Run | undefined): void {
    if (run === undefined) return;
    for (const [key, thread] of [...run.threads]) {
      if (thread.spawn !== undefined) {
        if (key !== null && this.#halted.has(key)) {
          this.#neverStarted(run, key); // finished below, with the run's other open calls
          continue;
        }
        this.#deliver(thread);
      }
      this.#endCall(thread);
      if (key !== null) thread.span?.end(now());
    }
    for (const toolUseId of run.tools) {
      if (this.#tools.get(toolUseId)?.run === run) this.#finishTool(toolUseId, undefined, undefined);
    }
    const main = run.threads.get(null)!;
    const index = this.#active.indexOf(run);
    if (index >= 0) this.#active.splice(index, 1);
    if (run.sessionId !== undefined) {
      if (main.lastCall !== undefined) this.#lastCalls.set(run.sessionId, main.lastCall);
      if (this.#sessions.get(run.sessionId) === run) this.#sessions.delete(run.sessionId);
    }
    run.root.end(now());
  }

  // Helpers.

  /**
   * The run a hook's session is in: before a run has read its first message, the newest that
   * hasn't. Never a run of another session, as one still going after this session's has ended,
   * while a subagent of this one runs on.
   */
  #runFor(sessionId: string | undefined): Run | undefined {
    return (sessionId ? this.#sessions.get(sessionId) : undefined) ?? this.#active.findLast((r) => r.sessionId === undefined);
  }

  /** The agent whose tool call is running, for a memory write it makes. */
  override _currentAgent(): string {
    const link = links.current();
    return (link !== undefined ? this.#toolAgents.get(link.eventId) : undefined) ?? this._recorder.agent;
  }

  override _currentInstance(): [string | undefined, readonly string[]] {
    const link = links.current();
    if (link === undefined || this.#ambiguous.has(link.eventId)) return [undefined, []];
    // Known from the stream, or from the agent_id its hook named. Before either, the instance is
    // left out rather than guessed: one the stream hasn't shown may be calling.
    return this.#toolInstances.get(link.eventId) ?? [undefined, []];
  }

  /** Under the tool call or message that started this run, if any. */
  override _eventContext(): Context | undefined {
    return contextOf(links.current());
  }

  #safely<T>(what: string, fn: () => T): T | undefined {
    try {
      return fn();
    } catch (error) {
      log.warn(`failed to record ${what}`, error);
      failed(this._recorder, what);
      return undefined;
    }
  }
}
