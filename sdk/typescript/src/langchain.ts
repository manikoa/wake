/**
 * Records model calls, tool calls and handoffs between agents from LangChain and LangGraph runs.
 *
 *     const handler = new WakeCallbackHandler(new Recorder({ agent: "email-agent" }));
 *     await graph.invoke(inputs, { callbacks: [handler] });
 *
 * Or record every LangChain call made from here on, and name the agent behind each run:
 *
 *     const handler = new WakeCallbackHandler(new Recorder({ agent: "clavr" })).install();
 *
 *     await handler.agent("supervisor", { input: query, origin: "user" }, async () => {
 *       ...
 *       await handler.agent("email", { input: step }, async (run) => { // a handoff
 *         run.reply(await emailAgent.run(step));
 *       });
 *     });
 *
 * A run started inside another agent's run is an instance of its own, <name>#<its span id>,
 * started by that run (docs/events.md), so two runs of one agent at once can be told apart.
 *
 * Causal edges follow the messages: a model call's parents are the events that produced the
 * AI and tool messages in its context, and a tool call's parent is the model call that
 * requested it. A message from one run to another descends from the sender's latest event,
 * however the app words it. When an app builds its prompts from strings, so no message ids
 * link them, an event's parents are the earlier events in the same trace whose output appears
 * in its input. See docs/events.md.
 *
 * A tool made with LangChain's tool() runs with its event current, so an MCP or A2A call it
 * makes, or a memory write, links back to it, and so does a run it starts.
 *
 * Given the brake's rules (new WakeCallbackHandler(recorder, { quarantine })), the handler also
 * stops an agent, or one run of it, in its own process: agent() refuses to start a run a halt
 * covers, throwing AgentHalted, and a LangGraph WakeToolNode (wake-sdk/langgraph) refuses a tool
 * call a halt covers before it runs, telling the model why. Any other tool call a halt covers,
 * as from createReactAgent's ToolNode, a node of the app's own or a direct invoke, is refused
 * as it starts: the handler throws AgentHalted from its tool start callback, and its raiseError
 * has LangChain pass that on, so the tool never runs. A ToolNode answers the model with it as
 * an error message, as it does any error by default; with handleToolErrors off it throws,
 * ending the graph's run. Each refusal is recorded as denied, once. It's a cooperative stop: it
 * holds only while the agent's code does.
 *
 * The same checks apply the flow policy the rules carry (flow.ts), once no halt covers the
 * call: a tool call, or a run agent() would start, by the agent it reaches, is refused the same
 * way when the policy keeps it from what entered the agent making it, the named run's, or the
 * recorder's outside any. A tool's result enters that agent once the tool has run, by the
 * tool's name. A run started inside another starts with what had entered that one, entering
 * as agent:<that run's agent>, and its reply, and what it returns, bring back what entered it.
 */

import { randomUUID } from "node:crypto";
import { AsyncLocalStorage } from "node:async_hooks";

import { BaseCallbackHandler } from "@langchain/core/callbacks/base";
import { registerConfigureHook, setContextVariable } from "@langchain/core/context";
import { ToolMessage, type BaseMessage } from "@langchain/core/messages";
import type { LLMResult } from "@langchain/core/outputs";
import { AsyncLocalStorageProviderSingleton } from "@langchain/core/singletons";
import { ROOT_CONTEXT, SpanStatusCode, trace, type Context, type Span, type SpanContext } from "@opentelemetry/api";

import * as events from "./events.ts";
import { entered } from "./flow.ts";
import { failed, Graph, MIN_MATCH, Recent, contextOf, traceIdOf, unique } from "./graph.ts";
import * as links from "./links.ts";
import { log } from "./log.ts";
import type { Recorder } from "./recorder.ts";
import { registerToolCall, unregisterToolCall } from "./registry.ts";
import { AgentHalted, flowRefusal, keptOf, refusal, stopsWith, unchecked, type Halts, type Refusal } from "./stop.ts";

export { AgentHalted } from "./stop.ts";

const currentRun = new AsyncLocalStorage<AgentRun | undefined>();

// Tool calls running now, by LangChain run id, and each run's parent run, shared by every
// handler: run ids are unique. With them, the tool call whose function runs now is found
// from the config LangChain keeps current while it runs, even inside a nested run.
const runningTools = new Recent<string, links.Frame>();
const parentRuns = new Recent<string, string | undefined>();

function toolRunAbove(runId: string | undefined): [string, links.Frame] | undefined {
  for (let i = 0; runId !== undefined && i < 1000; i++) {
    const frame = runningTools.get(runId);
    if (frame !== undefined && !frame.ended) return [runId, frame];
    runId = parentRuns.get(runId);
  }
  return undefined;
}

links._addResolver(() => {
  const callbacks = (AsyncLocalStorageProviderSingleton.getRunnableConfig() as { callbacks?: unknown } | undefined)?.callbacks;
  const parent = (callbacks as { getParentRunId?: () => string | undefined } | undefined)?.getParentRunId?.();
  return toolRunAbove(parent)?.[1];
});

const HANDLER_NAME = "wake_callback_handler";
const INSTALLED = "wake_callback_handler";

