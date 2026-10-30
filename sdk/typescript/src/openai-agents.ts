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
 * the response that handed off, and it feeds that agent's next step. Each run, by its first
 * agent span, is linked within itself, as a workflow of its own, so two copies of one agent
 * running at once never take each other's steps as parents. A function tool runs with its
 * event current, and an agent run inside it, such as an agent used as a tool, descends from
 * that tool call, and what it said last feeds the response that reads the tool's result. It's
 * an instance of its agent of its own, <agent>#<its first agent span>, since a tool call can
 * run the same agent more than once, and a response can call it as a tool more than once at a
 * time (docs/events.md). Where nothing else links events, content does, as for LangChain,
 * within each run.
 *
 * Inputs and outputs are only in the trace when the SDK's traceIncludeSensitiveData is on, as
 * it is by default. Without them, events are recorded with empty payloads, and the SDK's loop
 * links them instead: a tool call comes from its agent's latest response, and a response
 * reads that response and the tool results since.
 *
 * Given the brake's rules (install(recorder, { quarantine })), guard() also stops an agent, or
 * one instance of it, in its own process. A call a halt covers, to a function tool, an agent
 * used as a tool or an MCP server's tool, is refused before it runs, by a tool input guardrail.
 * One to a tool the agent runs itself, a shell, an editor or a computer, is refused as it
 * starts. Either way it's recorded as denied, and a refused agent tool starts no run. A handoff
 * is offered only while neither the agent offering it nor the one it hands off to is halted,
 * and one the model picked before a halt landed is refused as it starts. Hosted tools run at
 * OpenAI, and can't be refused here. It's a cooperative stop: it holds only while the agent's
 * code does.
 *
 * At the same points, guard() applies the flow policy the rules carry (flow.ts), once no halt
 * covers the call: a tool call, or a handoff by the agent it reaches, is refused the same way,
 * and a handoff isn't offered, while the policy keeps it from what entered the agent making
 * it. What entered each agent is noted as its spans end, by its name: a tool's result as
 * tool:<name>, an MCP server's tool by the name the SDK gives it, which is its MCP name unless
 * the agent's mcpConfig includes server names in tool names; a handoff in its target as
 * agent:<source>, with what had entered the source; and a run inside a tool call, such as an
 * agent used as a tool, starts with what had entered the agent calling it, and gives back what
 * entered it with its answer. So they're noted only while the SDK traces, as Wake records.
 * The SDK doesn't say which agent an agent tool runs, so a rule for that agent as a receiver
 * isn't checked when it starts, only its own calls are; hosted tools' results aren't noted.
 */

import { randomUUID } from "node:crypto";

import {
  addTraceProcessor,
  computerTool,
  defineToolInputGuardrail,
  getCurrentSpan,
  handoff,
  Handoff,
  ToolGuardrailFunctionOutputFactory,
  type Agent,
  type ComputerTool,
  type HandoffEnabledFunction,
  type Tool,
  type ToolGuardrailFunctionOutput,
  type ToolInputGuardrailData,
  type ToolInputGuardrailDefinition,
} from "@openai/agents";
import { ROOT_CONTEXT, SpanStatusCode, trace, type Context, type Span as OtelSpan } from "@opentelemetry/api";

import * as events from "./events.ts";
import { entered } from "./flow.ts";
import { contextOf, EventGraph, failed, Recent, unique } from "./graph.ts";
import * as links from "./links.ts";
import { log } from "./log.ts";
import type { Recorder } from "./recorder.ts";
import { AgentHalted, flowRefusal, keptOf, refusal, stopsWith, unchecked, type Halts, type Refusal } from "./stop.ts";

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

/** Marks what guard() made refuse, so guarding again changes nothing. */
const WAKE = Symbol("wake");

/** What the SDK asks of a computer while it carries out a computer call. */
const COMPUTER_ACTIONS = ["click", "doubleClick", "drag", "keypress", "move", "screenshot", "scroll", "type", "wait"];

/**
 * Adds Wake as a trace processor for every OpenAI Agents SDK run in this process. With
 * quarantine, the brake's rules for the agent (Quarantine), guard() stops what a halt covers.
 */
export function install(recorder: Recorder, options: { quarantine?: Halts } = {}): WakeTracingProcessor {
  const processor = new WakeTracingProcessor(recorder, options);
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
  // What a shell command printed.
  const printed = [field(value, "stdout"), field(value, "stderr")].filter((t): t is string => typeof t === "string" && t !== "");
  if (printed.length > 0) return printed.join("\n");
  // A Chat Completions response: the messages in its choices.
  const choices = field(value, "choices");
  if (Array.isArray(choices)) return text(choices.map((c) => field(c, "message")));
  return "";
}

/** Whether an input item is a tool's result, as function_call_result or shell_call_output. */
function isResult(type: unknown): boolean {
  return typeof type === "string" && (type.endsWith("_output") || type.endsWith("_result"));
}

/** The assistant messages a model call produced, whatever shape the SDK reports them in. */
function outputItems(output: unknown): unknown[] {
  const items = Array.isArray(output) ? output : [];
  return items.flatMap((item) => {
    const choices = field(item, "choices");
    return Array.isArray(choices) ? choices.map((c) => field(c, "message")).filter((m) => m) : [item];
  });
}

