/**
 * Records OpenAI Agents SDK runs: model responses, tool calls and handoffs between agents.
 *
 *     import { install } from "wake-sdk/openai-agents";
 *
 *     install(new Recorder({ agent: "support" })); // then run agents as usual
 *     await run(triageAgent, "Refund order 1234");
 *
 * The Agents SDK traces every run, and Wake adds itself as one of its trace processors. Each
 * agent's run groups its events, named for the agent. Links are exact where the SDK gives
 * ids: a tool call's parent is the response that requested it, and a response's parents are
 * the earlier responses and tool results in its input, as Responses items or as Chat
 * Completions messages. A handoff is an agent_message in the receiving agent's name, fed by
 * the response that handed off, and it feeds that agent's next step. A function tool runs
 * with its event current, and an agent run inside it, such as an agent used as a tool,
 * descends from that tool call. Where nothing else links events, content does, as for
 * LangChain.
 *
 * Inputs and outputs are only in the trace when the SDK's traceIncludeSensitiveData is on, as
 * it is by default. Without them, events are recorded with empty payloads, and the SDK's loop
 * links them instead: a tool call comes from its agent's latest response, and a response
 * reads that response and the tool results since.
 */

import { randomUUID } from "node:crypto";

import { addTraceProcessor, getCurrentSpan } from "@openai/agents";
import { ROOT_CONTEXT, SpanStatusCode, trace, type Context, type Span as OtelSpan } from "@opentelemetry/api";

import * as events from "./events.ts";
import { contextOf, EventGraph, failed, Recent, unique } from "./graph.ts";
import * as links from "./links.ts";
import { log } from "./log.ts";
import type { Recorder } from "./recorder.ts";

// The Agents SDK's tracing objects, as far as Wake reads them.
interface SdkTrace {
  traceId: string;
  name: string;
}

interface SdkSpan {
  traceId: string;
  spanId: string;
  parentId: string | null;
  spanData: { type: string; [key: string]: unknown };
  error: { message: string; data?: unknown } | null;
}

// Function tools running now, by the SDK's span id, and each span's parent, shared by every
// processor: span ids are unique. With them, the tool call whose function runs now is found
// from the span the SDK keeps current while it runs, even inside a nested run.
const runningTools = new Recent<string, links.Frame>();
const spanParents = new Recent<string, string | null>();

links._addResolver(() => {
  let sid = (getCurrentSpan() as SdkSpan | null)?.spanId ?? null;
  for (let i = 0; sid !== null && sid !== undefined && i < 1000; i++) {
    const frame = runningTools.get(sid);
    if (frame !== undefined && !frame.ended) return frame;
    sid = spanParents.get(sid) ?? null;
  }
  return undefined;
});

/** Adds Wake as a trace processor for every OpenAI Agents SDK run in this process. */
export function install(recorder: Recorder): WakeTracingProcessor {
  const processor = new WakeTracingProcessor(recorder);
  addTraceProcessor(processor);
  return processor;
}

function field(item: unknown, ...names: string[]): unknown {
  if (item === null || typeof item !== "object") return undefined;
  for (const name of names) {
    const value = (item as Record<string, unknown>)[name];
    if (value !== undefined) return value;
  }
  return undefined;
}

/** The text in an item or a list of them, for the content index. */
function text(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(text).filter((t) => t).join("\n");
  const content = field(value, "content");
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map((c) => field(c, "text")).filter((t): t is string => typeof t === "string" && t !== "").join("\n");
  for (const key of ["text", "output", "arguments"]) {
    const v = field(value, key);
    if (typeof v === "string") return v;
    if (key === "output" && v !== undefined) return text(v);
  }
  // A Chat Completions response: the messages in its choices.
  const choices = field(value, "choices");
  if (Array.isArray(choices)) return text(choices.map((c) => field(c, "message")));
  return "";
}

/** The assistant messages a model call produced, whatever shape the SDK reports them in. */
function outputItems(output: unknown): unknown[] {
  const items = Array.isArray(output) ? output : [];
  return items.flatMap((item) => {
    const choices = field(item, "choices");
    return Array.isArray(choices) ? choices.map((c) => field(c, "message")).filter((m) => m) : [item];
  });
}