interface Pending {
  eventId: string;
  inputs: unknown;
  toolCallId?: string;
  frame?: links.Frame;
}

/** One run of one agent. Events recorded inside it carry its agent id and share its trace. */
export class AgentRun {
  readonly agent: string;
  readonly _span: Span | undefined;
  readonly _parent: AgentRun | undefined;
  // Started inside another agent's run, it's an instance of its own, started by that one.
  readonly instance: string | undefined;
  readonly lineage: readonly string[] = [];
  readonly _handler: WakeCallbackHandler;
  // The message that started this run feeds its next event.
  _nextParents: string[] = [];
  _nextOrigins: string[] = [];
  // The latest event recorded in this run, which a message it sends descends from.
  _latest: string | undefined;

  constructor(handler: WakeCallbackHandler, agent: string, span: Span | undefined, parent: AgentRun | undefined) {
    this._handler = handler;
    this.agent = agent;
    this._span = span;
    this._parent = parent;
    if (parent !== undefined) {
      this.instance = `${agent}#${span?.spanContext().spanId ?? randomUUID().replaceAll("-", "").slice(0, 16)}`;
      this.lineage = [...parent.lineage, parent.instance ?? parent.agent];
    }
  }

  /** Records this agent's answer going back to the agent that handed off to it. It brings that agent what had entered this run. */
  reply(content: unknown): void {
    if (this._parent === undefined) return;
    entered.message(this.agent, this._parent.agent);
    try {
      this._handler._recordMessage(this, this._parent, content);
    } catch (error) {
      log.warn("failed to record reply", error);
      failed(this._handler._recorder, "reply");
    }
  }

  _takeNext(): [string[], string[]] {
    const taken: [string[], string[]] = [this._nextParents, this._nextOrigins];
    this._nextParents = [];
    this._nextOrigins = [];
    return taken;
  }
}

export interface AgentOptions {
  /** Inside another agent's run, a message from that agent; at the top, what the run was given. */
  input?: unknown;
  /** At the top, where input came from, such as "user". */
  origin?: string;
}

/**
 * What install() registers with LangChain: a handler that passes each callback on to the
 * installed one while it's installed. LangChain adds it to every run made where it was
 * installed, and skips it for a run already given the handler itself.
 */
class Installed extends BaseCallbackHandler {
  name = HANDLER_NAME;
  handler: WakeCallbackHandler | undefined;

  constructor(handler: WakeCallbackHandler) {
    // It passes on a refused tool call's AgentHalted, as the handler does.
    super({ _awaitHandler: true, raiseError: true });
    this.handler = handler;
  }

  override copy(): this {
    return this;
  }
}
for (const method of [
  "handleChainStart",
  "handleChainEnd",
  "handleChainError",
  "handleChatModelStart",
  "handleLLMEnd",
  "handleLLMError",
  "handleToolStart",
  "handleToolEnd",
  "handleToolError",
] as const) {
  Object.defineProperty(Installed.prototype, method, {
    value(this: Installed, ...args: unknown[]) {
      return (this.handler?.[method] as ((...a: unknown[]) => unknown) | undefined)?.apply(this.handler, args);
    },
  });
}

export class WakeCallbackHandler extends Graph(BaseCallbackHandler) {
  name = HANDLER_NAME;
  readonly #spans = new Recent<string, Span>(); // open spans by run id
  readonly #pending = new Recent<string, Pending>(); // open events by run id
  readonly #eventByMessage = new Recent<string, string>(); // AI message id -> model call event id
  readonly #eventByToolCall = new Recent<string, string>(); // tool call id -> model call that requested it
  readonly #resultByToolCall = new Recent<string, string>(); // tool call id -> tool call event id
  readonly #returnedByEvent = new Recent<string, string[]>(); // tool call event id -> events its result came from
  readonly #seenUserMessages = new Recent<string, true>(); // message id, or trace id and digest for one with none
  readonly #linked = new Recent<string, { parents: string[]; taken: boolean }>(); // top level run id -> the link it started under
  readonly #underTool = new Recent<string, true>(); // tool call run id whose nested run's first event is recorded
  readonly #quarantine: Halts | undefined;
  readonly #callContext = new Recent<string, SpanContext>(); // tool call id -> the model call that requested it
  readonly #entering = new Recent<string, [agent: string, tool: string]>(); // tool call run id -> the agent its result enters, and the tool
  #installed: Installed | undefined;

  /**
   * With quarantine, the brake's rules for the agent (Quarantine), agent(), WakeToolNode and
   * every tool call stop what a halt covers, and what the flow policy they carry keeps.
   */
  constructor(recorder: Recorder, { quarantine }: { quarantine?: Halts } = {}) {
    // Callbacks are awaited, in order, so a tool call finds the model call that requested
    // it and is recorded before the tool runs. The work done here is small and never waits
    // on the network. LangChain passes on what a callback throws: recording never throws, so
    // the only error that reaches it is a refused tool call's AgentHalted.
    super({ _awaitHandler: true, raiseError: true });
    this._recorder = recorder;
    this.#quarantine = quarantine;
    stopsWith(quarantine);
  }