/** The span the SDK has current here, if any. */
function currentSpan(): SdkSpan | undefined {
  try {
    return (getCurrentSpan() as SdkSpan | null) ?? undefined;
  } catch {
    return undefined;
  }
}

function wrapped(value: unknown): boolean {
  return (typeof value === "object" || typeof value === "function") && value !== null && (value as { [WAKE]?: true })[WAKE] === true;
}

type Key = string;

function key(...parts: unknown[]): Key {
  return JSON.stringify(parts);
}

interface Pending {
  eventId: string;
  frame?: links.Frame;
}

/** A tool call or handoff refused as it started: the refusal, the call's id when the SDK gives it, and the agent a handoff would have started. */
interface Stopped {
  refusal: Refusal;
  call?: string;
  receiver?: string;
}

/** An OpenAI Agents SDK TracingProcessor that records Wake events. */
export class WakeTracingProcessor extends EventGraph {
  readonly #agentSpan = new Recent<string, string>(); // sdk span id of an agent span -> agent name
  // A run is told apart from other runs of its agent, and from other copies of it, by its first
  // agent span: the agent spans of one run, after handoffs too, share a parent, the task span
  // the SDK opens for each run. Its steps are linked, and their content matched, within the run.
  readonly #runs = new Recent<string, string>(); // sdk span id of an agent span -> its run, the run's first agent span id
  readonly #firsts = new Recent<Key, string>(); // (sdk trace id, parent span id) -> the first agent span under it
  readonly #runsIn = new Recent<string, string[]>(); // sdk span id of a tool call -> the runs inside it
  readonly #runLast = new Recent<string, string>(); // run -> its latest response event id
  readonly #begun = new Recent<Key, true>(); // (trace, run) whose first response is recorded
  readonly #pending = new Recent<string, Pending>(); // sdk span id -> what onSpanStart set up
  readonly #call = new Recent<Key, [string | undefined, string]>(); // (trace, run, tool name, arguments) -> (call id, response event id)
  readonly #eventByCall = new Recent<string, string>(); // call id -> tool call event id
  readonly #returnedByEvent = new Recent<string, string[]>(); // tool call event id -> events its output came from
  readonly #eventByItem = new Recent<string, string>(); // response or output item id -> response event id
  readonly #lastResponse = new Recent<Key, string>(); // (trace, run, agent) -> response event id
  readonly #handedOff = new Recent<Key, string>(); // (trace, run, agent) -> handoff event id, feeding its next step
  readonly #seenUser = new Recent<Key, true>(); // (trace, run, digest) for user input already seen
  readonly #roots = new Recent<string, OtelSpan>(1024); // sdk trace id -> the workflow's root span
  readonly #linked = new Recent<string, string[]>(1024); // sdk trace id -> what started it, feeding each of its runs' first response
  readonly #spans = new Recent<string, OtelSpan>(); // sdk span id -> its open span
  readonly #toolAgents = new Recent<string, string>(); // tool call event id -> the agent making it
  readonly #toolInstances = new Recent<string, [string, string[]]>(); // tool call event id -> the instance making it, and its lineage
  readonly #toolRuns = new Recent<string, string | undefined>(); // tool call event id -> the run making it
  readonly #toolsSince = new Recent<Key, string[]>(); // (trace, run, agent) -> tool calls since its last response, when inputs are hidden
  readonly #quarantine: Halts | undefined;
  readonly #halted = new Recent<string, Stopped>(); // sdk span id of a tool call or handoff -> what refused it as it started
  readonly #refused = new Recent<string, true>(); // sdk span id of a tool call or handoff refused, which nothing of entered
  readonly #instances = new Recent<string, [string, string[]]>(); // instance -> its agent and lineage, for each Wake has seen start
  #guardrail: ToolInputGuardrailDefinition | undefined; // the tool input guardrail guard() adds, made once

  constructor(recorder: Recorder, { quarantine }: { quarantine?: Halts } = {}) {
    super(recorder);
    this.#quarantine = quarantine;
  }

  // Cooperative stops.