type Key = string;

function key(...parts: unknown[]): Key {
  return JSON.stringify(parts);
}

interface Pending {
  eventId: string;
  frame?: links.Frame;
}

/** An OpenAI Agents SDK TracingProcessor that records Wake events. */
export class WakeTracingProcessor extends EventGraph {
  readonly #agentSpan = new Recent<string, string>(); // sdk span id of an agent span -> agent name
  readonly #pending = new Recent<string, Pending>(); // sdk span id -> what onSpanStart set up
  readonly #call = new Recent<Key, [string | undefined, string]>(); // (trace, tool name, arguments) -> (call id, response event id)
  readonly #eventByCall = new Recent<string, string>(); // call id -> tool call event id
  readonly #returnedByEvent = new Recent<string, string[]>(); // tool call event id -> events its output came from
  readonly #eventByItem = new Recent<string, string>(); // response or output item id -> response event id
  readonly #lastResponse = new Recent<Key, string>(); // (trace, agent) -> response event id
  readonly #handedOff = new Recent<Key, string>(); // (trace, agent) -> handoff event id, feeding its next step
  readonly #seenUser = new Recent<Key, true>(); // (trace, digest) for user input already seen
  readonly #roots = new Recent<string, OtelSpan>(1024); // sdk trace id -> the workflow's root span
  readonly #linked = new Recent<string, string[]>(1024); // sdk trace id -> what started it, feeding its first response
  readonly #spans = new Recent<string, OtelSpan>(); // sdk span id -> its open span
  readonly #toolAgents = new Recent<string, string>(); // tool call event id -> the agent making it
  readonly #toolsSince = new Recent<Key, string[]>(); // (trace, agent) -> tool calls since its last response, when inputs are hidden
  readonly #nested = new Recent<string, true>(); // sdk span id of a tool call whose nested run's first response is recorded

  constructor(recorder: Recorder) {
    super(recorder);
  }

  // TracingProcessor.