  override copy(): this {
    return this;
  }

  /**
   * Records every LangChain run made from here on, in this task and the tasks it starts,
   * without passing callbacks at each call. Install it where the app starts.
   */
  install(): this {
    this.#installed ??= new Installed(this);
    this.#installed.handler = this;
    registerConfigureHook({ contextVar: INSTALLED, inheritable: true });
    setContextVariable(INSTALLED, this.#installed);
    return this;
  }

  /** Stops recording runs that don't pass this handler explicitly. */
  uninstall(): void {
    if (this.#installed !== undefined) this.#installed.handler = undefined;
  }

  /**
   * Names the agent behind the events recorded inside fn.
   *
   * Inside another agent's run, input is a message from that agent: it's recorded as an
   * agent_message event that feeds this run's next event. At the top, origin labels where
   * input came from, such as "user". Call reply() on the run to record the answer going back.
   *
   * With the brake's rules, a run a halt covers, by its name or what starts it, isn't started:
   * this throws AgentHalted, and the attempt is recorded as denied. Nor is one the flow policy
   * keeps from what entered the run starting it, or the recorder's agent at the top.
   *
   * Started inside another run, it starts with what had entered that run, and what it returns,
   * or throws, brings that run what entered it.
   */
  agent<T>(name: string, fn: (run: AgentRun) => T): T;
  agent<T>(name: string, options: AgentOptions, fn: (run: AgentRun) => T): T;
  agent<T>(name: string, optionsOrFn: AgentOptions | ((run: AgentRun) => T), maybeFn?: (run: AgentRun) => T): T {
    const [options, fn] = typeof optionsOrFn === "function" ? [{}, optionsOrFn] : [optionsOrFn, maybeFn!];
    this.#refuseStart(name, options.input);
    const run = this.#startRun(name, options.input, options.origin);
    const starter = run._parent?.agent;
    if (starter !== undefined) entered.message(starter, name);
    const answered = () => {
      if (starter !== undefined) entered.message(name, starter);
    };
    let result: T;
    try {
      result = currentRun.run(run, () => fn(run));
    } catch (error) {
      this.#endSpan(run._span, error);
      answered();
      throw error;
    }
    if (result instanceof Promise) {
      return result.then(
        (value) => {
          this.#endSpan(run._span);
          answered();
          return value;
        },
        (error) => {
          this.#endSpan(run._span, error);
          answered();
          throw error;
        },
      ) as T;
    }
    this.#endSpan(run._span);
    answered();
    return result;
  }

  // Structure: one invoke_agent span per top level run groups its events into a trace.

  // The callback manager passes the parent run fourth, whatever the declared signature says.
  override handleChainStart(_chain: unknown, _inputs: unknown, runId: string, parentRunId?: string): void {
    this.#safely("handleChainStart", () => {
      parentRuns.set(runId, parentRunId);
      if (parentRunId !== undefined || currentRun.getStore() !== undefined) return;
      // A run inside a recorded tool call, or for a message from another agent, joins that
      // trace, and its first event descends from it.
      const agent = this._recorder.agent;
      const link = links.current();
      const span = this._recorder.tracer.startSpan(
        `invoke_agent ${agent}`,
        { attributes: { "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": agent, [events.AGENT_ID]: agent } },
        contextOf(link),
      );
      this.#spans.set(runId, span);
      if (link !== undefined) this.#linked.set(runId, { parents: link.parents, taken: false });
    });
  }

  override handleChainEnd(_outputs: unknown, runId: string): void {
    this.#safely("handleChainEnd", () => this.#endSpan(this.#spans.pop(runId)));
  }

  override handleChainError(error: unknown, runId: string): void {
    this.#safely("handleChainError", () => this.#endSpan(this.#spans.pop(runId), error));
  }

  // Model calls.