  /**
   * Has the brake's halts stop these agents, and every agent they hand off to, in this process,
   * and returns the first. Each function tool, agent used as a tool and MCP server gets a tool
   * input guardrail that refuses a call a halt covers before it runs. Each tool the agent runs
   * in its own process refuses such a call as it starts: a shell or an editor answers with the
   * refusal, and a computer, whose output is a screenshot that can't carry it, throws
   * AgentHalted, which the SDK answers the model with an empty screenshot for, as for any
   * computer action that fails. Each handoff is offered only while neither the agent offering
   * it nor the one it hands off to is halted, and if a halt lands after the model picked it, it
   * throws AgentHalted as it starts. Bare agents in handoffs become the handoff the SDK would
   * make of them. The SDK doesn't say which agent an agent tool runs, so pass the agents used
   * as tools too. A tool the agent would run itself that Wake can't make refuse throws a
   * TypeError, rather than run unstopped. Each refuses what the flow policy the rules carry
   * keeps too, and a handoff it would refuse isn't offered.
   */
  guard<A extends Agent<any, any>>(agent: A, ...others: Agent<any, any>[]): A {
    if (this.#quarantine === undefined) throw new Error("guard() needs the brake's rules: install(recorder, { quarantine })");
    const guardrail = (this.#guardrail ??= defineToolInputGuardrail({ name: "wake_halts", run: async (data) => this.#checkToolCall(data) }));
    stopsWith(this.#quarantine);
    const seen = new Set<Agent<any, any>>();
    const todo: Agent<any, any>[] = [agent, ...others];
    for (let current = todo.pop(); current !== undefined; current = todo.pop()) {
      if (seen.has(current)) continue;
      seen.add(current);
      const owner = current.name;
      current.tools = current.tools.map((tool: Tool<any>) => {
        if (tool.type === "function") {
          if (!(tool.inputGuardrails ?? []).includes(guardrail)) tool.inputGuardrails = [...(tool.inputGuardrails ?? []), guardrail];
          return tool;
        }
        // Hosted tools run at OpenAI, a shell too when it has no local shell.
        if (tool.type === "hosted_tool" || (tool.type === "shell" && !("shell" in tool && tool.shell))) return tool;
        return this.#guardLocal(tool, owner);
      });
      for (const server of current.mcpServers as { toolInputGuardrails?: ToolInputGuardrailDefinition<any>[] }[]) {
        if (!(server.toolInputGuardrails ?? []).includes(guardrail)) server.toolInputGuardrails = [...(server.toolInputGuardrails ?? []), guardrail];
      }
      current.handoffs = current.handoffs.map((item) => {
        const guarded = this.#guardHandoff(item instanceof Handoff ? item : handoff(item), owner);
        todo.push(guarded.agent);
        return guarded;
      });
    }
    return agent;
  }

  /**
   * A tool the agent runs in its own process, made to refuse a call a halt covers as it starts.
   * owner is the agent guard() found it on, which the call is checked as when no span says
   * who's calling.
   */
  #guardLocal(tool: Tool<any>, owner: string): Tool<any> {
    switch (tool.type) {
      case "shell":
        if ("shell" in tool && tool.shell) {
          tool.shell = this.#refusing(tool.shell, ["run"], owner, tool.name, (action) => [action, undefined], (refused) => ({
            // As the SDK answers a shell call its approval rejects.
            output: [{ stdout: "", stderr: refused[1], outcome: { type: "exit", exitCode: null } }],
          }));
        }
        return tool;
      case "apply_patch":
        tool.editor = this.#refusing(tool.editor, ["createFile", "updateFile", "deleteFile"], owner, tool.name, (operation) => [operation, undefined], (refused) => ({
          status: "failed",
          output: refused[1],
        }));
        return tool;
      case "computer":
        return this.#guardComputer(tool, owner);
      default:
        throw new TypeError(`guard() can't refuse calls to a ${String((tool as { type?: unknown }).type)} tool, which would run in the agent's process, so it can't stop this agent`);
    }
  }

  /**
   * A computer tool whose computer refuses each action a halt covers, throwing AgentHalted. One
   * made for each run, by a function or a provider, is made refusing, in a new tool, since the
   * SDK keeps the one it was given.
   */
  #guardComputer(tool: ComputerTool<any, any>, owner: string): Tool<any> {
    const computer = tool.computer as unknown;
    if (wrapped(computer)) return tool;
    const refusing = <T extends object>(made: T): T =>
      this.#refusing(made, COMPUTER_ACTIONS, owner, tool.name, () => [undefined, undefined], (refused) => {
        throw new AgentHalted(refused);
      });
    const provider = typeof computer === "object" && computer !== null && typeof (computer as { create?: unknown }).create === "function" && typeof (computer as { click?: unknown }).click !== "function";
    if (typeof computer !== "function" && !provider) {
      if (computer === null || typeof computer !== "object") throw new TypeError("guard() can't refuse this computer tool's actions: it has no computer it can wrap");
      tool.computer = refusing(computer) as typeof tool.computer;
      return tool;
    }
    const create = (typeof computer === "function" ? computer : (computer as { create: (...args: unknown[]) => unknown }).create) as (...args: unknown[]) => unknown;
    const made = Object.assign(async (...args: unknown[]) => refusing((await create(...args)) as object), { [WAKE]: true as const });
    const config = typeof computer === "function" ? made : { create: made, dispose: (computer as { dispose?: unknown }).dispose, [WAKE]: true as const };
    return computerTool({
      name: tool.name,
      computer: config as never,
      needsApproval: tool.needsApproval,
      onSafetyCheck: tool.onSafetyCheck,
      customDataExtractor: tool.customDataExtractor,
    });
  }

  /**
   * target, its methods refusing a call a halt covers as it starts: answer(refusal) is what they
   * give instead. describe(...args) gives the call's arguments and id, for its record.
   */
  #refusing<T extends object>(
    target: T,
    methods: string[],
    owner: string,
    name: string,
    describe: (...args: unknown[]) => [unknown, string | undefined],
    answer: (refused: Refusal) => unknown,
  ): T {
    if (wrapped(target)) return target;
    return new Proxy(target, {
      get: (object, property) => {
        if (property === WAKE) return true;
        const value = Reflect.get(object, property) as unknown;
        if (typeof property !== "string" || !methods.includes(property) || typeof value !== "function") return value;
        return (...args: unknown[]) => {
          let described: [unknown, string | undefined] = [undefined, undefined];
          try {
            described = describe(...args);
          } catch {
            // Refused or not all the same; only its record may say less.
          }
          const refused = this.#refuseCall(undefined, name, described[0], described[1], owner);
          return refused === undefined ? (value as (...a: unknown[]) => unknown).apply(object, args) : answer(refused);
        };
      },
    });
  }

  /**
   * The handoff, offered only while neither the agent offering it nor the one it hands off to is
   * halted, nor the flow policy keeps it, and refused as it starts, throwing AgentHalted, if that
   * has changed since.
   */
  #guardHandoff(item: Handoff<any, any>, owner: string): Handoff<any, any> {
    const enabled = item.isEnabled as HandoffEnabledFunction<any> & { wake?: true };
    if (!enabled.wake) {
      item.isEnabled = Object.assign(
        async (args: Parameters<HandoffEnabledFunction<any>>[0]) =>
          this._refusal(args.agent?.name ?? this._recorder.agent, { target: item.agentName }) === undefined && (await enabled(args)),
        { wake: true as const },
      );
    }
    const invoke = item.onInvokeHandoff as Handoff<any, any>["onInvokeHandoff"] & { wake?: true };
    if (!invoke.wake) {
      item.onInvokeHandoff = Object.assign(
        async (...args: Parameters<Handoff<any, any>["onInvokeHandoff"]>) => {
          this.#refuseHandoff(item.agentName, owner);
          return invoke.apply(item, args);
        },
        { wake: true as const },
      );
    }
    return item;
  }

  /** The guardrail: refuses a call a halt covers, telling the model why. */
  async #checkToolCall(data: ToolInputGuardrailData): Promise<ToolGuardrailFunctionOutput> {
    const call = data.toolCall as { name?: string; arguments?: string; callId?: string } | undefined;
    const refused = this.#refuseCall(data.agent?.name ?? this._recorder.agent, call?.name ?? "tool", call?.arguments, call?.callId);
    if (refused === undefined) return ToolGuardrailFunctionOutputFactory.allow();
    return ToolGuardrailFunctionOutputFactory.rejectContent(refused[1], { halt: refused[0] });
  }

  /**
   * The refusal of a tool call about to run, if a halt covers who's making it, or the flow
   * policy keeps it from what entered them: agent, as the SDK says, or else the agent whose run
   * the SDK's current span is in, or owner. It's noted on the call's span, to be recorded as
   * denied when that ends, or recorded as denied now if the SDK opened none for it, as for a
   * guardrail run before the call's approval. Never throws: a check that fails refuses.
   */
  #refuseCall(agent: string | undefined, name: string, args: unknown, callId: string | undefined, owner?: string): Refusal | undefined {
    if (this.#quarantine === undefined) return undefined;
    const span = currentSpan();
    let who = agent ?? owner ?? this._recorder.agent;
    let refused: Refusal | undefined;
    try {
      who = agent ?? this.#agentAt(span?.spanId) ?? who;
      refused = this._refusal(who, { span, tool: name });
    } catch (error) {
      log.warn("couldn't check the halts for a tool call; refusing it", error);
      refused = unchecked();
    }
    if (refused === undefined) return undefined;
    const stopped = refused;
    // Refused all the same if this fails; only its record may say less.
    this.#safely("refused tool call", () => {
      if (span === undefined) return;
      this.#refused.set(span.spanId, true);
      if (this.#pending.has(span.spanId)) this.#halted.set(span.spanId, { refusal: stopped, call: callId });
      else this.#refusedAlone(span, who, name, args, callId, stopped);
    });
    log.warn(`refused a ${name} call of ${who}: ${refused[0]}`);
    return refused;
  }

  /**
   * Throws AgentHalted, noting the refusal on the handoff's span to be recorded as denied, if a
   * halt covers the agent handing off or the one it would hand off to, or the flow policy keeps
   * the handoff from what entered the agent handing off.
   */
  #refuseHandoff(target: string, owner: string): void {
    const span = currentSpan();
    const handing = span?.spanData.type === "handoff" ? span : undefined;
    const source = (handing?.spanData.from_agent as string | undefined) ?? owner;
    const refused = this._refusal(source, { span, target });
    if (refused === undefined) return;
    this.#safely("refused handoff", () => {
      if (handing !== undefined) this.#refused.set(handing.spanId, true);
      if (handing !== undefined && this.#spans.has(handing.spanId)) this.#halted.set(handing.spanId, { refusal: refused, receiver: target });
    });
    log.warn(`refused a handoff of ${source} to ${target}: ${refused[0]}`);
    throw new AgentHalted(refused);
  }

  /**
   * The refusal of what a run of agent is about to do, if a halt covers it: by its name, the
   * instance the run is and those that started it. With target, a handoff to target is refused
   * too when a halt covers it, or the instance of it the handoff would start in the same run.
   * The run is the one span is in, by default the SDK's current span, as for its events. While
   * Wake can't place it, a halt on any run of the agent Wake has seen refuses it. Then the flow
   * policy, on what entered agent: a call to tool, or a handoff to target, it keeps is refused
   * too, its rules for callers holding the run's lineage as the halts do. Never throws: a check
   * that fails refuses.
   */
  _refusal(agent: string, { span = currentSpan(), target, tool }: { span?: SdkSpan; target?: string; tool?: string } = {}): Refusal | undefined {
    if (this.#quarantine === undefined) return undefined;
    const outcome = target !== undefined ? `the handoff to ${target} wasn't made` : "this tool call wasn't run";
    try {
      const names = new Set([this._recorder.agent, agent]);
      const unplaced: string[] = [];
      let who = agent;
      let run: string | undefined;
      let agentSid = this.#agentAbove(span?.spanId);
      if (agentSid !== undefined && this.#agentSpan.get(agentSid) === agent) {
        const [instance, lineage] = this.#identity(agentSid);
        for (const name of lineage) names.add(name);
        if (instance !== undefined) {
          names.add(instance);
          who = instance;
          run = this.#runs.get(agentSid);
        }
      } else {
        agentSid = undefined;
        for (const [instance, [name, lineage]] of this.#instances.entries()) {
          if (name === agent) unplaced.push(instance, ...lineage);
        }
      }
      let refused = refusal(this.#quarantine, who, names, { unplaced, outcome });
      if (refused === undefined && target !== undefined) {
        // A handoff's target runs where its source does: in a tool call's run, as an instance of
        // that run.
        const targetUnplaced = agentSid === undefined ? [...this.#instances.entries()].filter(([, [name]]) => name === target).map(([i]) => i) : [];
        const instance = run !== undefined ? `${target}#${run}` : undefined;
        refused = refusal(this.#quarantine, instance ?? target, instance !== undefined ? [target, instance] : [target], { unplaced: targetUnplaced, outcome });
      }
      if (refused === undefined && (target !== undefined || tool !== undefined)) {
        const call = target !== undefined ? { receiver: target } : { tool };
        refused = flowRefusal(this.#quarantine, who, { caller: agent, lineage: [...names, ...unplaced], ...call }, outcome);
      }
      return refused;
    } catch (error) {
      log.warn(`couldn't check the halts for ${agent}; refusing`, error);
      return unchecked(outcome);
    }
  }

  /**
   * Records a tool call refused where the SDK opened no span for it, as for a guardrail run
   * before the call's approval: denied, under its agent's run, the response that asked for it
   * its parent.
   */
  #refusedAlone(span: SdkSpan, agent: string, name: string, args: unknown, callId: string | undefined, refused: Refusal): void {
    const run = this.#runAt(span.spanId);
    const requested = typeof args === "string" ? this.#call.get(key(span.traceId, run ?? null, name, args)) : undefined;
    const parent = requested?.[1] ?? this.#lastResponse.get(key(span.traceId, run ?? null, agent));
    const otel = this._recorder.tracer.startSpan(`execute_tool ${name}`, {}, this.#contextAt(span.spanId, span.traceId));
    const eventId = randomUUID();
    const at: SdkSpan = { traceId: span.traceId, spanId: "", parentId: span.spanId, spanData: { type: "function" }, error: null };
    this.#event(otel, at, eventId, events.DENIED, `execute_tool ${name}`, agent, parent !== undefined ? [parent] : [], [], {
      "gen_ai.operation.name": "execute_tool",
      "gen_ai.tool.name": name,
      "wake.brake.halt_id": refused[0],
    }, { input: args ?? null, output: refused[1], ...keptOf(refused) });
    if (callId) this.#eventByCall.set(callId, eventId);
    this.#remember(otel.spanContext().traceId, run, eventId, refused[1]);
    otel.end();
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
        // The first agent span under a parent starts a run; the rest are its next agents, after
        // a handoff.
        let run = this.#firsts.get(key(span.traceId, span.parentId));
        if (run === undefined) {
          run = span.spanId;
          this.#firsts.set(key(span.traceId, span.parentId), run);
          const tool = this.#toolAbove(span.parentId);
          if (tool !== undefined) {
            this.#runsIn.set(tool[0], [...(this.#runsIn.get(tool[0]) ?? []), run]);
            // A run inside a tool call, such as an agent used as a tool, starts with what had
            // entered the agent calling it.
            entered.message(this.#agentAt(tool[0]) ?? this._recorder.agent, String(data.name));
          }
        }
        this.#runs.set(span.spanId, run);
        const [instance, lineage] = this.#identity(span.spanId);
        if (instance !== undefined) this.#instances.set(instance, [String(data.name), lineage]);
        attributes = {
          "gen_ai.operation.name": "invoke_agent",
          "gen_ai.agent.name": String(data.name),
          [events.AGENT_ID]: String(data.name),
          ...events.instanceAttributes(instance, lineage),
        };
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
      const run = this.#runOf(span);
      const [instance, lineage] = this.#identity(this.#agentAbove(span.parentId));
      this.#pending.set(span.spanId, { eventId, frame });
      this.#toolAgents.set(eventId, agent);
      this.#toolRuns.set(eventId, run);
      if (instance !== undefined) this.#toolInstances.set(eventId, [instance, lineage]);
      let requestedBy = [...this.#call.entries()]
        .filter(([k]) => {
          const [t, r, n] = JSON.parse(k) as [string, string | null, string];
          return t === span.traceId && r === (run ?? null) && n === name;
        })
        .map(([, [, response]]) => response);
      const last = this.#lastResponse.get(key(span.traceId, run ?? null, agent));
      // Without sensitive data no call names it: the agent's latest response did.
      if (requestedBy.length === 0 && last !== undefined) requestedBy = [last];
      this._rememberLineage(eventId, unique(requestedBy), [`tool:${name}`]);
    });
  }

  async onSpanEnd(span: SdkSpan): Promise<void> {
    this.#safely("what entered", () => this.#entering(span));
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

  /**
   * Notes what a span that ended brought into an agent (flow.ts): a tool's result enters the
   * agent that called it, unless Wake refused the call; a handoff that was made enters its
   * target from its source; and the answer of a run inside a tool call goes back to the agent
   * that called the tool, with what had entered the run.
   */
  #entering(span: SdkSpan): void {
    const data = span.spanData;
    if (data.type === "function") {
      if (!this.#refused.has(span.spanId)) entered.note(this.#agentFor(span), `tool:${String(data.name ?? "")}`);
    } else if (data.type === "handoff") {
      // The SDK names the target once the handoff is made, and never for one refused.
      if (typeof data.to_agent === "string" && data.to_agent && !this.#refused.has(span.spanId)) entered.message(String(data.from_agent ?? "unknown"), data.to_agent);
    } else if (data.type === "agent") {
      const tool = this.#toolAbove(span.parentId);
      if (tool !== undefined) entered.message(String(data.name), this.#agentAt(tool[0]) ?? this._recorder.agent);
    }
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
    const run = this.#runOf(span);
    const scope = run ?? null;
    // The SDK leaves the input out without sensitive data.
    const hidden = call.input === undefined || call.input === null;
    const items: unknown[] = Array.isArray(call.input) ? call.input : call.input ? [{ role: "user", content: call.input }] : [];
    let parents: string[] = [];
    const origins: string[] = [];
    const first = !this.#begun.has(key(traceKey, scope));
    this.#begun.set(key(traceKey, scope), true);
    // A run inside a recorded tool call, such as an agent used as a tool, shares the outer
    // trace, so no trace starts for it. It descends from the tool call: its first response
    // has the call as a parent, and its input isn't the user's. A run of the workflow's own
    // descends from what started the workflow, if anything did.
    const tool = this.#enclosingTool(span);
    const linked = first && tool === undefined ? this.#linked.get(traceKey) : undefined;
    if (linked !== undefined) parents.push(...linked);
    if (first && tool !== undefined) parents.push(tool[1]);
    const handoff = this.#handedOff.pop(key(traceKey, scope, agent));
    if (handoff !== undefined) parents.push(handoff);
    if (hidden) {
      // The SDK's loop says what this response read: the agent's last response and the tool
      // results since, and what they came from. Its first reads the workflow's input.
      const last = this.#lastResponse.get(key(traceKey, scope, agent));
      if (last !== undefined) parents.push(last);
      for (const toolEvent of this.#toolsSince.pop(key(traceKey, scope, agent)) ?? []) parents.push(toolEvent, ...(this.#returnedByEvent.get(toolEvent) ?? []));
      if (!(linked || tool || handoff || last)) origins.push(events.ORIGIN_USER);
    }
    for (const item of items) {
      const type = field(item, "type");
      const role = field(item, "role");
      const callId = field(item, "callId", "call_id") ?? (role === "tool" ? field(item, "tool_call_id") : undefined);
      const itemId = field(item, "id");
      if ((isResult(type) || role === "tool") && typeof callId === "string" && this.#eventByCall.has(callId)) {
        const toolEvent = this.#eventByCall.get(callId)!;
        parents.push(toolEvent, ...(this.#returnedByEvent.get(toolEvent) ?? []));
      } else if (typeof itemId === "string" && this.#eventByItem.has(itemId)) {
        parents.push(this.#eventByItem.get(itemId)!);
      } else if (role === "user") {
        // A user's message is new content the first time a model sees it in a run. In a linked
        // workflow, or a run inside a tool call, the input came from what started it.
        const seen = key(traceKey, scope, events.sha256(text(item)));
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
    if (inputText) parents = [...parents, ...this.#matching(traceId, run, inputText)];
    const eventId = randomUUID();
    this.#event(otel, span, eventId, events.MODEL_CALL, `chat ${call.model}`.trim(), agent, parents, origins, {
      "gen_ai.operation.name": "chat",
      "gen_ai.request.model": call.model,
    }, { input: items, output: call.output });
    this.#lastResponse.set(key(traceKey, scope, agent), eventId);
    if (run !== undefined) this.#runLast.set(run, eventId);
    if (call.responseId) this.#eventByItem.set(call.responseId, eventId);
    for (const item of call.output) {
      const itemId = field(item, "id");
      if (typeof itemId === "string") this.#eventByItem.set(itemId, eventId);
      if (field(item, "type") === "function_call") {
        this.#call.set(key(traceKey, scope, field(item, "name"), field(item, "arguments")), [field(item, "call_id", "callId") as string | undefined, eventId]);
      }
      // Chat Completions models report an assistant message with its tool calls.
      for (const requested of (field(item, "tool_calls") as unknown[] | undefined) ?? []) {
        const fn = field(requested, "function");
        this.#call.set(key(traceKey, scope, field(fn, "name"), field(fn, "arguments")), [field(requested, "id") as string | undefined, eventId]);
      }
    }
    this.#remember(traceId, run, eventId, text(call.output));
    otel.end();
  }

  #functionEnded(span: SdkSpan): void {
    const data = span.spanData;
    const name = String(data.name ?? "");
    const pending = this.#pending.pop(span.spanId);
    // What the runs inside the tool call, such as an agent used as a tool, said last is what
    // the tool returns.
    const ran = (this.#runsIn.pop(span.spanId) ?? []).map((r) => this.#runLast.get(r)).filter((e): e is string => e !== undefined);
    const returned = [...links._returned(pending?.frame), ...ran];
    links._exit(pending?.frame);
    if (runningTools.get(span.spanId) === pending?.frame) runningTools.delete(span.spanId);
    const eventId = pending?.eventId ?? randomUUID();
    // What the tool got back came from these events, such as an A2A reply: they feed the
    // response that reads its output.
    if (returned.length > 0) this.#returnedByEvent.set(eventId, returned);
    const agent = this.#agentFor(span);
    const run = this.#runOf(span);
    const scope = run ?? null;
    const otel = this.#take(span);
    if (otel === undefined) return;
    const traceId = otel.spanContext().traceId;
    const parents: string[] = [];
    const input = data.input;
    const call = this.#call.pop(key(span.traceId, scope, name, input));
    const handoff = this.#handedOff.pop(key(span.traceId, scope, agent));
    const stopped = this.#halted.pop(span.spanId);
    if (call !== undefined) {
      const [callId, requestedBy] = call;
      parents.push(requestedBy);
      if (callId) this.#eventByCall.set(callId, eventId);
    } else if (typeof input === "string" && input) {
      // No exact match: the arguments themselves point at the response that chose them.
      parents.push(...this.#containing(traceId, run, [input]));
    } else if (input === undefined || input === null || input === "") {
      // Without sensitive data the SDK leaves the arguments out, but a tool only runs from its
      // agent's latest response, and its result feeds the agent's next one.
      const since = this.#toolsSince.get(key(span.traceId, scope, agent)) ?? [];
      this.#toolsSince.set(key(span.traceId, scope, agent), [...since, eventId]);
    }
    if (parents.length === 0) {
      // A call no response names, as a shell's: it ran from its agent's latest response.
      const last = this.#lastResponse.get(key(span.traceId, scope, agent));
      if (last !== undefined) parents.push(last);
    }
    if (handoff !== undefined) parents.unshift(handoff);
    const attributes: Record<string, string> = { "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name };
    const server = mcpServer(data.mcp_data);
    if (server) attributes["wake.mcp.server"] = server;
    let kind = events.TOOL_CALL;
    let origins = [`tool:${name}`];
    let output = data.output ?? null;
    if (stopped !== undefined) {
      // Refused in the agent's own process, under the halt or the flow policy: what the agent
      // tried, and what the policy kept from it. The tool never ran, so nothing of it entered.
      kind = events.DENIED;
      origins = [];
      attributes["wake.brake.halt_id"] = stopped.refusal[0];
      output ??= stopped.refusal[1];
      if (stopped.call) this.#eventByCall.set(stopped.call, eventId);
    }
    this.#event(otel, span, eventId, kind, `execute_tool ${name}`, agent, parents, origins, attributes, {
      input: input ?? null,
      output,
      ...keptOf(stopped?.refusal),
    });
    // A refused call's output, as the model reads it, is the refusal.
    this.#remember(traceId, run, eventId, stopped !== undefined ? stopped.refusal[1] : text(output));
    otel.end();
  }

  #handoffEnded(span: SdkSpan): void {
    const data = span.spanData;
    const source = String(data.from_agent ?? "unknown");
    const scope = this.#runOf(span) ?? null;
    const requestedBy = this.#lastResponse.get(key(span.traceId, scope, source));
    const stopped = this.#halted.pop(span.spanId);
    const otel = this.#take(span);
    if (otel === undefined) return;
    const eventId = randomUUID();
    if (stopped !== undefined) {
      // Refused as it started, under the halt or the flow policy: the attempt to hand the target
      // its task, in the name of the agent that tried. Nothing reached the target.
      const target = stopped.receiver ?? String(data.to_agent ?? "unknown");
      this.#event(otel, span, eventId, events.DENIED, `agent_message ${source} -> ${target}`, source, requestedBy ? [requestedBy] : [], [], {
        "gen_ai.agent.name": target,
        "wake.brake.halt_id": stopped.refusal[0],
        "wake.brake.receiver": target,
      }, { from: source, to: target, output: stopped.refusal[1], ...keptOf(stopped.refusal) });
      otel.end();
      return;
    }
    const target = String(data.to_agent ?? "unknown");
    this.#event(otel, span, eventId, events.AGENT_MESSAGE, `agent_message ${source} -> ${target}`, target, requestedBy ? [requestedBy] : [], [`agent:${source}`], {
      "gen_ai.agent.name": target,
    }, { from: source, to: target });
    this.#handedOff.set(key(span.traceId, scope, target), eventId);
    otel.end();
  }

  // Content matching, within a run: what each output is kept under says which run it was made
  // in, so another run's steps never match it. A memory read or write is matched in the run
  // whose tool call made it.

  override _rememberOutput(traceId: string, eventId: string, text: string): void {
    this.#remember(traceId, this.#runNow(), eventId, text);
  }

  override _matchingOutputs(traceId: string, text: string): string[] {
    return this.#matching(traceId, this.#runNow(), text);
  }

  override _outputsContaining(traceId: string, args: string[]): string[] {
    return this.#containing(traceId, this.#runNow(), args);
  }

  #remember(traceId: string, run: string | undefined, eventId: string, text: string): void {
    super._rememberOutput(within(traceId, run), eventId, text);
  }

  #matching(traceId: string, run: string | undefined, text: string): string[] {
    return super._matchingOutputs(within(traceId, run), text);
  }

  #containing(traceId: string, run: string | undefined, args: string[]): string[] {
    return super._outputsContaining(within(traceId, run), args);
  }

  /** The run whose tool call is running, for a memory read or write it makes. */
  #runNow(): string | undefined {
    const link = links.current();
    return link !== undefined ? this.#toolRuns.get(link.eventId) : undefined;
  }

  // Helpers.

  /** The recorded tool call a span runs inside, as its sdk span id and event id, if any. */
  #enclosingTool(span: SdkSpan): [string, string] | undefined {
    return this.#toolAbove(span.parentId);
  }

  /** The recorded tool call at or above sdk span sid, as its sdk span id and event id. */
  #toolAbove(sid: string | null | undefined): [string, string] | undefined {
    for (let i = 0; sid !== null && sid !== undefined && i < 1000; sid = spanParents.get(sid) ?? null, i++) {
      const pending = this.#pending.get(sid);
      if (pending !== undefined) return [sid, pending.eventId];
    }
    return undefined;
  }

  /** The run a span is a step of, by its first agent span, or undefined outside any agent's run. */
  #runOf(span: SdkSpan): string | undefined {
    return this.#runAt(span.parentId);
  }

  /** The run of the agent span at or above sdk span sid. */
  #runAt(sid: string | null | undefined): string | undefined {
    const agentSid = this.#agentAbove(sid);
    return agentSid !== undefined ? this.#runs.get(agentSid) : undefined;
  }

  /** The agent span at or above sdk span sid. */
  #agentAbove(sid: string | null | undefined): string | undefined {
    for (let i = 0; sid !== null && sid !== undefined && i < 1000; sid = spanParents.get(sid) ?? null, i++) {
      if (this.#agentSpan.has(sid)) return sid;
    }
    return undefined;
  }

  /** The agent whose run sdk span sid is in, if Wake has seen it start. */
  #agentAt(sid: string | null | undefined): string | undefined {
    const agentSid = this.#agentAbove(sid);
    return agentSid !== undefined ? this.#agentSpan.get(agentSid) : undefined;
  }

  /**
   * The run an agent span is a step of, by its first agent span, when it runs inside a recorded
   * tool call, and the lineage that started it: the agent that made the call, after its own
   * lineage. Undefined for a run of the workflow's own.
   */
  #run(agentSid: string | undefined): [string | undefined, string[]] {
    if (agentSid === undefined) return [undefined, []];
    const tool = this.#toolAbove(spanParents.get(agentSid));
    if (tool === undefined) return [undefined, []];
    const outer = this.#agentAbove(spanParents.get(tool[0]));
    const [outerInstance, outerLineage] = this.#identity(outer);
    const outerName = outer !== undefined ? this.#agentSpan.get(outer)! : this._recorder.agent;
    return [this.#runs.get(agentSid) ?? agentSid, [...outerLineage, outerInstance ?? outerName]];
  }

  /** The instance an agent span's run is, <agent>#<the run's first agent span id>, and the lineage that started it. */
  #identity(agentSid: string | undefined): [string | undefined, string[]] {
    const [run, lineage] = this.#run(agentSid);
    if (run === undefined) return [undefined, []];
    return [`${this.#agentSpan.get(agentSid!)}#${run}`, lineage];
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

  /** The instance whose tool call is running, for a memory write it makes. */
  override _currentInstance(): [string | undefined, readonly string[]] {
    const link = links.current();
    return (link !== undefined ? this.#toolInstances.get(link.eventId) : undefined) ?? [undefined, []];
  }

  /** Under the closest enclosing span Wake opened, else the workflow's root. */
  #context(span: SdkSpan): Context | undefined {
    return this.#contextAt(span.parentId, span.traceId);
  }

  /** Under the span Wake opened for sdk span sid or the closest above it, else the workflow's root. */
  #contextAt(sid: string | null | undefined, traceId: string): Context | undefined {
    for (let i = 0; sid !== null && sid !== undefined && i < 1000; sid = spanParents.get(sid) ?? null, i++) {
      const otel = this.#spans.get(sid);
      if (otel !== undefined) return trace.setSpan(ROOT_CONTEXT, otel);
    }
    const root = this.#roots.get(traceId);
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
    // In the run the span is in. A handoff is recorded in its target's name, and the target
    // runs where its source did: the same run, as an instance of its own.
    const [run, lineage] = this.#run(this.#agentAbove(span.parentId));
    otel.updateName(name);
    otel.setAttributes({
      [events.EVENT_ID]: eventId,
      [events.EVENT_KIND]: kind,
      [events.AGENT_ID]: agent,
      [events.CAPTURE]: "sdk",
      [events.PARENTS]: parents,
      [events.ORIGINS]: origins,
      ...events.instanceAttributes(run !== undefined ? `${agent}#${run}` : undefined, lineage),
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

/** A trace id, with the run in it whose content an output belongs to. */
function within(traceId: string, run: string | undefined): string {
  return traceId ? `${traceId}/${run ?? ""}` : "";
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