  async onTraceStart(sdkTrace: SdkTrace): Promise<void> {
    this.#safely("onTraceStart", () => {
      // One root per workflow, so every agent in it, handoffs included, shares a trace. A
      // workflow run inside a recorded tool call, or for a message from another agent,
      // joins that trace, and its first response descends from it.
      const link = links.current();
      const root = this._recorder.tracer.startSpan(
        `workflow ${sdkTrace.name}`,
        { attributes: { "wake.openai_agents.workflow": sdkTrace.name ?? "" } },
        this._eventContext(),
      );
      this.#roots.set(sdkTrace.traceId, root);
      if (link !== undefined) this.#linked.set(sdkTrace.traceId, link.parents);
    });
  }

  async onTraceEnd(sdkTrace: SdkTrace): Promise<void> {
    this.#safely("onTraceEnd", () => this.#roots.pop(sdkTrace.traceId)?.end());
  }

  async onSpanStart(span: SdkSpan): Promise<void> {
    this.#safely("onSpanStart", () => {
      spanParents.set(span.spanId, span.parentId);
      const data = span.spanData;
      const kind = data.type;
      if (!["agent", "response", "generation", "function", "handoff"].includes(kind)) return;
      let attributes = {};
      if (kind === "agent") {
        this.#agentSpan.set(span.spanId, String(data.name));
        attributes = { "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": String(data.name), [events.AGENT_ID]: String(data.name) };
      }
      const name = typeof data.name === "string" ? data.name : "";
      const otel = this._recorder.tracer.startSpan(`${kind} ${name}`.trim(), { attributes }, this.#context(span));
      this.#spans.set(span.spanId, otel);
      if (kind !== "function") return;
      // Calls the tool makes, such as memory writes, link back to this event: it's current
      // while the tool's function runs.
      const eventId = randomUUID();
      const ctx = otel.spanContext();
      const frame = links._frame(new links.Link(eventId, ctx.traceId, ctx.spanId));
      runningTools.set(span.spanId, frame);
      // Its arguments aren't set yet, so the responses that asked for a tool of this name
      // stand in as its parents until it ends, for a memory write it makes.
      const agent = this.#agentFor(span);
      this.#pending.set(span.spanId, { eventId, frame });
      this.#toolAgents.set(eventId, agent);
      let requestedBy = [...this.#call.entries()]
        .filter(([k]) => {
          const [t, n] = JSON.parse(k) as [string, string];
          return t === span.traceId && n === name;
        })
        .map(([, [, response]]) => response);
      const last = this.#lastResponse.get(key(span.traceId, agent));
      // Without sensitive data no call names it: the agent's latest response did.
      if (requestedBy.length === 0 && last !== undefined) requestedBy = [last];
      this._rememberLineage(eventId, unique(requestedBy), [`tool:${name}`]);
    });
  }

  async onSpanEnd(span: SdkSpan): Promise<void> {
    this.#safely("onSpanEnd", () => {
      const data = span.spanData;
      switch (data.type) {
        case "agent":
          return this.#agentEnded(span);
        case "response":
          return this.#responseEnded(span);
        case "generation":
          return this.#generationEnded(span);
        case "function":
          return this.#functionEnded(span);
        case "handoff":
          return this.#handoffEnded(span);
      }
    });
  }

  async shutdown(): Promise<void> {
    await this._recorder.flush();
  }

  async forceFlush(): Promise<void> {
    await this._recorder.flush();
  }

  // Spans.

  #agentEnded(span: SdkSpan): void {
    const otel = this.#take(span);
    if (otel === undefined) return;
    otel.updateName(`invoke_agent ${String(span.spanData.name)}`);
    otel.end();
  }

  #responseEnded(span: SdkSpan): void {
    const data = span.spanData;
    const response = data._response as Record<string, unknown> | undefined;
    this.#modelCall(span, {
      model: String(response?.model ?? ""),
      input: data._input,
      output: Array.isArray(response?.output) ? response.output : [],
      responseId: (response?.id ?? data.response_id) as string | undefined,
    });
  }

  #generationEnded(span: SdkSpan): void {
    // Chat Completions models report generations rather than responses.
    const data = span.spanData;
    this.#modelCall(span, { model: String(data.model ?? ""), input: data.input, output: outputItems(data.output), responseId: undefined });
  }

  #modelCall(span: SdkSpan, call: { model: string; input: unknown; output: unknown[]; responseId: string | undefined }): void {
    const traceKey = span.traceId;
    const agent = this.#agentFor(span);
    // The SDK leaves the input out without sensitive data.
    const hidden = call.input === undefined || call.input === null;
    const items: unknown[] = Array.isArray(call.input) ? call.input : call.input ? [{ role: "user", content: call.input }] : [];
    let parents: string[] = [];
    const origins: string[] = [];
    const linked = this.#linked.pop(traceKey);
    if (linked !== undefined) parents.push(...linked);
    // A run inside a recorded tool call, such as an agent used as a tool, shares the outer
    // trace, so no trace starts for it. It descends from the tool call: its first response
    // has the call as a parent, and its input isn't the user's.
    const tool = this.#enclosingTool(span);
    if (tool !== undefined && !this.#nested.has(tool[0])) {
      this.#nested.set(tool[0], true);
      parents.push(tool[1]);
    }
    const handoff = this.#handedOff.pop(key(traceKey, agent));
    if (handoff !== undefined) parents.push(handoff);
    if (hidden) {
      // The SDK's loop says what this response read: the agent's last response and the
      // tool results since. Its first reads the workflow's input.
      const last = this.#lastResponse.get(key(traceKey, agent));
      if (last !== undefined) parents.push(last);
      parents.push(...(this.#toolsSince.pop(key(traceKey, agent)) ?? []));
      if (!(linked || tool || handoff || last)) origins.push(events.ORIGIN_USER);
    }
    for (const item of items) {
      const type = field(item, "type");
      const role = field(item, "role");
      const callId = field(item, "callId", "call_id") ?? (role === "tool" ? field(item, "tool_call_id") : undefined);
      const itemId = field(item, "id");
      if ((type === "function_call_result" || type === "function_call_output" || role === "tool") && typeof callId === "string" && this.#eventByCall.has(callId)) {
        const toolEvent = this.#eventByCall.get(callId)!;
        parents.push(toolEvent, ...(this.#returnedByEvent.get(toolEvent) ?? []));
      } else if (typeof itemId === "string" && this.#eventByItem.has(itemId)) {
        parents.push(this.#eventByItem.get(itemId)!);
      } else if (role === "user") {
        // A user's message is new content the first time a model sees it. In a linked
        // workflow, or one run inside a tool call, the input came from what started it.
        const seen = key(traceKey, events.sha256(text(item)));
        if (!this.#seenUser.has(seen)) {
          this.#seenUser.set(seen, true);
          if (!linked && tool === undefined && !origins.includes(events.ORIGIN_USER)) origins.push(events.ORIGIN_USER);
        }
      }
    }
    const otel = this.#take(span);
    if (otel === undefined) return;
    const traceId = otel.spanContext().traceId;
    const inputText = text(items);
    if (inputText) parents = [...parents, ...this._matchingOutputs(traceId, inputText)];
    const eventId = randomUUID();
    this.#event(otel, span, eventId, events.MODEL_CALL, `chat ${call.model}`.trim(), agent, parents, origins, {
      "gen_ai.operation.name": "chat",
      "gen_ai.request.model": call.model,
    }, { input: items, output: call.output });
    this.#lastResponse.set(key(traceKey, agent), eventId);
    if (call.responseId) this.#eventByItem.set(call.responseId, eventId);
    for (const item of call.output) {
      const itemId = field(item, "id");
      if (typeof itemId === "string") this.#eventByItem.set(itemId, eventId);
      if (field(item, "type") === "function_call") {
        this.#call.set(key(traceKey, field(item, "name"), field(item, "arguments")), [field(item, "call_id", "callId") as string | undefined, eventId]);
      }
      // Chat Completions models report an assistant message with its tool calls.
      for (const requested of (field(item, "tool_calls") as unknown[] | undefined) ?? []) {
        const fn = field(requested, "function");
        this.#call.set(key(traceKey, field(fn, "name"), field(fn, "arguments")), [field(requested, "id") as string | undefined, eventId]);
      }
    }
    this._rememberOutput(traceId, eventId, text(call.output));
    otel.end();
  }

  #functionEnded(span: SdkSpan): void {
    const data = span.spanData;
    const name = String(data.name ?? "");
    const pending = this.#pending.pop(span.spanId);
    const returned = links._returned(pending?.frame);
    links._exit(pending?.frame);
    if (runningTools.get(span.spanId) === pending?.frame) runningTools.delete(span.spanId);
    const eventId = pending?.eventId ?? randomUUID();
    // What the tool got back came from these events, such as an A2A reply: they feed the
    // response that reads its output.
    if (returned.length > 0) this.#returnedByEvent.set(eventId, returned);
    const agent = this.#agentFor(span);
    const otel = this.#take(span);
    if (otel === undefined) return;
    const traceId = otel.spanContext().traceId;
    const parents: string[] = [];
    const input = data.input;
    const callKey = key(span.traceId, name, input);
    const call = this.#call.pop(callKey);
    const handoff = this.#handedOff.pop(key(span.traceId, agent));
    if (handoff !== undefined) parents.push(handoff);
    if (call !== undefined) {
      const [callId, requestedBy] = call;
      parents.push(requestedBy);
      if (callId) this.#eventByCall.set(callId, eventId);
    } else if (typeof input === "string" && input) {
      // No exact match: the arguments themselves point at the response that chose them.
      parents.push(...this._outputsContaining(traceId, [input]));
    } else if (input === undefined || input === null || input === "") {
      // Without sensitive data the SDK leaves the arguments out, but a tool only runs from
      // its agent's latest response, and its result feeds the agent's next one.
      const last = this.#lastResponse.get(key(span.traceId, agent));
      const since = this.#toolsSince.get(key(span.traceId, agent)) ?? [];
      this.#toolsSince.set(key(span.traceId, agent), [...since, eventId]);
      if (last !== undefined) parents.push(last);
    }
    const attributes: Record<string, string> = { "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name };
    const server = mcpServer(data.mcp_data);
    if (server) attributes["wake.mcp.server"] = server;
    this.#event(otel, span, eventId, events.TOOL_CALL, `execute_tool ${name}`, agent, parents, [`tool:${name}`], attributes, {
      input: input ?? null,
      output: data.output ?? null,
    });
    this._rememberOutput(traceId, eventId, text(data.output));
    otel.end();
  }

  #handoffEnded(span: SdkSpan): void {
    const data = span.spanData;
    const source = String(data.from_agent ?? "unknown");
    const target = String(data.to_agent ?? "unknown");
    const requestedBy = this.#lastResponse.get(key(span.traceId, source));
    const otel = this.#take(span);
    if (otel === undefined) return;
    const eventId = randomUUID();
    this.#event(otel, span, eventId, events.AGENT_MESSAGE, `agent_message ${source} -> ${target}`, target, requestedBy ? [requestedBy] : [], [`agent:${source}`], {
      "gen_ai.agent.name": target,
    }, { from: source, to: target });
    this.#handedOff.set(key(span.traceId, target), eventId);
    otel.end();
  }

  // Helpers.

  /** The recorded tool call a span runs inside, as its sdk span id and event id, if any. */
  #enclosingTool(span: SdkSpan): [string, string] | undefined {
    for (let sid = span.parentId, i = 0; sid !== null && sid !== undefined && i < 1000; sid = spanParents.get(sid) ?? null, i++) {
      const pending = this.#pending.get(sid);
      if (pending !== undefined) return [sid, pending.eventId];
    }
    return undefined;
  }

  #agentFor(span: SdkSpan): string {
    for (let sid = span.parentId, i = 0; sid !== null && sid !== undefined && i < 1000; sid = spanParents.get(sid) ?? null, i++) {
      const agent = this.#agentSpan.get(sid);
      if (agent !== undefined) return agent;
    }
    return this._recorder.agent;
  }

  /** The agent whose tool call is running, for a memory write it makes. */
  override _currentAgent(): string {
    const link = links.current();
    return (link !== undefined ? this.#toolAgents.get(link.eventId) : undefined) ?? this._recorder.agent;
  }

  /** Under the closest enclosing span Wake opened, else the workflow's root. */
  #context(span: SdkSpan): Context | undefined {
    for (let sid = span.parentId, i = 0; sid !== null && sid !== undefined && i < 1000; sid = spanParents.get(sid) ?? null, i++) {
      const otel = this.#spans.get(sid);
      if (otel !== undefined) return trace.setSpan(ROOT_CONTEXT, otel);
    }
    const root = this.#roots.get(span.traceId);
    return root !== undefined ? trace.setSpan(ROOT_CONTEXT, root) : contextOf(links.current());
  }

  #take(span: SdkSpan): OtelSpan | undefined {
    return this.#spans.pop(span.spanId);
  }

  #event(
    otel: OtelSpan,
    span: SdkSpan,
    eventId: string,
    kind: string,
    name: string,
    agent: string,
    parents: string[],
    origins: string[],
    attributes: Record<string, string>,
    payload: Record<string, unknown>,
  ): void {
    parents = unique(parents);
    this._rememberLineage(eventId, parents, origins);
    otel.updateName(name);
    otel.setAttributes({
      [events.EVENT_ID]: eventId,
      [events.EVENT_KIND]: kind,
      [events.AGENT_ID]: agent,
      [events.CAPTURE]: "sdk",
      [events.PARENTS]: parents,
      [events.ORIGINS]: origins,
      ...attributes,
    });
    if (span.error) {
      payload = { ...payload, error: span.error };
      otel.setStatus({ code: SpanStatusCode.ERROR, message: String(span.error.message ?? "error") });
    }
    otel.setAttributes(events.payloadAttributes(payload));
    const ctx = otel.spanContext();
    links._note(new links.Link(eventId, ctx.traceId, ctx.spanId));
  }

  #safely(what: string, fn: () => void): void {
    try {
      fn();
    } catch (error) {
      log.warn(`failed to record ${what}`, error);
      failed(this._recorder, what);
    }
  }
}

/** The MCP server a tool call went to, from the SDK's mcp_data. */
function mcpServer(data: unknown): string | undefined {
  let value = data;
  if (typeof value === "string") {
    try {
      value = JSON.parse(value);
    } catch {
      return undefined;
    }
  }
  const server = field(value, "server");
  return server === undefined || server === null ? undefined : String(server);
}