  override handleChatModelStart(
    serialized: unknown,
    messages: BaseMessage[][],
    runId: string,
    parentRunId?: string,
    extraParams?: Record<string, unknown>,
    _tags?: string[],
    metadata?: Record<string, unknown>,
  ): void {
    this.#safely("handleChatModelStart", () => {
      metadata ??= {};
      const context = messages.flat();
      const parents: string[] = [];
      const origins: string[] = [];
      const linked = this.#isLinked(parentRunId);
      // The trace this call joins, if it doesn't start one of its own.
      const traceId = traceIdOf(this.#contextFor(parentRunId) ?? contextOf(links.current()));
      for (const m of context) {
        const type = messageType(m);
        const toolCallId = (m as { tool_call_id?: string }).tool_call_id;
        if (type === "ai" && m.id && this.#eventByMessage.has(m.id)) {
          parents.push(this.#eventByMessage.get(m.id)!);
        } else if (type === "tool" && toolCallId && this.#resultByToolCall.has(toolCallId)) {
          const toolEvent = this.#resultByToolCall.get(toolCallId)!;
          parents.push(toolEvent, ...(this.#returnedByEvent.get(toolEvent) ?? []));
        } else if (type === "human" && currentRun.getStore() === undefined && !linked) {
          // A user's message is new content the first time a model sees it. Inside a named
          // agent run, a human message is usually a prompt the app built, so the run's input
          // says where content came from instead; in a linked run, or one inside a tool
          // call, what started it does. A message with no id is known by its text within
          // its trace.
          let key = m.id;
          if (!key && traceId) key = `${traceId}:${events.sha256(text(m.content))}`;
          if (key && this.#seenUserMessages.has(key)) continue;
          if (key) this.#seenUserMessages.set(key, true);
          if (!origins.includes(events.ORIGIN_USER)) origins.push(events.ORIGIN_USER);
        }
      }
      const model = modelName(serialized, extraParams, metadata);
      this._startEvent(runId, parentRunId, {
        name: `chat ${model}`.trim(),
        kind: events.MODEL_CALL,
        parents,
        origins,
        metadata,
        attributes: { "gen_ai.operation.name": "chat", "gen_ai.request.model": model },
        inputs: { model, messages: context.map(message) },
        text: context.map((m) => text(m.content)).join("\n"),
      });
    });
  }

  override handleLLMEnd(response: LLMResult, runId: string): void {
    this.#safely("handleLLMEnd", () => {
      const pending = this.#pending.get(runId);
      if (pending === undefined) return;
      const outputs: unknown[] = [];
      const texts: string[] = [];
      for (const generations of response.generations ?? []) {
        for (const generation of generations) {
          const m = (generation as { message?: BaseMessage }).message;
          if (m === undefined) {
            outputs.push({ text: generation.text });
            texts.push(generation.text);
            continue;
          }
          outputs.push(message(m));
          texts.push(text(m.content));
          if (m.id) this.#eventByMessage.set(m.id, pending.eventId);
          const span = this.#spans.get(runId);
          for (const call of (m as { tool_calls?: { id?: string }[] }).tool_calls ?? []) {
            if (call.id) this.#eventByToolCall.set(call.id, pending.eventId);
            // Where a refusal of the call is recorded, if it's refused.
            if (call.id && this.#quarantine !== undefined && span !== undefined) this.#callContext.set(call.id, span.spanContext());
          }
        }
      }
      this.#endEvent(runId, outputs, undefined, texts.join("\n"));
    });
  }

  override handleLLMError(error: unknown, runId: string): void {
    this.#safely("handleLLMError", () => this.#endEvent(runId, [], error));
  }

  // Tool calls.

  override handleToolStart(
    tool: unknown,
    input: string,
    runId: string,
    parentRunId?: string,
    _tags?: string[],
    metadata?: Record<string, unknown>,
    runName?: string,
    toolCallId?: string,
  ): void {
    const serialized = (tool ?? {}) as { name?: string; kwargs?: { name?: string } };
    const name = runName || serialized?.name || serialized?.kwargs?.name || "tool";
    // The stop for any tool call a WakeToolNode didn't refuse: thrown here, before the tool's
    // run is opened, it never runs, and nothing but the refusal is recorded.
    const refused = this.#refuseTool({ name, id: toolCallId, args: parsed(input) });
    if (refused !== undefined) throw new AgentHalted(refused);
    // Its result enters the agent making it once it has run, whether or not it's recorded.
    this.#safely("tool call entering", () => this.#entering.set(runId, [currentRun.getStore()?.agent ?? this._recorder.agent, name]));
    this.#safely("handleToolStart", () => {
      parentRuns.set(runId, parentRunId);
      const requestedBy = toolCallId ? this.#eventByToolCall.get(toolCallId) : undefined;
      const attributes: Record<string, string> = { "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name };
      if (toolCallId) attributes["gen_ai.tool.call.id"] = toolCallId;
      this._startEvent(runId, parentRunId, {
        name: `execute_tool ${name}`,
        kind: events.TOOL_CALL,
        parents: requestedBy ? [requestedBy] : [],
        // The tool's result is new content entering the system here.
        origins: [`tool:${name}`],
        metadata: metadata ?? {},
        attributes,
        inputs: input,
        toolCallId,
        // Without a tool call id, the arguments themselves point at the model call that chose them.
        arguments: requestedBy ? undefined : argumentTexts(input),
      });
    });
  }

  override handleToolEnd(output: unknown, runId: string): void {
    this.#entered(runId);
    this.#safely("handleToolEnd", () => {
      this.#rememberToolResult(runId, output);
      this.#endEvent(runId, toolOutput(output), undefined, text(output));
    });
  }

  override handleToolError(error: unknown, runId: string): void {
    // What a tool that failed says enters the agent too: its error can carry what it read.
    this.#entered(runId);
    this.#safely("handleToolError", () => {
      this.#rememberToolResult(runId, undefined);
      this.#endEvent(runId, null, error);
    });
  }

  /** Notes that the result of the tool call runId names entered the agent that made it, as tool:<the tool's name>. */
  #entered(runId: string): void {
    const call = this.#entering.pop(runId);
    if (call !== undefined) entered.note(call[0], `tool:${call[1]}`);
  }

  // Cooperative stops.

  /**
   * The answer to a tool call a halt covers, recorded as denied; undefined for a call to run.
   * WakeToolNode (wake-sdk/langgraph) asks before each call.
   */
  _toolRefusal(call: { name?: string; args?: unknown; id?: string }): ToolMessage | undefined {
    const refused = this.#refuseTool(call);
    if (refused === undefined) return undefined;
    return new ToolMessage({ content: refused[1], tool_call_id: call.id ?? "", name: call.name || "tool", status: "error" });
  }

  /**
   * The refusal of a tool call about to run, if a halt covers the agent making it, the run it's
   * in or what started that run, or the flow policy keeps it from what entered that agent,
   * recorded as denied; undefined for a call to run. Never throws: a check that fails refuses.
   */
  #refuseTool(call: { name?: string; args?: unknown; id?: string }): Refusal | undefined {
    if (this.#quarantine === undefined) return undefined;
    const run = currentRun.getStore();
    const name = call.name || "tool";
    let who = this._recorder.agent;
    let refused: Refusal | undefined;
    try {
      const names = [this._recorder.agent];
      if (run !== undefined) {
        names.push(run.agent, ...run.lineage);
        if (run.instance !== undefined) names.push(run.instance);
        who = run.instance ?? run.agent;
      }
      refused = refusal(this.#quarantine, who, names);
      if (refused === undefined) refused = flowRefusal(this.#quarantine, who, { caller: run?.agent ?? this._recorder.agent, lineage: names, tool: name });
    } catch (error) {
      log.warn("couldn't check the halts for a tool call; refusing it", error);
      refused = unchecked();
    }
    if (refused === undefined) return undefined;
    const stopped = refused;
    this.#safely("refused tool call", () => this.#refusedCall(run, name, call, stopped));
    log.warn(`refused a ${name} call of ${who}: ${refused[0]}`);
    return refused;
  }

  /**
   * Throws AgentHalted, recording the attempt, if a halt covers the run of name about to start
   * here, or the flow policy keeps it from what entered the run starting it.
   */
  #refuseStart(name: string, input: unknown): void {
    if (this.#quarantine === undefined) return;
    const enclosing = currentRun.getStore();
    const outcome = `this run of ${name} wasn't started`;
    let refused: Refusal | undefined;
    try {
      const lineage = enclosing !== undefined ? [...enclosing.lineage, enclosing.instance ?? enclosing.agent] : [];
      refused = refusal(this.#quarantine, name, [this._recorder.agent, name, ...lineage], { outcome });
      if (refused === undefined) {
        // It's reached from the run starting it, or from the recorder's agent at the top.
        const caller = enclosing?.agent ?? this._recorder.agent;
        const who = enclosing !== undefined ? (enclosing.instance ?? enclosing.agent) : caller;
        refused = flowRefusal(this.#quarantine, who, { caller, lineage: [this._recorder.agent, ...lineage], receiver: name }, outcome);
      }
    } catch (error) {
      log.warn(`couldn't check the halts for a run of ${name}; refusing it`, error);
      refused = unchecked(outcome);
    }
    if (refused === undefined) return;
    const stopped = refused;
    this.#safely("refused run", () => this.#refusedStart(enclosing, name, input, stopped));
    log.warn(`refused to start a run of ${name}: ${refused[0]}`);
    throw new AgentHalted(refused);
  }

  /**
   * Records a tool call a halt or the flow policy refused before it ran: what the agent tried,
   * under the model call that asked for it, and what the policy kept from it. The tool never
   * ran, so it has no origins. The model's next call reads the refusal.
   */
  #refusedCall(run: AgentRun | undefined, name: string, call: { args?: unknown; id?: string }, refused: Refusal): void {
    const [halt, reason] = refused;
    const requestedBy = call.id ? this.#eventByToolCall.get(call.id) : undefined;
    const requester = call.id ? this.#callContext.get(call.id) : undefined;
    const context =
      requester !== undefined
        ? trace.setSpanContext(ROOT_CONTEXT, requester)
        : run?._span !== undefined
          ? trace.setSpan(ROOT_CONTEXT, run._span)
          : this._eventContext();
    const attributes: Record<string, string> = { "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name, "wake.brake.halt_id": halt };
    if (call.id) attributes["gen_ai.tool.call.id"] = call.id;
    const payload = { input: call.args ?? null, output: reason, ...keptOf(refused) };
    const eventId = this.#denied(`execute_tool ${name}`, run, requestedBy !== undefined ? [requestedBy] : [], [], attributes, payload, context);
    if (call.id) this.#resultByToolCall.set(call.id, eventId);
  }

  /**
   * Records a run a halt or the flow policy refused to start: the attempt to hand it its task,
   * in the name of what tried, descending from what tried's latest event, and what the policy
   * kept from it. Nothing entered the run, so it has no origins.
   */
  #refusedStart(sender: AgentRun | undefined, name: string, content: unknown, refused: Refusal): void {
    const context = sender?._span !== undefined ? trace.setSpan(ROOT_CONTEXT, sender._span) : this._eventContext();
    const traceId = traceIdOf(context);
    const parents = [...this.#latestOf(sender), ...(traceId && content !== undefined && content !== null ? this._matchingOutputs(traceId, text(content)) : [])];
    const agent = sender?.agent ?? this._recorder.agent;
    this.#denied(
      `agent_message ${agent} -> ${name}`,
      sender,
      parents,
      [],
      { "gen_ai.agent.name": name, "wake.brake.halt_id": refused[0], "wake.brake.receiver": name },
      { from: agent, to: name, content: content ?? null, ...keptOf(refused) },
      context,
    );
  }

  /** Records a denied event, ended as soon as it starts, and returns its id. */
  #denied(
    name: string,
    run: AgentRun | undefined,
    parents: string[],
    origins: string[],
    attributes: Record<string, string>,
    payload: unknown,
    context: Context | undefined,
  ): string {
    const eventId = randomUUID();
    parents = unique(parents);
    this._rememberLineage(eventId, parents, origins);
    this.#latestIn(run, eventId);
    const span = this._recorder.tracer.startSpan(
      name,
      {
        attributes: {
          [events.EVENT_ID]: eventId,
          [events.EVENT_KIND]: events.DENIED,
          [events.AGENT_ID]: run?.agent ?? this._recorder.agent,
          ...(run !== undefined ? events.instanceAttributes(run.instance, run.lineage) : {}),
          [events.CAPTURE]: "sdk",
          [events.PARENTS]: parents,
          [events.ORIGINS]: origins,
          ...attributes,
          ...events.payloadAttributes(payload),
        },
      },
      context,
    );
    span.end();
    const ctx = span.spanContext();
    links._note(new links.Link(eventId, ctx.traceId, ctx.spanId));
    return eventId;
  }

  // Agent runs and the messages between them.

  #startRun(name: string, input: unknown, origin: string | undefined): AgentRun {
    const enclosing = currentRun.getStore();
    const link = enclosing === undefined ? links.current() : undefined;
    let span: Span;
    try {
      const context =
        enclosing !== undefined
          ? enclosing._span !== undefined
            ? trace.setSpan(ROOT_CONTEXT, enclosing._span)
            : undefined
          : contextOf(link);
      span = this._recorder.tracer.startSpan(
        `invoke_agent ${name}`,
        { attributes: { "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": name, [events.AGENT_ID]: name } },
        context,
      );
    } catch (error) {
      log.warn("failed to record agent run", error);
      failed(this._recorder, "agent run");
      return new AgentRun(this, name, undefined, enclosing);
    }
    const run = new AgentRun(this, name, span, enclosing);
    span.setAttributes(events.instanceAttributes(run.instance, run.lineage));
    try {
      if (enclosing !== undefined && input !== undefined && input !== null) {
        const messageId = this._recordMessage(enclosing, run, input);
        if (messageId) run._nextParents.push(messageId);
      } else {
        if (link !== undefined) run._nextParents.push(...link.parents);
        if (origin !== undefined) run._nextOrigins.push(origin);
      }
    } catch (error) {
      log.warn("failed to record handoff", error);
      failed(this._recorder, "handoff");
    }
    return run;
  }

  /**
   * Records a message from one agent to another as an event in the receiver's run. Its parents
   * are the sender's latest event, since an app may word the message its own way, and the
   * events whose output it carries. Its origin is the sender: this is where the content enters
   * the receiver.
   */
  _recordMessage(sender: AgentRun, receiver: AgentRun, content: unknown): string | undefined {
    if (receiver._span === undefined) return undefined;
    const body = text(content);
    const traceId = receiver._span.spanContext().traceId;
    const eventId = randomUUID();
    const parents = unique([...this.#latestOf(sender), ...this._matchingOutputs(traceId, body)]);
    this._rememberLineage(eventId, parents, [`agent:${sender.agent}`]);
    this.#latestIn(receiver, eventId);
    const span = this._recorder.tracer.startSpan(
      `agent_message ${sender.agent} -> ${receiver.agent}`,
      {
        attributes: {
          [events.EVENT_ID]: eventId,
          [events.EVENT_KIND]: events.AGENT_MESSAGE,
          [events.AGENT_ID]: receiver.agent,
          ...events.instanceAttributes(receiver.instance, receiver.lineage),
          [events.CAPTURE]: "sdk",
          [events.PARENTS]: parents,
          [events.ORIGINS]: [`agent:${sender.agent}`],
          "gen_ai.agent.name": receiver.agent,
          ...events.payloadAttributes({ from: sender.agent, to: receiver.agent, content }),
        },
      },
      trace.setSpan(ROOT_CONTEXT, receiver._span),
    );
    span.end();
    const ctx = span.spanContext();
    links._note(new links.Link(eventId, ctx.traceId, ctx.spanId));
    this._rememberOutput(traceId, eventId, body);
    return eventId;
  }

  // Helpers.

  _startEvent(
    runId: string,
    parentRunId: string | undefined,
    event: {
      name: string;
      kind: string;
      parents: string[];
      origins: string[];
      metadata: Record<string, unknown>;
      attributes: Record<string, string>;
      inputs: unknown;
      text?: string;
      arguments?: string[];
      toolCallId?: string;
    },
  ): void {
    const run = currentRun.getStore();
    const agent = run?.agent ?? this._recorder.agent;
    let { parents, origins } = event;
    // A run inside a recorded tool call descends from it (docs/linking.md).
    const underTool = this.#takeToolParent(parentRunId);
    if (run !== undefined) {
      const [nextParents, nextOrigins] = run._takeNext();
      parents = [...underTool, ...nextParents, ...parents];
      origins = [...origins, ...nextOrigins.filter((o) => !origins.includes(o))];
    } else {
      parents = [...(underTool.length > 0 ? underTool : this.#takeLinked(parentRunId)), ...parents];
    }
    let context = this.#contextFor(parentRunId);
    if (context === undefined && run?._span !== undefined) context = trace.setSpan(ROOT_CONTEXT, run._span);
    context ??= contextOf(links.current());
    const traceId = traceIdOf(context);
    if (traceId) {
      if (event.text) parents = [...parents, ...this._matchingOutputs(traceId, event.text)];
      if (event.arguments?.length) parents = [...parents, ...this._outputsContaining(traceId, event.arguments)];
    }
    const eventId = randomUUID();
    parents = unique(parents);
    this._rememberLineage(eventId, parents, origins);
    this.#latestIn(run, eventId);
    const attributes: Record<string, string | string[]> = {
      [events.EVENT_ID]: eventId,
      [events.EVENT_KIND]: event.kind,
      [events.AGENT_ID]: agent,
      ...(run !== undefined ? events.instanceAttributes(run.instance, run.lineage) : {}),
      [events.CAPTURE]: "sdk",
      [events.PARENTS]: parents,
      [events.ORIGINS]: origins,
      ...event.attributes,
    };
    if (typeof event.metadata.langgraph_node === "string" && event.metadata.langgraph_node) {
      attributes["wake.langgraph.node"] = event.metadata.langgraph_node;
    }
    const span = this._recorder.tracer.startSpan(event.name, { attributes }, context);
    const ctx = span.spanContext();
    const link = new links.Link(eventId, ctx.traceId, ctx.spanId);
    links._note(link);
    let frame: links.Frame | undefined;
    if (event.kind === events.TOOL_CALL) {
      // Calls the tool makes, such as to an MCP server through the proxy, link back to this
      // event: it's current while the tool's function runs.
      frame = links._frame(link);
      runningTools.set(runId, frame);
      registerToolCall(event.toolCallId, frame);
    }
    this.#spans.set(runId, span);
    this.#pending.set(runId, { eventId, inputs: event.inputs, toolCallId: event.toolCallId, frame });
  }

  #endEvent(runId: string, outputs: unknown, error?: unknown, body = ""): void {
    const span = this.#spans.pop(runId);
    const pending = this.#pending.pop(runId);
    if (span === undefined || pending === undefined) return;
    try {
      const returned = links._returned(pending.frame);
      if (pending.frame !== undefined) {
        links._exit(pending.frame);
        if (runningTools.get(runId) === pending.frame) runningTools.delete(runId);
        unregisterToolCall(pending.toolCallId, pending.frame);
      }
      // What the tool got back came from these events, such as an A2A reply: they feed the
      // step that reads its result.
      if (returned.length > 0) this.#returnedByEvent.set(pending.eventId, returned);
      const payload: Record<string, unknown> = { input: pending.inputs, output: outputs };
      if (error !== undefined) {
        payload.error = String(error);
        span.setStatus({ code: SpanStatusCode.ERROR, message: String(error) });
      }
      span.setAttributes(events.payloadAttributes(payload));
    } finally {
      // Whatever failed above, the event is still sent, and the failure counted.
      span.end();
    }
    this._rememberOutput(span.spanContext().traceId, pending.eventId, body);
  }

  #endSpan(span: Span | undefined, error?: unknown): void {
    if (span === undefined) return;
    try {
      if (error !== undefined) span.setStatus({ code: SpanStatusCode.ERROR, message: String(error) });
      span.end();
    } catch (e) {
      log.warn("failed to end span", e);
      failed(this._recorder, "span end");
    }
  }

  /** Under the tool call making it, else the agent run. */
  override _eventContext(): Context | undefined {
    const context = super._eventContext();
    if (context !== undefined) return context;
    const run = currentRun.getStore();
    return run?._span !== undefined ? trace.setSpan(ROOT_CONTEXT, run._span) : undefined;
  }

  /** Notes the run's latest event, if it's one of this handler's runs: another handler recording the same calls keeps its own. */
  #latestIn(run: AgentRun | undefined, eventId: string): void {
    if (run?._handler === this) run._latest = eventId;
  }

  /** The run's latest event, as a parent, if it's one of this handler's runs. */
  #latestOf(run: AgentRun | undefined): string[] {
    return run?._handler === this && run._latest !== undefined ? [run._latest] : [];
  }

  override _currentAgent(): string {
    return currentRun.getStore()?.agent ?? this._recorder.agent;
  }

  override _currentInstance(): [string | undefined, readonly string[]] {
    const run = currentRun.getStore();
    return run !== undefined ? [run.instance, run.lineage] : [undefined, []];
  }

  #rememberToolResult(runId: string, output: unknown): void {
    const pending = this.#pending.get(runId);
    if (pending === undefined) return;
    const toolCallId = pending.toolCallId || (output as { tool_call_id?: string } | undefined)?.tool_call_id;
    if (toolCallId) this.#resultByToolCall.set(toolCallId, pending.eventId);
  }

  /** The top level run this one is part of. */
  #rootOf(runId: string): string {
    for (let i = 0; i < 1000; i++) {
      const parent = parentRuns.get(runId);
      if (parent === undefined) break;
      runId = parent;
    }
    return runId;
  }

  /** The enclosing tool call, if this is the first event of a run inside it. */
  #takeToolParent(parentRunId: string | undefined): string[] {
    const tool = toolRunAbove(parentRunId);
    if (tool === undefined || this.#underTool.has(tool[0])) return [];
    this.#underTool.set(tool[0], true);
    return [tool[1].link.eventId];
  }

  #isLinked(parentRunId: string | undefined): boolean {
    if (parentRunId === undefined) return links.current() !== undefined;
    return toolRunAbove(parentRunId) !== undefined || this.#linked.has(this.#rootOf(parentRunId));
  }

  /** What started this event's top level run, if it's the run's first event. */
  #takeLinked(parentRunId: string | undefined): string[] {
    if (parentRunId === undefined) {
      // A call with no run around it is a top level run of its own.
      return links.current()?.parents ?? [];
    }
    const entry = this.#linked.get(this.#rootOf(parentRunId));
    if (entry === undefined || entry.taken) return [];
    entry.taken = true;
    return [...entry.parents];
  }

  /** Nests an event under the closest enclosing span this handler opened. */
  #contextFor(parentRunId: string | undefined): Context | undefined {
    for (let run = parentRunId, i = 0; run !== undefined && i < 1000; run = parentRuns.get(run), i++) {
      const span = this.#spans.get(run);
      if (span !== undefined) return trace.setSpan(ROOT_CONTEXT, span);
    }
    return undefined;
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

type MessageLike = BaseMessage & { type?: string; _getType?: () => string; name?: string; tool_calls?: unknown[]; tool_call_id?: string };

function messageType(m: unknown): string | undefined {
  const message = m as MessageLike;
  try {
    return message?.type ?? message?._getType?.();
  } catch {
    return undefined;
  }
}

function isMessage(value: unknown): value is MessageLike {
  return value !== null && typeof value === "object" && "content" in value && messageType(value) !== undefined;
}

/** The model's name, as LangChain reports it. */
function modelName(serialized: unknown, extraParams: Record<string, unknown> | undefined, metadata: Record<string, unknown>): string {
  const invocation = extraParams?.invocation_params as Record<string, unknown> | undefined;
  const candidates = [metadata.ls_model_name, invocation?.model, invocation?.model_name, (serialized as { name?: unknown })?.name];
  const id = (serialized as { id?: unknown })?.id;
  if (Array.isArray(id)) candidates.push(id[id.length - 1]);
  return (candidates.find((c) => typeof c === "string" && c) as string | undefined) ?? "";
}

/** The plain text in a message's content, a tool's output or a handoff. */
export function text(content: unknown): string {
  if (content === null || content === undefined) return "";
  if (typeof content === "string") return content;
  if (isMessage(content)) return text(content.content);
  if (Array.isArray(content)) {
    return content
      .map((c) => (typeof c === "string" ? c : c !== null && typeof c === "object" && typeof c.text === "string" ? c.text : ""))
      .filter((p) => p)
      .join("\n");
  }
  return typeof content === "object" ? events.dumps(content, { ensureAscii: false }) : String(content);
}

/** A tool call's input as LangChain passes it to a callback, JSON as the arguments it holds. */
function parsed(input: unknown): unknown {
  if (typeof input !== "string") return input;
  try {
    return JSON.parse(input);
  } catch {
    return input;
  }
}

/** The string arguments of a tool call long enough to be matched against model output. */
function argumentTexts(inputs: unknown): string[] {
  let values: unknown[] = [inputs];
  if (typeof inputs === "string") {
    try {
      const parsed: unknown = JSON.parse(inputs);
      if (parsed !== null && typeof parsed === "object") values = Object.values(parsed);
    } catch {
      // Plain text, matched whole.
    }
  } else if (inputs !== null && typeof inputs === "object") {
    values = Object.values(inputs);
  }
  return values.filter((v): v is string => typeof v === "string" && v.trim().length >= MIN_MATCH).map((v) => v.trim());
}

function message(m: MessageLike): Record<string, unknown> {
  const out: Record<string, unknown> = { type: messageType(m), content: m.content };
  if (m.id) out.id = m.id;
  if (m.name) out.name = m.name;
  if (Array.isArray(m.tool_calls) && m.tool_calls.length > 0) out.tool_calls = m.tool_calls;
  if (messageType(m) === "tool") out.tool_call_id = m.tool_call_id;
  return out;
}

function toolOutput(output: unknown): unknown {
  return isMessage(output) ? message(output) : output;
}
