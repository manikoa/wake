/**
 * OpenAI Agents SDK runs: a triage agent hands off to a billing agent, which reads an email
 * with a tool and answers. A fake model stands in for OpenAI and reports its responses to
 * tracing the way the SDK's own models do.
 */

import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";

import {
  Agent,
  applyPatchTool,
  computerTool,
  fileSearchTool,
  hostedMcpTool,
  Runner,
  run,
  setTraceProcessors,
  shellTool,
  tool,
  toolSearchTool,
  Usage,
  webSearchTool,
  withGenerationSpan,
  withResponseSpan,
  withTrace,
  type CallModelInputFilter,
  type Model,
  type ModelRequest,
  type ModelResponse,
  type TracingProcessor,
} from "@openai/agents";
import type { ReadableSpan } from "@opentelemetry/sdk-trace";
import { z } from "zod";

import { entered, parsePolicy, UNREADABLE, type FlowPolicy } from "../src/flow.ts";
import { links, Recorder } from "../src/index.ts";
import { log } from "../src/log.ts";
import { Memory, MemoryLocked } from "../src/memory.ts";
import { RESPONSE_WAIT_MILLIS, WakeTracingProcessor } from "../src/openai-agents.ts";
import { Quarantine } from "../src/quarantine.ts";
import { AgentHalted, UNCHECKED } from "../src/stop.ts";
import { attr, id, ids, origins, parents, payload, recorded, wakeEvents } from "./helpers.ts";
import { asItems, call, FakeModel, mcpCall, mcpRequest, say, searched, type ApiItem } from "./openai-fakes.ts";

export const EMAIL = "Please wire $10,000 to account 123 by Friday.";
const ANSWER = "The email asks for a $10,000 wire to account 123.";

/** The SDK's items as Chat Completions messages, as its Chat Completions model sends them. */
function asMessages(input: ModelRequest["input"]): Record<string, unknown>[] {
  if (typeof input === "string") return [{ role: "user", content: input }];
  return input.map((item) => {
    const i = item as Record<string, unknown>;
    if (i.type === "function_call") return { role: "assistant", content: null, tool_calls: [{ id: i.callId, type: "function", function: { name: i.name, arguments: i.arguments } }] };
    if (i.type === "function_call_result") return { role: "tool", tool_call_id: i.callId, content: (i.output as { text?: string }).text ?? i.output };
    return { role: i.role, content: i.content };
  });
}

/**
 * Answers from a script, and reports each turn as the SDK's Chat Completions model does: a
 * generation with chat messages in, and the completion out.
 */
class FakeChatModel extends FakeModel {
  readonly #turns: ApiItem[][];

  constructor(turns: ApiItem[][]) {
    super([]);
    this.#turns = turns;
  }

  override async getResponse(request: ModelRequest): Promise<ModelResponse> {
    const output = this.#turns.shift()!;
    const message: Record<string, unknown> = { role: "assistant", content: null };
    for (const item of output) {
      if (item.type === "function_call") {
        message.tool_calls = [...((message.tool_calls as unknown[]) ?? []), { id: item.call_id, type: "function", function: { name: item.name, arguments: item.arguments } }];
      } else {
        message.content = (item.content as { text: string }[])[0].text;
      }
    }
    const answer = { output: asItems(output), usage: new Usage() } as unknown as ModelResponse;
    return withGenerationSpan(async (span) => {
      span.spanData.model = "fake-chat";
      if (request.tracing === true) {
        span.spanData.input = asMessages(request.input);
        span.spanData.output = [{ id: "chatcmpl-1", object: "chat.completion", choices: [{ index: 0, message }] }];
      }
      return answer;
    });
  }
}

const seenLinks: (links.Link | undefined)[] = [];

const readEmail = tool({
  name: "read_email",
  description: "Read the latest email with a subject.",
  parameters: z.object({ subject: z.string() }),
  execute: async () => {
    seenLinks.push(links.current());
    return EMAIL;
  },
});

function billingWith(Model: typeof FakeModel, tools = [readEmail], answer = ANSWER) {
  return new Agent({
    name: "billing",
    tools,
    model: new Model([[call("read_email", JSON.stringify({ subject: "invoice" }), "call_read")], [say(answer)]]),
  });
}

export function agentsForTest() {
  const billing = billingWith(FakeModel);
  return new Agent({ name: "triage", handoffs: [billing], model: new FakeModel([[call("transfer_to_billing", "{}", "call_handoff")]]) });
}

afterEach(() => setTraceProcessors([]));

async function runWith(agent: Agent, options: { traceIncludeSensitiveData?: boolean } = {}) {
  const { recorder, spans } = recorded("support");
  setTraceProcessors([new WakeTracingProcessor(recorder)]);
  seenLinks.length = 0;
  const result = await new Runner(options).run(agent, "Is the Acme invoice legit?");
  return { result, spans: await spans() };
}

let triaged: ReturnType<typeof runWith> | undefined;
function triage() {
  triaged ??= runWith(agentsForTest());
  return triaged;
}

test("records responses, tool calls and the handoff", async () => {
  const { result, spans } = await triage();
  assert.equal(result.finalOutput, ANSWER);
  assert.deepEqual(
    wakeEvents(spans).map((e) => [attr(e, "wake.agent.id"), attr(e, "wake.event.kind")]),
    [
      ["triage", "model_call"],
      ["billing", "agent_message"], // the handoff
      ["billing", "model_call"],
      ["billing", "tool_call"],
      ["billing", "model_call"],
    ],
  );
});

test("links follow the SDK's ids", async () => {
  const { spans } = await triage();
  const [triageCall, handoff, first, read, last] = wakeEvents(spans);
  // The user's question enters at the first response.
  assert.deepEqual(origins(triageCall), ["user"]);
  // The handoff came from triage's response, enters billing from triage, and feeds billing's next step.
  assert.deepEqual(new Set(parents(handoff)), ids(triageCall));
  assert.deepEqual(origins(handoff), ["agent:triage"]);
  assert.ok(parents(first).includes(id(handoff)));
  // The tool call was requested by billing's first response, and its result is new content.
  assert.deepEqual(new Set(parents(read)), ids(first));
  assert.deepEqual(origins(read), ["tool:read_email"]);
  assert.equal(attr(read, "gen_ai.tool.name"), "read_email");
  // The last response saw the tool's result and the earlier response.
  assert.ok([id(read), id(first)].every((p) => parents(last).includes(p)));
  assert.match(attr(read, "wake.payload") as string, /Please wire \$10,000/);
});

test("one trace, with each agent's events under its run", async () => {
  const { spans } = await triage();
  const events = wakeEvents(spans);
  assert.equal(new Set(events.map((e) => e.spanContext().traceId)).size, 1);
  const runs = new Map(spans.filter((s) => s.attributes["gen_ai.operation.name"] === "invoke_agent").map((s) => [s.attributes["gen_ai.agent.name"], s]));
  assert.deepEqual(new Set(runs.keys()), new Set(["triage", "billing"]));
  const bySpan = new Map<string, ReadableSpan>(spans.map((s) => [s.spanContext().spanId, s]));
  for (const e of events) {
    if (attr(e, "wake.event.kind") === "agent_message") continue;
    // Each event sits under its own agent's run.
    let parent = bySpan.get(e.parentSpanContext!.spanId)!;
    while (parent.attributes["gen_ai.operation.name"] !== "invoke_agent") parent = bySpan.get(parent.parentSpanContext!.spanId)!;
    assert.equal(parent.attributes["gen_ai.agent.name"], attr(e, "wake.agent.id"));
  }
});

test("the tool runs with its event current", async () => {
  const { spans } = await triage();
  const [read] = wakeEvents(spans, "tool_call");
  assert.equal(seenLinks.length, 1);
  assert.equal(seenLinks[0]?.eventId, id(read));
  assert.equal(seenLinks[0]?.spanId, read.spanContext().spanId);
  assert.equal(links.current(), undefined);
});

test("recording failures never break the run", async () => {
  const recorder = new Recorder({ agent: "support", exporter: recorded("x").exporter });
  recorder.tracer.startSpan = () => {
    throw new Error("recorder bug");
  };
  setTraceProcessors([new WakeTracingProcessor(recorder)]);
  const result = await run(agentsForTest(), "Is the Acme invoice legit?");
  assert.equal(result.finalOutput, ANSWER);
});

test("a memory write in a tool is labeled with its origins", async () => {
  const { recorder, spans } = recorded("support");
  const processor = new WakeTracingProcessor(recorder);
  const memory = new Memory(processor);
  const dir = await mkdtemp(join(tmpdir(), "wake-openai-"));
  const remember = tool({
    name: "read_email",
    description: "Read the latest email with a subject.",
    parameters: z.object({ subject: z.string() }),
    execute: async () => {
      await memory.writeFile(join(dir, "notes.md"), EMAIL);
      return EMAIL;
    },
  });
  setTraceProcessors([processor]);
  await run(billingWith(FakeModel, [remember], "Noted."), "Is the Acme invoice legit?");
  const events = wakeEvents(await spans());
  const [toolCall] = events.filter((e) => attr(e, "wake.event.kind") === "tool_call");
  const [write] = events.filter((e) => attr(e, "wake.event.kind") === "memory_write");
  assert.equal(parents(write)[0], id(toolCall));
  assert.deepEqual(new Set(attr(write, "wake.memory.origins") as string[]), new Set(["tool:read_email", "user"]));
  // In the name of the agent whose tool wrote it, not the recorder's.
  assert.equal(attr(write, "wake.agent.id"), "billing");
  assert.equal(JSON.parse(await readFile(join(dir, "notes.md.wake.json"), "utf8")).agent, "billing");
});

test("Chat Completions messages link as responses do", async () => {
  const [first, read, last] = wakeEvents((await runWith(billingWith(FakeChatModel))).spans);
  assert.deepEqual(origins(first), ["user"]);
  // The assistant message's tool call names the response that requested the tool...
  assert.deepEqual(new Set(parents(read)), ids(first));
  // ...and the tool message in the next input names the tool call.
  assert.ok(parents(last).includes(id(read)));
});

test("without sensitive data, the loop links events", async () => {
  const [triageCall, handoff, first, read, last] = wakeEvents((await runWith(agentsForTest(), { traceIncludeSensitiveData: false })).spans);
  // Nothing of the exchange is in the trace, but the run's shape is: the workflow's input
  // enters at the first response, a tool runs from its agent's latest response, and the
  // next response reads that response and the tool's result.
  assert.deepEqual(origins(triageCall), ["user"]);
  assert.deepEqual(new Set(parents(handoff)), ids(triageCall));
  assert.ok(parents(first).includes(id(handoff)));
  assert.deepEqual(new Set(parents(read)), ids(first));
  assert.deepEqual(new Set(parents(last)), ids(first, read));
  assert.ok(!origins(last).includes("user"));
});

test("an agent run inside a tool descends from the tool call", async () => {
  const summarizer = new Agent({ name: "summarizer", model: new FakeModel([[say("A request to wire $10,000.")]]) });
  const summarize = tool({
    name: "summarize",
    description: "Summarize the latest email with a subject.",
    parameters: z.object({ subject: z.string() }),
    execute: async () => (await run(summarizer, `Summarize this email: ${EMAIL}`)).finalOutput as string,
  });
  const support = new Agent({
    name: "support",
    tools: [summarize],
    model: new FakeModel([[call("summarize", JSON.stringify({ subject: "invoice" }), "call_summary")], [say("It asks for a wire.")]]),
  });
  const [first, toolCall, nested, last, ...rest] = wakeEvents((await runWith(support)).spans);
  assert.equal(rest.length, 0);
  assert.ok(last);
  assert.equal(attr(toolCall, "wake.event.kind"), "tool_call");
  assert.deepEqual([attr(nested, "wake.agent.id"), attr(nested, "wake.event.kind")], ["summarizer", "model_call"]);
  // The summarizer's input came from the tool call, not from the user.
  assert.ok(parents(nested).includes(id(toolCall)));
  assert.ok(!origins(nested).includes("user"));
  assert.deepEqual(origins(first), ["user"]);
  // It's an instance, started by support's tool call; support's own events aren't.
  assert.match(attr(nested, "wake.agent.instance") as string, /^summarizer#/);
  assert.deepEqual(attr(nested, "wake.agent.lineage"), ["support"]);
  assert.ok([first, toolCall, last].every((e) => attr(e, "wake.agent.instance") === undefined));
});

/**
 * A researcher that looks its question up, then answers from what it found: by what's in its
 * input, so copies running at once each follow their own run. It yields between steps, so
 * copies interleave as they would on a real model.
 */
class ResearcherModel extends FakeModel {
  constructor() {
    super([]);
  }

  override async getResponse(request: ModelRequest): Promise<ModelResponse> {
    await new Promise((resolve) => setImmediate(resolve));
    const input = Array.isArray(request.input) ? (request.input as Record<string, unknown>[]) : [];
    const found = input.filter((i) => i.type === "function_call_result");
    const question = typeof request.input === "string" ? request.input : String(input.find((i) => i.role === "user")?.content ?? "");
    const output = found.length > 0 ? [say(`Answer: ${JSON.stringify(found.at(-1)!.output)}`)] : [call("lookup", JSON.stringify({ q: question }), `call_${randomUUID().slice(0, 8)}`)];
    const answer = { output: asItems(output), usage: new Usage(), responseId: undefined } as unknown as ModelResponse;
    const result = !request.tracing
      ? answer
      : await withResponseSpanFor(request, output, answer);
    await new Promise((resolve) => setImmediate(resolve));
    return result;
  }
}

async function withResponseSpanFor(request: ModelRequest, output: ApiItem[], answer: ModelResponse): Promise<ModelResponse> {
  return withResponseSpan(async (span) => {
    if (request.tracing === true) {
      const responseId = `resp_${randomUUID().slice(0, 8)}`;
      span.spanData.response_id = responseId;
      span.spanData._input = request.input as never;
      span.spanData._response = { id: responseId, model: "fake-model", output, object: "response" };
    }
    return answer;
  });
}

/** Long enough that content matching would link two copies' steps, as their answers match. */
const LONG_QUESTION = "What is the refund window for damaged items?";

for (const [sensitive, question] of [true, false].flatMap((s) => ["refund window", LONG_QUESTION].map((q) => [s, q] as const))) {
  test(`an agent called twice as a tool is two instances that never cross (sensitive data ${sensitive ? "on" : "off"}, ${question.length > 20 ? "long" : "short"} question)`, async () => {
    const { recorder, spans } = recorded("support");
    const processor = new WakeTracingProcessor(recorder);
    const memory = new Memory(processor);
    const dir = await mkdtemp(join(tmpdir(), "wake-openai-"));
    const lookup = tool({
      name: "lookup",
      description: "Look a question up.",
      parameters: z.object({ q: z.string() }),
      execute: async ({ q }) => {
        await new Promise((resolve) => setImmediate(resolve));
        await memory.writeFile(join(dir, `${randomUUID()}.md`), `Found: ${q}`);
        return `Found: ${q}`;
      },
    });
    const researcher = new Agent({ name: "researcher", tools: [lookup], model: new ResearcherModel() });
    const triage = new Agent({
      name: "triage",
      tools: [researcher.asTool({ toolName: "research", toolDescription: "Research one question." })],
      model: new FakeModel([
        // The same question twice: nothing in the arguments tells the copies apart.
        [call("research", JSON.stringify({ input: question }), "call_a"), call("research", JSON.stringify({ input: question }), "call_b")],
        [say("Both answered.")],
      ]),
    });
    setTraceProcessors([processor]);
    await new Runner({ traceIncludeSensitiveData: sensitive }).run(triage, "Check our refund terms.");
    const events = wakeEvents(await spans());

    const byInstance = new Map<string, ReadableSpan[]>();
    for (const e of events) {
      if (attr(e, "wake.agent.id") === "researcher") {
        assert.deepEqual(attr(e, "wake.agent.lineage"), ["triage"]);
        const instance = attr(e, "wake.agent.instance") as string;
        byInstance.set(instance, [...(byInstance.get(instance) ?? []), e]);
      } else {
        assert.equal(attr(e, "wake.agent.instance"), undefined, String(attr(e, "wake.event.kind")));
      }
    }
    assert.equal(byInstance.size, 2, [...byInstance.keys()].join(", "));
    const triageIds = ids(...events.filter((e) => attr(e, "wake.agent.id") === "triage"));
    for (const [instance, own] of byInstance) {
      assert.deepEqual(own.map((e) => attr(e, "wake.event.kind")).sort(), ["memory_write", "model_call", "model_call", "tool_call"], instance);
      const ownIds = ids(...own);
      for (const e of own) {
        // Each copy links only within itself, and to the triage call that started it.
        const crossed = parents(e).filter((p) => !ownIds.has(p) && !triageIds.has(p));
        assert.deepEqual(crossed, [], `${instance} ${String(attr(e, "wake.event.kind"))} links to the other copy`);
      }
      const [first, last] = own.filter((e) => attr(e, "wake.event.kind") === "model_call");
      const [lookupCall] = own.filter((e) => attr(e, "wake.event.kind") === "tool_call");
      // Its tool ran from its own first response, and its answer read that tool's result.
      assert.deepEqual(new Set(parents(lookupCall)), ids(first));
      assert.ok(parents(last).includes(id(lookupCall)));
    }
  });
}

// Cooperative stops.

type Halt = { id: string; fleet?: boolean; agents?: string[] };

/** Stands in for the brake's rules (Quarantine): the halts in force, and the flow policy served with them, if any. */
class Halts {
  halts: Halt[];
  policy: FlowPolicy | undefined;
  constructor(...halts: Halt[]) {
    this.halts = halts;
  }
  halted(names: Iterable<string>): string | null {
    const named = new Set(names);
    return this.halts.find((h) => h.fleet || (h.agents ?? []).some((a) => named.has(a)))?.id ?? null;
  }
  flow(): FlowPolicy | undefined {
    return this.policy;
  }
}

/** Halts the first copy of the researcher it's asked about. */
class HaltOneResearcher {
  instance: string | undefined;
  halted(names: Iterable<string>): string | null {
    for (const name of [...names].sort()) {
      if (!name.startsWith("researcher#")) continue;
      this.instance ??= name;
      if (name === this.instance) return "halt-researcher";
    }
    return null;
  }
}

/** Rules whose check fails. */
const brokenHalts = {
  halted(): string | null {
    throw new Error("the rules can't be read");
  },
};

function twoResearchers() {
  const lookups: string[] = [];
  const lookup = tool({
    name: "lookup",
    description: "Look a question up.",
    parameters: z.object({ q: z.string() }),
    execute: async ({ q }) => {
      await new Promise((resolve) => setImmediate(resolve));
      lookups.push(q);
      return `Found: ${q}`;
    },
  });
  const researcher = new Agent({ name: "researcher", tools: [lookup], model: new ResearcherModel() });
  const triage = new Agent({
    name: "triage",
    tools: [researcher.asTool({ toolName: "research", toolDescription: "Research one question." })],
    model: new FakeModel([
      [call("research", JSON.stringify({ input: "refund window" }), "call_a"), call("research", JSON.stringify({ input: "refund window" }), "call_b")],
      [say("Both answered.")],
    ]),
  });
  return { triage, researcher, lookups };
}

async function guardedRun(quarantine: { halted(names: Iterable<string>): string | null }, agent: Agent<any, any>, ...others: Agent<any, any>[]) {
  const { recorder, spans } = recorded("support");
  const processor = new WakeTracingProcessor(recorder, { quarantine });
  setTraceProcessors([processor]);
  const result = await new Runner().run(processor.guard(agent, ...others), "Check our refund terms.");
  return { result, events: wakeEvents(await spans()), processor };
}

const kinds = (found: ReadableSpan[]) => found.map((e) => attr(e, "wake.event.kind"));
const haltsOf = (found: ReadableSpan[]) => new Set(found.filter((e) => attr(e, "wake.event.kind") === "denied").map((e) => attr(e, "wake.brake.halt_id")));

test("a halted instance is stopped in process, and its sibling goes on", async () => {
  const halts = new HaltOneResearcher();
  const { triage, researcher, lookups } = twoResearchers();
  const { events } = await guardedRun(halts, triage, researcher);
  const calls = events.filter((e) => attr(e, "gen_ai.tool.name") === "lookup");
  assert.deepEqual(kinds(calls).sort(), ["denied", "tool_call"], "only the halted copy's call was refused");
  assert.equal(lookups.length, 1, "only the sibling's lookup ran");
  const denied = calls.find((e) => attr(e, "wake.event.kind") === "denied")!;
  assert.equal(attr(denied, "wake.brake.halt_id"), "halt-researcher");
  assert.equal(attr(denied, "wake.agent.instance"), halts.instance, "recorded as the halted copy's attempt");
  assert.deepEqual(attr(denied, "wake.agent.lineage"), ["triage"]);
  assert.deepEqual(origins(denied), [], "the tool never ran: nothing of it entered");
  const ran = calls.find((e) => attr(e, "wake.event.kind") === "tool_call")!;
  assert.notEqual(attr(ran, "wake.agent.instance"), halts.instance);
  // The refused call has the response that asked for it as its parent, like any call.
  const responses = ids(...events.filter((e) => attr(e, "wake.event.kind") === "model_call" && attr(e, "wake.agent.instance") === halts.instance));
  assert.ok(parents(denied).length > 0 && parents(denied).every((p) => responses.has(p)));
});

test("a halted agent's spawn starts nothing", async () => {
  const { triage, researcher, lookups } = twoResearchers();
  const { result, events } = await guardedRun(new Halts({ id: "halt-triage", agents: ["triage"] }), triage, researcher);
  assert.deepEqual(
    events.map((e) => [attr(e, "wake.agent.id"), attr(e, "wake.event.kind"), attr(e, "gen_ai.tool.name")]),
    [
      ["triage", "model_call", undefined],
      ["triage", "denied", "research"],
      ["triage", "denied", "research"],
      ["triage", "model_call", undefined],
    ],
    "no copy of the researcher started",
  );
  assert.equal(lookups.length, 0);
  assert.deepEqual(haltsOf(events), new Set(["halt-triage"]));
  assert.equal(result.finalOutput, "Both answered.");
});

test("stale rules refuse every call", async () => {
  const { triage, researcher } = twoResearchers();
  const neverFetched = new Quarantine("http://127.0.0.1:9", { agent: "support", start: false });
  const { events } = await guardedRun(neverFetched, triage, researcher);
  assert.deepEqual(kinds(events), ["model_call", "denied", "denied", "model_call"]);
  assert.deepEqual(haltsOf(events), new Set(["stale-rules"]));
});

test("a halt check that fails refuses the call", async () => {
  const { triage, researcher } = twoResearchers();
  const { events } = await guardedRun(brokenHalts, triage, researcher);
  assert.deepEqual(kinds(events), ["model_call", "denied", "denied", "model_call"]);
  assert.deepEqual(haltsOf(events), new Set([UNCHECKED]));
});

test("while Wake can't place a run, a halt on any copy of its agent stops it", async () => {
  const halts = new HaltOneResearcher();
  const { triage, researcher } = twoResearchers();
  const { processor } = await guardedRun(halts, triage, researcher);
  // Outside any run: Wake can't tell which copy of the researcher this would be.
  const refused = processor._refusal("researcher");
  assert.equal(refused?.[0], "halt-researcher");
  assert.ok(refused?.[1].includes("can't yet tell which copy of researcher is calling"), refused?.[1]);
  assert.equal(processor._refusal("triage"), undefined);
});

/** Answers at once, noting the handoffs it was offered. */
class OfferedModel extends FakeModel {
  offered: string[] | undefined;
  constructor() {
    super([[say("Done.")]]);
  }
  override async getResponse(request: ModelRequest): Promise<ModelResponse> {
    this.offered = request.handoffs.map((h) => h.toolName);
    return super.getResponse(request);
  }
}

for (const halted of [false, true]) {
  test(`a halted agent is offered no handoff (${halted ? "halted" : "running"})`, async () => {
    const model = new OfferedModel();
    const billing = new Agent({ name: "billing", model: new FakeModel([[say("Paid.")]]) });
    const triage = new Agent({ name: "triage", handoffs: [billing], model });
    await guardedRun(new Halts(...(halted ? [{ id: "halt-triage", agents: ["triage"] }] : [])), triage);
    assert.deepEqual(model.offered, halted ? [] : ["transfer_to_billing"]);
  });
}

test("guard reaches handed-off agents and guards each tool once", () => {
  const look = tool({ name: "look", description: "Look a question up.", parameters: z.object({ q: z.string() }), execute: async ({ q }) => q });
  const pay = tool({ name: "pay", description: "Pay an order.", parameters: z.object({ order: z.string() }), execute: async ({ order }) => order });
  const researcher = new Agent({ name: "researcher", tools: [look] });
  const billing = new Agent({ name: "billing", tools: [pay] });
  const research = researcher.asTool({ toolName: "research", toolDescription: "Research." });
  const triage = new Agent({ name: "triage", tools: [research], handoffs: [billing] });
  const processor = new WakeTracingProcessor(recorded("support").recorder, { quarantine: new Halts() });
  processor.guard(triage, researcher);
  processor.guard(triage, researcher);
  assert.equal(look.inputGuardrails?.length, 1, "the agent used as a tool, once however often it's guarded");
  assert.equal(pay.inputGuardrails?.length, 1, "the agent handed off to");
  assert.equal(research.inputGuardrails?.length, 1, "the agent tool itself: a spawn");
  assert.throws(() => new WakeTracingProcessor(recorded("support").recorder).guard(triage), /needs the brake's rules/);
});

// Tools the agent runs in its own process.

/** Answers each turn with the SDK's own items as they are, and reports them as the SDK's Responses model does. */
class ItemModel implements Model {
  readonly #turns: unknown[][];

  constructor(turns: unknown[][]) {
    this.#turns = turns;
  }

  async getResponse(request: ModelRequest): Promise<ModelResponse> {
    const output = this.#turns.shift()!;
    const responseId = `resp_${randomUUID().slice(0, 8)}`;
    const answer = { output, usage: new Usage(), responseId } as unknown as ModelResponse;
    return withResponseSpan(async (span) => {
      if (request.tracing === true) {
        span.spanData.response_id = responseId;
        span.spanData._input = request.input as never;
        span.spanData._response = { id: responseId, model: "fake-model", output, object: "response" };
      }
      return answer;
    });
  }

  async *getStreamedResponse(): AsyncIterable<never> {
    throw new Error("not streamed");
  }
}

const DONE = asItems([say("Done.")]);

function localTool(kind: "shell" | "apply_patch", ran: unknown[]) {
  if (kind === "shell") {
    const shell = shellTool({ shell: { run: async (action) => (ran.push(action.commands), { output: [{ stdout: "ok", stderr: "", outcome: { type: "exit", exitCode: 0 } }] }) } });
    return [shell, { type: "shell_call", id: "sh_1", callId: "call_x", status: "completed", action: { commands: ["rm -rf /srv/orders"] } }] as const;
  }
  const done = async (operation: { path: string }) => (ran.push(operation.path), { status: "completed" as const, output: "done" });
  const editor = applyPatchTool({ editor: { createFile: done, updateFile: done, deleteFile: done } });
  return [editor, { type: "apply_patch_call", id: "ap_1", callId: "call_x", status: "completed", operation: { type: "delete_file", path: "orders.db" } }] as const;
}

for (const kind of ["shell", "apply_patch"] as const) {
  test(`a halted agent's own ${kind} tool is refused as it starts`, async () => {
    const ran: unknown[] = [];
    const [tool, requested] = localTool(kind, ran);
    const triage = new Agent({ name: "triage", tools: [tool], model: new ItemModel([[requested], DONE]) });
    const { result, events } = await guardedRun(new Halts({ id: "halt-triage", agents: ["triage"] }), triage);
    assert.deepEqual(ran, [], "the tool never ran");
    assert.equal(result.finalOutput, "Done.");
    const [first, denied, second, ...rest] = events;
    assert.equal(rest.length, 0);
    assert.deepEqual(kinds([first, denied, second]), ["model_call", "denied", "model_call"]);
    assert.equal(attr(denied, "wake.brake.halt_id"), "halt-triage");
    assert.equal(attr(denied, "gen_ai.tool.name"), kind);
    assert.equal(attr(denied, "wake.agent.id"), "triage");
    assert.deepEqual(origins(denied), [], "the tool never ran: nothing of it entered");
    assert.deepEqual(parents(denied), [id(first)], "the response that asked for it");
    assert.ok(parents(second).includes(id(denied)), "the model read the refusal");
    assert.ok((attr(second, "wake.payload") as string).includes("Wake halted triage (halt-triage), so this tool call wasn't run."), "the refusal is the tool's output");
    // Without a halt the tool runs.
    const [again, asked] = localTool(kind, ran);
    const running = await guardedRun(new Halts(), new Agent({ name: "triage", tools: [again], model: new ItemModel([[asked], DONE]) }));
    assert.equal(ran.length, 1);
    assert.ok(!kinds(running.events).includes("denied"));
  });
}

/** A computer that notes each action it carries out. */
function screen(did: string[]) {
  const note = (action: string) => async () => void did.push(action);
  return {
    environment: "browser" as const,
    dimensions: [1024, 768] as [number, number],
    screenshot: async () => (did.push("screenshot"), "iVBORw0KGgo="),
    click: note("click"),
    doubleClick: note("doubleClick"),
    scroll: note("scroll"),
    type: note("type"),
    wait: note("wait"),
    move: note("move"),
    keypress: note("keypress"),
    drag: note("drag"),
  };
}

for (const made of [false, true]) {
  test(`a halted agent's computer does nothing (${made ? "made for each run" : "given"})`, async () => {
    const did: string[] = [];
    const computer = screen(did);
    const tool = computerTool({ computer: made ? async () => computer : computer });
    const clicked = { type: "computer_call", id: "cu_1", callId: "call_x", status: "completed", action: { type: "click", x: 1, y: 2, button: "left" } };
    const triage = new Agent({ name: "triage", tools: [tool], model: new ItemModel([[clicked], DONE]) });
    const { events } = await guardedRun(new Halts({ id: "halt-triage", agents: ["triage"] }), triage);
    assert.deepEqual(did, [], "the computer did nothing");
    const denied = events.filter((e) => attr(e, "wake.event.kind") === "denied");
    assert.equal(denied.length, 1);
    assert.equal(attr(denied[0], "wake.brake.halt_id"), "halt-triage");
    assert.deepEqual(origins(denied[0]), []);
  });
}

test("guard throws rather than leave a tool it can't refuse, and keeps hosted tools, checked before each model call", () => {
  const search = webSearchTool();
  const triage = new Agent({ name: "triage", instructions: "Be brief.", tools: [search] });
  const processor = new WakeTracingProcessor(recorded("support").recorder, { quarantine: new Halts() });
  processor.guard(triage);
  assert.equal(triage.tools[0], search, "a hosted tool runs at OpenAI: there's no call of it here to refuse");
  assert.equal(typeof triage.instructions, "function", "its agent's model calls are checked instead");
  const checked = triage.instructions;
  processor.guard(triage);
  assert.equal(triage.instructions, checked, "once however often it's guarded");
  const plain = new Agent({ name: "plain", instructions: "Be brief.", tools: [] });
  processor.guard(plain);
  assert.equal(plain.instructions, "Be brief.", "an agent that offers none is left as it is");
  const ops = new Agent({ name: "ops" });
  ops.tools.push({ type: "gadget", name: "gadget" } as never); // a kind of tool this version of Wake doesn't know
  assert.throws(() => processor.guard(ops), (error: unknown) => error instanceof TypeError && /gadget/.test(error.message));
});

test("a guardrail run before a call's approval records the call it refuses", async () => {
  const paid: string[] = [];
  const pay = tool({ name: "pay", description: "Pay an order.", parameters: z.object({ order: z.string() }), needsApproval: true, execute: async ({ order }) => (paid.push(order), "Paid.") });
  const payer = new Agent({ name: "payer", tools: [pay], model: new FakeModel([[call("pay", JSON.stringify({ order: "1234" }), "call_pay")], [say("Done.")]]) });
  const { recorder, spans } = recorded("support");
  const processor = new WakeTracingProcessor(recorder, { quarantine: new Halts({ id: "halt-payer", agents: ["payer"] }) });
  setTraceProcessors([processor]);
  const result = await new Runner({ toolExecution: { preApprovalInputGuardrails: true } }).run(processor.guard(payer), "Pay order 1234.");
  assert.equal(result.interruptions?.length ?? 0, 0, "refused before anyone was asked to approve it");
  assert.deepEqual(paid, []);
  const [first, denied, second, ...rest] = wakeEvents(await spans());
  assert.equal(rest.length, 0);
  assert.deepEqual(kinds([first, denied, second]), ["model_call", "denied", "model_call"]);
  assert.equal(attr(denied, "gen_ai.tool.name"), "pay");
  assert.equal(attr(denied, "wake.brake.halt_id"), "halt-payer");
  assert.equal(attr(denied, "wake.agent.id"), "payer");
  assert.deepEqual(parents(denied), [id(first)], "the response that asked for it");
  assert.ok(parents(second).includes(id(denied)), "the model read the refusal");
  assert.equal(denied.spanContext().traceId, first.spanContext().traceId);
});

// Handoffs.

for (const halted of ["triage", "billing"]) {
  test(`a handoff from or to a halted agent isn't offered (${halted} halted)`, async () => {
    const model = new OfferedModel();
    const billing = new Agent({ name: "billing", model: new FakeModel([[say("Paid.")]]) });
    const triage = new Agent({ name: "triage", handoffs: [billing], model });
    await guardedRun(new Halts({ id: `halt-${halted}`, agents: [halted] }), triage);
    assert.deepEqual(model.offered, []);
  });
}

/** The halt lands while the model is answering the turn that hands off. */
class HaltDuringTurn extends FakeModel {
  readonly #halts: Halts;
  readonly #halt: Halt;

  constructor(turns: ApiItem[][], halts: Halts, halt: Halt) {
    super(turns);
    this.#halts = halts;
    this.#halt = halt;
  }

  override async getResponse(request: ModelRequest): Promise<ModelResponse> {
    this.#halts.halts.push(this.#halt);
    return super.getResponse(request);
  }
}

for (const halted of ["triage", "billing"]) {
  test(`a handoff picked before a halt landed is refused as it starts (${halted} halted)`, async () => {
    const paid: string[] = [];
    const pay = tool({ name: "pay", description: "Pay an order.", parameters: z.object({ order: z.string() }), execute: async ({ order }) => (paid.push(order), "Paid.") });
    const billing = new Agent({ name: "billing", tools: [pay], model: new FakeModel([[call("pay", JSON.stringify({ order: "1234" }), "call_pay")], [say("Paid.")]]) });
    const halts = new Halts();
    const triage = new Agent({ name: "triage", handoffs: [billing], model: new HaltDuringTurn([[call("transfer_to_billing", "{}", "call_h")]], halts, { id: `halt-${halted}`, agents: [halted] }) });
    const { recorder, spans } = recorded("support");
    const processor = new WakeTracingProcessor(recorder, { quarantine: halts });
    setTraceProcessors([processor]);
    await assert.rejects(new Runner().run(processor.guard(triage), "Refund 1234."), (error: unknown) => {
      assert.ok(error instanceof AgentHalted, String(error));
      assert.equal(error.halt, `halt-${halted}`);
      assert.match(error.message, /so the handoff to billing wasn't made\.$/);
      return true;
    });
    assert.deepEqual(paid, [], "billing never ran");
    const [response, denied, ...rest] = wakeEvents(await spans());
    assert.equal(rest.length, 0);
    assert.deepEqual(kinds([response, denied]), ["model_call", "denied"]);
    assert.equal(denied.name, "agent_message triage -> billing");
    assert.equal(attr(denied, "wake.agent.id"), "triage", "recorded as what tried to hand off");
    assert.equal(attr(denied, "wake.brake.receiver"), "billing");
    assert.equal(attr(denied, "wake.brake.halt_id"), `halt-${halted}`);
    assert.deepEqual(parents(denied), [id(response)]);
    assert.deepEqual(origins(denied), [], "nothing reached billing");
  });
}

// Runs of one agent at once.

function researcherAgent() {
  const lookup = tool({
    name: "lookup",
    description: "Look a question up.",
    parameters: z.object({ q: z.string() }),
    execute: async ({ q }) => {
      await new Promise((resolve) => setImmediate(resolve));
      return `Found: ${q}`;
    },
  });
  return new Agent({ name: "researcher", tools: [lookup], model: new ResearcherModel() });
}

test("two runs of one agent inside one tool call are two instances that never cross", async () => {
  const agent = researcherAgent();
  const researchTwice = tool({
    name: "research_twice",
    description: "Research a question twice over.",
    parameters: z.object({ question: z.string() }),
    execute: async ({ question }) => (await Promise.all([run(agent, question), run(agent, question)])).map((r) => r.finalOutput).join("\n"),
  });
  const triage = new Agent({
    name: "triage",
    tools: [researchTwice],
    model: new FakeModel([[call("research_twice", JSON.stringify({ question: LONG_QUESTION }), "call_twice")], [say("Both answered.")]]),
  });
  const events = wakeEvents((await runWith(triage)).spans);
  const byInstance = new Map<string, ReadableSpan[]>();
  for (const e of events.filter((e) => attr(e, "wake.agent.id") === "researcher")) {
    assert.deepEqual(attr(e, "wake.agent.lineage"), ["triage"]);
    const instance = attr(e, "wake.agent.instance") as string;
    byInstance.set(instance, [...(byInstance.get(instance) ?? []), e]);
  }
  assert.equal(byInstance.size, 2, [...byInstance.keys()].join(", "));
  const toolCall = events.find((e) => attr(e, "gen_ai.tool.name") === "research_twice")!;
  const triageIds = ids(...events.filter((e) => attr(e, "wake.agent.id") === "triage"));
  const lastAnswers: string[] = [];
  for (const [instance, own] of byInstance) {
    assert.deepEqual(kinds(own).sort(), ["model_call", "model_call", "tool_call"], instance);
    const ownIds = ids(...own);
    for (const e of own) {
      const crossed = parents(e).filter((p) => !ownIds.has(p) && !triageIds.has(p));
      assert.deepEqual(crossed, [], `${instance} ${String(attr(e, "wake.event.kind"))} links to the other copy`);
    }
    const [first, last] = own.filter((e) => attr(e, "wake.event.kind") === "model_call");
    assert.ok(parents(first).includes(id(toolCall)), "each run descends from the tool call");
    lastAnswers.push(id(last));
  }
  // What both runs said last is what the tool returned, which triage's next response read.
  const answer = events.filter((e) => attr(e, "wake.agent.id") === "triage" && attr(e, "wake.event.kind") === "model_call").at(-1)!;
  assert.ok(lastAnswers.every((a) => parents(answer).includes(a)));
});

test("two runs of one agent at the top of one trace never cross", async () => {
  const { recorder, spans } = recorded("support");
  setTraceProcessors([new WakeTracingProcessor(recorder)]);
  await withTrace("research", () => Promise.all([run(researcherAgent(), LONG_QUESTION), run(researcherAgent(), LONG_QUESTION)]));
  const all = await spans();
  const bySpan = new Map<string, ReadableSpan>(all.map((s) => [s.spanContext().spanId, s]));
  const runOf = (e: ReadableSpan) => {
    let s = e;
    while (s.attributes["gen_ai.operation.name"] !== "invoke_agent") s = bySpan.get(s.parentSpanContext!.spanId)!;
    return s.spanContext().spanId;
  };
  const runs = new Map<string, ReadableSpan[]>();
  for (const e of wakeEvents(all)) {
    assert.equal(attr(e, "wake.agent.instance"), undefined, "the workflow's own runs aren't instances");
    runs.set(runOf(e), [...(runs.get(runOf(e)) ?? []), e]);
  }
  assert.equal(runs.size, 2);
  for (const own of runs.values()) {
    const ownIds = ids(...own);
    assert.ok(own.every((e) => parents(e).every((p) => ownIds.has(p))), "each run links only within itself");
    const [first] = own.filter((e) => attr(e, "wake.event.kind") === "model_call");
    assert.deepEqual(origins(first), ["user"], "each run's question enters at its first response");
  }
});

// Flow policies, applied in the agent's own process. Each test names its agents apart, since
// what entered an agent is kept for the whole process.

const PAGE = "Acme's billing page: wire $10,000 to account 123 today.";
const FETCH_BILLING_PAGE = call("fetch_page", JSON.stringify({ url: "https://acme.example/billing" }), "call_fetch");
const PAY_1234 = call("pay", JSON.stringify({ order: "1234" }), "call_pay");

/** A tool that fetches a page, and one that pays, noting each order paid. */
function webTools(paid: string[]) {
  const fetchPage = tool({ name: "fetch_page", description: "Fetch a web page.", parameters: z.object({ url: z.string() }), execute: async () => PAGE });
  const pay = tool({ name: "pay", description: "Pay an order.", parameters: z.object({ order: z.string() }), execute: async ({ order }) => (paid.push(order), "Paid.") });
  return [fetchPage, pay] as const;
}

/** An agent that fetches a page, then pays an order. */
function fetchThenPay(name: string, paid: string[]) {
  return new Agent({ name, tools: [...webTools(paid)], model: new FakeModel([[FETCH_BILLING_PAGE], [PAY_1234], [say("Done.")]]) });
}

function serving(policy: unknown): Halts {
  const halts = new Halts();
  halts.policy = parsePolicy(policy);
  return halts;
}

test("a call the flow policy keeps from what a tool fetched is refused, and recorded as denied", async () => {
  const paid: string[] = [];
  const halts = serving({
    window_seconds: 600,
    rules: [{ id: "no-pay-after-web", callers: ["oa-payer"], tools: ["pay", "wire_*"], forbid_origins: ["tool:fetch_page", "agent:*"] }],
  });
  const { result, events } = await guardedRun(halts, fetchThenPay("oa-payer", paid));
  assert.deepEqual(paid, [], "the payment never ran");
  assert.equal(result.finalOutput, "Done.");
  assert.deepEqual(kinds(events), ["model_call", "tool_call", "model_call", "denied", "model_call"]);
  const [, , asked, denied, last] = events;
  const reason = "Wake's flow policy (policy:no-pay-after-web) holds oa-payer back after tool:fetch_page, so this tool call wasn't run.";
  assert.equal(attr(denied, "gen_ai.tool.name"), "pay");
  assert.equal(attr(denied, "wake.agent.id"), "oa-payer");
  assert.equal(attr(denied, "wake.brake.halt_id"), "policy:no-pay-after-web");
  assert.ok(String(payload(denied).output).includes(reason), String(payload(denied).output));
  assert.deepEqual(payload(denied).origins, ["tool:fetch_page"]);
  assert.deepEqual(origins(denied), [], "nothing entered at the denial");
  assert.deepEqual(parents(denied), [id(asked)], "the response that asked for it");
  assert.ok((attr(last, "wake.payload") as string).includes(reason), "the model read the refusal");
});

test("a handoff the flow policy keeps is refused as it starts, and isn't offered after", async () => {
  const paid: string[] = [];
  const [fetchPage, pay] = webTools(paid);
  const billing = new Agent({ name: "oa-billing", tools: [pay], model: new FakeModel([[PAY_1234], [say("Paid.")]]) });
  // The page is fetched in the response that hands off, which was offered the handoff before.
  const triage = new Agent({ name: "oa-triage", tools: [fetchPage], handoffs: [billing], model: new FakeModel([[FETCH_BILLING_PAGE, call("transfer_to_oa_billing", "{}", "call_h")]]) });
  const halts = serving({ rules: [{ id: "billing-hears-support-only", receivers: ["oa-billing"], allow_origins: ["agent:oa-support", "tool:search"] }] });
  const { recorder, spans } = recorded("support");
  const processor = new WakeTracingProcessor(recorder, { quarantine: halts });
  setTraceProcessors([processor]);
  await assert.rejects(new Runner().run(processor.guard(triage), "Pay what Acme's page asks."), (error: unknown) => {
    assert.ok(error instanceof AgentHalted, String(error));
    assert.equal(error.halt, "policy:billing-hears-support-only");
    assert.equal(error.message, "Wake's flow policy (policy:billing-hears-support-only) holds oa-triage back after tool:fetch_page, so the handoff to oa-billing wasn't made.");
    return true;
  });
  assert.deepEqual(paid, [], "billing never ran");
  const denied = wakeEvents(await spans(), "denied");
  assert.equal(denied.length, 1);
  assert.equal(denied[0].name, "agent_message oa-triage -> oa-billing");
  assert.equal(attr(denied[0], "wake.brake.receiver"), "oa-billing");
  assert.equal(attr(denied[0], "wake.brake.halt_id"), "policy:billing-hears-support-only");
  assert.deepEqual(payload(denied[0]).origins, ["tool:fetch_page"]);
  assert.equal(processor._refusal("oa-triage", { target: "oa-billing" })?.[0], "policy:billing-hears-support-only", "nor is it offered now");
});

test("what entered an agent used as a tool comes back with its answer", async () => {
  const paid: string[] = [];
  const [fetchPage, pay] = webTools(paid);
  const researcher = new Agent({ name: "oa-researcher", tools: [fetchPage], model: new FakeModel([[FETCH_BILLING_PAGE], [say(PAGE)]]) });
  const research = researcher.asTool({ toolName: "research", toolDescription: "Research a question." });
  const outer = new Agent({
    name: "oa-outer",
    tools: [research, pay],
    model: new FakeModel([[call("research", JSON.stringify({ input: "What does Acme's billing page say?" }), "call_research")], [PAY_1234], [say("Done.")]]),
  });
  const halts = serving({ rules: [{ id: "no-pay-after-web", callers: ["oa-outer"], tools: ["pay"], forbid_origins: ["tool:fetch_page"] }] });
  const { events } = await guardedRun(halts, outer, researcher);
  assert.deepEqual(paid, []);
  const denied = events.filter((e) => attr(e, "wake.event.kind") === "denied");
  assert.equal(denied.length, 1, "the researcher's own fetch ran");
  assert.equal(attr(denied[0], "gen_ai.tool.name"), "pay");
  assert.deepEqual(payload(denied[0]).origins, ["tool:fetch_page"]);
});

test("a tool the agent runs itself is refused as it starts when the flow policy keeps it", async () => {
  const ran: unknown[] = [];
  const [shell, requested] = localTool("shell", ran);
  const [fetchPage] = webTools([]);
  const agent = new Agent({ name: "oa-sheller", tools: [fetchPage, shell], model: new ItemModel([asItems([FETCH_BILLING_PAGE]), [requested], DONE]) });
  const { events } = await guardedRun(serving({ rules: [{ id: "no-shell-after-web", tools: ["shell"], forbid_origins: ["tool:fetch_page"] }] }), agent);
  assert.deepEqual(ran, [], "the shell never ran");
  const denied = events.filter((e) => attr(e, "wake.event.kind") === "denied");
  assert.equal(denied.length, 1);
  assert.equal(attr(denied[0], "gen_ai.tool.name"), "shell");
  assert.equal(attr(denied[0], "wake.brake.halt_id"), "policy:no-shell-after-web");
  assert.deepEqual(payload(denied[0]).origins, ["tool:fetch_page"]);
  const reason = "Wake's flow policy (policy:no-shell-after-web) holds oa-sheller back after tool:fetch_page, so this tool call wasn't run.";
  assert.ok((attr(events.at(-1)!, "wake.payload") as string).includes(reason), "the refusal is the shell's output");
});

test("a flow policy wake-proxy can't use refuses every call", async () => {
  const paid: string[] = [];
  const { events } = await guardedRun(serving({ unreadable: true }), fetchThenPay("oa-unread", paid));
  assert.deepEqual(paid, []);
  assert.deepEqual(kinds(events), ["model_call", "denied", "model_call", "denied", "model_call"]);
  for (const denied of events.filter((e) => attr(e, "wake.event.kind") === "denied")) {
    assert.equal(attr(denied, "wake.brake.halt_id"), UNREADABLE);
    assert.ok(String(payload(denied).output).includes("Wake's flow policy can't be read, so this tool call wasn't run."));
    assert.deepEqual(payload(denied).origins, []);
  }
});

test("without a flow policy nothing is refused for what entered the agent", async () => {
  const paid: string[] = [];
  const { events } = await guardedRun(new Halts(), fetchThenPay("oa-free", paid));
  assert.deepEqual(paid, ["1234"]);
  assert.ok(!kinds(events).includes("denied"));
});

// Hosted tools: OpenAI runs them inside the model call. Each test names its agents apart, as
// above.

const WEB_SEARCH = searched("acme billing account");

/** The origins that entered an agent, which here are never every origin. */
function seenBy(agent: string): string[] {
  const seen = entered.seen(agent);
  assert.ok(Array.isArray(seen), `${agent} has every origin`);
  return [...seen];
}

/** Answers from a script, noting what each call was given. */
class NotingModel extends FakeModel {
  readonly instructions: (string | undefined)[] = [];
  readonly inputs: unknown[] = [];

  override async getResponse(request: ModelRequest): Promise<ModelResponse> {
    this.instructions.push(request.systemInstructions);
    this.inputs.push(request.input);
    return super.getResponse(request);
  }
}

/** A trace processor ahead of Wake that holds up each span's end, as OpenAI's exporter does while it sends a batch. */
class SlowProcessor implements TracingProcessor {
  readonly #millis: number;
  readonly #held = new Set<Promise<void>>();

  constructor(millis: number) {
    this.#millis = millis;
  }

  async onTraceStart(): Promise<void> {}
  async onTraceEnd(): Promise<void> {}
  async onSpanStart(): Promise<void> {}

  async onSpanEnd(): Promise<void> {
    const held = new Promise<void>((resolve) => setTimeout(resolve, this.#millis));
    this.#held.add(held);
    await held;
    this.#held.delete(held);
  }

  async shutdown(): Promise<void> {}
  async forceFlush(): Promise<void> {}

  /** Resolves once each span end it held up has gone on to the processors after it. */
  async drained(): Promise<void> {
    while (this.#held.size > 0) await Promise.all(this.#held);
    await new Promise((resolve) => setImmediate(resolve));
  }
}

/** Runs the agent, guarded, with slow ahead of Wake. */
async function slowRun(slow: SlowProcessor, quarantine: Halts, agent: Agent<any, any>, { waitMillis }: { waitMillis?: number } = {}) {
  const { recorder, spans } = recorded("support");
  const processor = new WakeTracingProcessor(recorder, { quarantine });
  if (waitMillis !== undefined) processor._waitMillis = waitMillis;
  setTraceProcessors([slow, processor]);
  const result = await new Runner().run(processor.guard(agent), "Pay what Acme's site says.");
  await slow.drained();
  return { result, events: wakeEvents(await spans()) };
}

/** Runs the agent, guarded, with these run options. */
async function guardedRunWith(quarantine: Halts, agent: Agent<any, any>, options: { traceIncludeSensitiveData?: boolean } = {}) {
  const { recorder, spans } = recorded("support");
  const processor = new WakeTracingProcessor(recorder, { quarantine });
  setTraceProcessors([processor]);
  const result = await new Runner(options).run(processor.guard(agent), "Pay what Acme's site says.");
  return { result, events: wakeEvents(await spans()), processor };
}

test("a hosted call is noted before a tool from its response is checked, though a processor before Wake holds up spans' ends", async () => {
  assert.equal(RESPONSE_WAIT_MILLIS, 5_000);
  const paid: string[] = [];
  const [, pay] = webTools(paid);
  const agent = new Agent({ name: "oa-searcher", tools: [webSearchTool(), pay], model: new FakeModel([[WEB_SEARCH, PAY_1234], [say("Done.")]]) });
  const halts = serving({ rules: [{ id: "no-pay-after-search", tools: ["pay"], forbid_origins: ["tool:web_search"] }] });
  const { result, events } = await slowRun(new SlowProcessor(50), halts, agent);
  assert.equal(result.finalOutput, "Done.");
  assert.deepEqual(paid, [], "the payment never ran");
  const denied = events.filter((e) => attr(e, "wake.event.kind") === "denied");
  assert.equal(denied.length, 1);
  assert.equal(attr(denied[0], "gen_ai.tool.name"), "pay");
  assert.equal(attr(denied[0], "wake.brake.halt_id"), "policy:no-pay-after-search");
  assert.deepEqual(payload(denied[0]).origins, ["tool:web_search"]);
  assert.deepEqual(entered.seen("oa-searcher"), ["tool:web_search"]);
});

test("past its bound, a check that hasn't seen a response end refuses, as one that failed", async () => {
  const paid: string[] = [];
  const [, pay] = webTools(paid);
  const agent = new Agent({ name: "oa-unseen", tools: [pay], model: new FakeModel([[PAY_1234], [say("Done.")]]) });
  const { result, events } = await slowRun(new SlowProcessor(300), new Halts(), agent, { waitMillis: 30 });
  assert.equal(result.finalOutput, "Done.");
  assert.deepEqual(paid, []);
  const [denied, ...rest] = events.filter((e) => attr(e, "wake.event.kind") === "denied");
  assert.equal(rest.length, 0);
  assert.equal(attr(denied, "wake.brake.halt_id"), UNCHECKED);
  assert.equal(payload(denied).output, "Wake couldn't check its halts, so this tool call wasn't run.");
});

test("without sensitive data, every hosted tool a model call offered but MCP enters, and with it only what ran", async () => {
  for (const sensitive of [false, true]) {
    const name = `oa-blind-${sensitive}`;
    const paid: string[] = [];
    const [, pay] = webTools(paid);
    const tools = [webSearchTool({ name: "search_the_web" }), fileSearchTool("vs_1"), crm(), pay];
    const agent = new Agent({ name, tools, model: new FakeModel([[PAY_1234], [say("Done.")]]) });
    const halts = serving({ rules: [{ id: "no-pay-after-search", tools: ["pay"], forbid_origins: ["tool:search_the_web"] }] });
    await guardedRunWith(halts, agent, { traceIncludeSensitiveData: sensitive });
    if (sensitive) {
      assert.deepEqual(paid, ["1234"], "nothing hosted ran, as the response says");
      assert.deepEqual(entered.seen(name), ["tool:pay"]);
    } else {
      assert.deepEqual(paid, [], "the trace can't say the search didn't run");
      assert.deepEqual(new Set(seenBy(name)), new Set(["tool:search_the_web", "tool:file_search"]), "by the names the agent offers them under, and no MCP tool");
    }
  }
  // For an agent guard() doesn't cover, the SDK's own hosted tools' names its span says it offers.
  const { recorder } = recorded("support");
  setTraceProcessors([new WakeTracingProcessor(recorder)]);
  const tools = [webSearchTool(), webSearchTool({ name: "search_the_web" }), fileSearchTool("vs_1"), crm()];
  await new Runner({ traceIncludeSensitiveData: false }).run(new Agent({ name: "oa-blind-plain", tools, model: new FakeModel([[say("Done.")]]) }), "Look it up.");
  assert.deepEqual(new Set(seenBy("oa-blind-plain")), new Set(["tool:web_search", "tool:file_search"]));
});

test("each hosted call a response reports enters its agent, by the name it's offered under, guarded or not", async () => {
  const codeRan = { type: "code_interpreter_call", id: "ci_1", status: "completed", code: "print(1)", container_id: "cntr_1", outputs: [] };
  const turns = () => [[WEB_SEARCH, codeRan, mcpCall("crm", "lookup_order", "Order 1234: paid."), say("Found it.")]];
  // Recorded, not guarded: Wake doesn't see what the agent offers, so each goes by its kind.
  const { recorder } = recorded("support");
  setTraceProcessors([new WakeTracingProcessor(recorder)]);
  await new Runner().run(new Agent({ name: "oa-hosted-plain", tools: [webSearchTool({ name: "search_the_web" })], model: new FakeModel(turns()) }), "Look it up.");
  assert.deepEqual(new Set(seenBy("oa-hosted-plain")), new Set(["tool:web_search", "tool:code_interpreter", "tool:lookup_order"]));
  // Guarded, by the names the agent offers them under.
  await guardedRunWith(new Halts(), new Agent({ name: "oa-hosted-named", tools: [webSearchTool({ name: "search_the_web" })], model: new FakeModel(turns()) }));
  assert.deepEqual(new Set(seenBy("oa-hosted-named")), new Set(["tool:search_the_web", "tool:code_interpreter", "tool:lookup_order"]));
  // A shell in a hosted container, by its call.
  const sandbox = shellTool({ name: "sandbox", environment: { type: "container_auto" } });
  const shellCall = { type: "shell_call", id: "sh_1", callId: "call_sh", status: "completed", action: { commands: ["cat /mnt/data/page.html"] } };
  await guardedRunWith(new Halts(), new Agent({ name: "oa-hosted-shell", tools: [sandbox], model: new ItemModel([[shellCall], DONE]) }));
  assert.deepEqual(entered.seen("oa-hosted-shell"), ["tool:sandbox"]);
});

test("a model call that could run a hosted tool a halt covers isn't made, and the run stops", async () => {
  const model = new NotingModel([[say("Searched.")]]);
  const hosted = [webSearchTool(), fileSearchTool("vs_1"), shellTool({ environment: { type: "container_auto" } })];
  const agent = new Agent({ name: "oa-hosted-halted", instructions: "Be brief.", tools: hosted, model });
  const { recorder, spans } = recorded("support");
  const processor = new WakeTracingProcessor(recorder, { quarantine: new Halts({ id: "halt-hosted", agents: ["oa-hosted-halted"] }) });
  setTraceProcessors([processor]);
  const outcome = (name: string) => `so its model call, which could run ${name} at OpenAI, wasn't made.`;
  await assert.rejects(new Runner().run(processor.guard(agent), "Search the web."), (error: unknown) => {
    assert.ok(error instanceof AgentHalted, String(error));
    assert.equal(error.halt, "halt-hosted");
    assert.equal(error.message, `Wake halted oa-hosted-halted (halt-hosted), ${outcome("web_search")}`);
    return true;
  });
  assert.equal(model.inputs.length, 0, "the model was never called");
  const denied = wakeEvents(await spans(), "denied");
  assert.deepEqual(denied.map((e) => attr(e, "gen_ai.tool.name")), ["web_search", "file_search", "shell"], "each held back, by its name");
  for (const e of denied) {
    assert.equal(attr(e, "wake.brake.halt_id"), "halt-hosted");
    assert.equal(attr(e, "wake.agent.id"), "oa-hosted-halted");
    assert.equal(payload(e).output, `Wake halted oa-hosted-halted (halt-hosted), ${outcome(String(attr(e, "gen_ai.tool.name")))}`);
    assert.deepEqual(origins(e), []);
  }

  // Not halted, the call is made with the instructions the agent gives, a string or a function.
  for (const instructions of ["Be brief.", async (_context: unknown, a: Agent<any, any>) => `You are ${a.name}.`]) {
    const free = new NotingModel([[say("Searched.")]]);
    const name = `oa-hosted-free-${typeof instructions}`;
    await guardedRunWith(new Halts(), new Agent({ name, instructions, tools: [webSearchTool()], model: free }));
    assert.deepEqual(free.instructions, [typeof instructions === "string" ? instructions : `You are ${name}.`]);
  }
});

test("a model call is refused when the flow policy holds back a hosted tool it offers", async () => {
  const [fetchPage] = webTools([]);
  const model = new NotingModel([[FETCH_BILLING_PAGE], [say("Searched.")]]);
  const agent = new Agent({ name: "oa-fetch-then-search", tools: [fetchPage, webSearchTool(), fileSearchTool("vs_1")], model });
  const halts = serving({ rules: [{ id: "no-search-after-fetch", tools: ["web_search"], forbid_origins: ["tool:fetch_page"] }] });
  const { recorder, spans } = recorded("support");
  const processor = new WakeTracingProcessor(recorder, { quarantine: halts });
  setTraceProcessors([processor]);
  await assert.rejects(new Runner().run(processor.guard(agent), "Search the web."), (error: unknown) => {
    assert.ok(error instanceof AgentHalted, String(error));
    assert.equal(error.halt, "policy:no-search-after-fetch");
    assert.equal(
      error.message,
      "Wake's flow policy (policy:no-search-after-fetch) holds oa-fetch-then-search back after tool:fetch_page, so its model call, which could run web_search at OpenAI, wasn't made.",
    );
    return true;
  });
  assert.equal(model.inputs.length, 1, "the first call was made, and the one after the page wasn't");
  const events = wakeEvents(await spans());
  const denied = events.filter((e) => attr(e, "wake.event.kind") === "denied");
  assert.equal(denied.length, 1, "file search is no rule's");
  assert.equal(attr(denied[0], "gen_ai.tool.name"), "web_search");
  assert.deepEqual(payload(denied[0]).origins, ["tool:fetch_page"]);
  const [first] = events.filter((e) => attr(e, "wake.event.kind") === "model_call");
  assert.deepEqual(parents(denied[0]), [id(first)], "the agent's latest response");
});

// Hosted MCP: the server's tools run at OpenAI a call at a time, and can ask first.

const LOOKUP = mcpRequest("crm", "lookup_order", JSON.stringify({ order: "1234" }), "mcpr_1");

function crm(options: Record<string, unknown> = {}) {
  return hostedMcpTool({ serverLabel: "crm", serverUrl: "https://crm.example/mcp", ...options } as Parameters<typeof hostedMcpTool>[0]);
}

/** What the SDK answered the server's request with, in the model's next input. */
function answered(model: NotingModel): { approve: boolean; approval_request_id: string; reason?: string } | undefined {
  const input = (model.inputs.at(-1) ?? []) as Record<string, unknown>[];
  return input.find((i) => i.type === "hosted_tool_call" && i.name === "mcp_approval_response")?.providerData as never;
}

async function crmRun(name: string, tool: ReturnType<typeof crm>, quarantine: Halts) {
  const model = new NotingModel([[LOOKUP], [say("Done.")]]);
  const { result, events } = await guardedRunWith(quarantine, new Agent({ name, tools: [tool], model }));
  return { result, events, answer: answered(model) };
}

test("a hosted MCP call a halt covers is rejected with the refusal, and recorded as denied", async () => {
  const { result, events, answer } = await crmRun("oa-crm-halted", crm(), new Halts({ id: "halt-crm", agents: ["oa-crm-halted"] }));
  assert.equal(result.finalOutput, "Done.");
  const reason = "Wake halted oa-crm-halted (halt-crm), so this tool call wasn't run.";
  assert.deepEqual([answer?.approve, answer?.approval_request_id, answer?.reason], [false, "mcpr_1", reason]);
  const [denied, ...rest] = events.filter((e) => attr(e, "wake.event.kind") === "denied");
  assert.equal(rest.length, 0);
  assert.equal(denied.name, "execute_tool lookup_order");
  assert.equal(attr(denied, "gen_ai.tool.name"), "lookup_order");
  assert.equal(attr(denied, "wake.brake.halt_id"), "halt-crm");
  assert.equal(attr(denied, "wake.mcp.server"), "crm");
  assert.equal(attr(denied, "wake.agent.id"), "oa-crm-halted");
  assert.deepEqual([payload(denied).input, payload(denied).output], [JSON.stringify({ order: "1234" }), reason]);
  const [asked] = events.filter((e) => attr(e, "wake.event.kind") === "model_call");
  assert.deepEqual(parents(denied), [id(asked)], "the response that asked for it");
  assert.deepEqual(entered.seen("oa-crm-halted"), [], "it never ran");
});

test("an allowed hosted MCP call is approved, and its result enters the agent then", async () => {
  const tool = crm();
  const { answer, events } = await crmRun("oa-crm-open", tool, new Halts());
  assert.equal(tool.providerData.require_approval, "always", "it asks before every call");
  assert.deepEqual([answer?.approve, answer?.approval_request_id], [true, "mcpr_1"]);
  assert.ok(!kinds(events).includes("denied"));
  assert.deepEqual(entered.seen("oa-crm-open"), ["tool:lookup_order"]);
});

test("a hosted MCP call the flow policy keeps is rejected, with what the rule kept", async () => {
  const [fetchPage] = webTools([]);
  const model = new NotingModel([[FETCH_BILLING_PAGE], [LOOKUP], [say("Done.")]]);
  const agent = new Agent({ name: "oa-crm-after-web", tools: [fetchPage, crm()], model });
  const halts = serving({ rules: [{ id: "no-crm-after-web", tools: ["lookup_order"], forbid_origins: ["tool:fetch_page"] }] });
  const { events } = await guardedRunWith(halts, agent);
  const answer = answered(model);
  assert.equal(answer?.approve, false);
  assert.equal(answer?.reason, "Wake's flow policy (policy:no-crm-after-web) holds oa-crm-after-web back after tool:fetch_page, so this tool call wasn't run.");
  const [denied] = events.filter((e) => attr(e, "wake.event.kind") === "denied");
  assert.equal(attr(denied, "wake.brake.halt_id"), "policy:no-crm-after-web");
  assert.deepEqual(payload(denied).origins, ["tool:fetch_page"]);
});

test("an allowed hosted MCP call goes to the tool's own callback when its setting would have asked", async () => {
  const asked: string[] = [];
  const onApproval = async (_context: unknown, item: { name?: string }) => (asked.push(item.name ?? ""), { approve: false, reason: "Not without a ticket." });
  // Its setting asks about every call: the callback answers what Wake allows.
  const always = crm({ requireApproval: "always", onApproval });
  let run = await crmRun("oa-crm-callback", always, new Halts());
  assert.notEqual(always.providerData.on_approval, onApproval, "Wake answers first");
  assert.deepEqual(asked, ["lookup_order"]);
  assert.deepEqual([run.answer?.approve, run.answer?.reason], [false, "Not without a ticket."]);
  assert.ok(!kinds(run.events).includes("denied"), "the app refused it, not Wake");
  assert.deepEqual(entered.seen("oa-crm-callback"), [], "and it never ran");
  // What Wake refuses never reaches the callback.
  run = await crmRun("oa-crm-callback-halted", crm({ requireApproval: "always", onApproval }), new Halts({ id: "halt-crm", agents: ["oa-crm-callback-halted"] }));
  assert.equal(asked.length, 1);
  assert.equal(run.answer?.reason, "Wake halted oa-crm-callback-halted (halt-crm), so this tool call wasn't run.");
  // A call its setting wouldn't have asked about, Wake approves itself.
  const filtered = crm({ requireApproval: { never: { toolNames: ["lookup_order"] } }, onApproval });
  run = await crmRun("oa-crm-filtered", filtered, new Halts());
  assert.equal(filtered.providerData.require_approval, "always");
  assert.equal(asked.length, 1, "the callback wasn't asked");
  assert.equal(run.answer?.approve, true);
  assert.deepEqual(entered.seen("oa-crm-filtered"), ["tool:lookup_order"]);
  // Whether a tool is read-only is the server's to say, so a readOnly filter leaves the callback to answer.
  run = await crmRun("oa-crm-read-only", crm({ requireApproval: { never: { readOnly: true } }, onApproval }), new Halts());
  assert.equal(asked.length, 2);
  assert.equal(run.answer?.approve, false);
});

test("a hosted MCP tool the app answers through run interruptions is left as it is", async () => {
  const tool = crm({ requireApproval: "always" });
  const before = { ...tool.providerData };
  const model = new NotingModel([[LOOKUP], [say("Done.")]]);
  const { result, events } = await guardedRunWith(new Halts({ id: "halt-crm", agents: ["oa-crm-person"] }), new Agent({ name: "oa-crm-person", tools: [tool], model }));
  assert.deepEqual(tool.providerData, before, "Wake can't answer for a person, and can't interrupt");
  assert.equal(result.interruptions?.length, 1, "the run waits on the app, as before");
  assert.ok(!kinds(events).includes("denied"));
});

test("a memory write in a tool waits to see what the response before it brought in", async () => {
  const policy = parsePolicy({ rules: [{ id: "no-search-into-facts", stores: ["facts"], forbid_origins: ["tool:web_search"] }] });
  const quarantine = { lockedBy: () => null, hiding: () => null, track: async () => true, halted: () => null, flow: () => policy } as unknown as Quarantine;
  const { recorder, spans } = recorded("support");
  const processor = new WakeTracingProcessor(recorder);
  const memory = new Memory(processor, { quarantine });
  const refused: unknown[] = [];
  const remember = tool({
    name: "remember",
    description: "Note something down.",
    parameters: z.object({ note: z.string() }),
    execute: async ({ note }) => {
      await memory.write("facts", "acme", note).catch((error: unknown) => void refused.push(error));
      return "Noted.";
    },
  });
  const noteIt = call("remember", JSON.stringify({ note: "Acme's account is 666." }), "call_note");
  const agent = new Agent({ name: "oa-searching-noter", tools: [webSearchTool(), remember], model: new FakeModel([[WEB_SEARCH, noteIt], [say("Noted.")]]) });
  const slow = new SlowProcessor(50);
  setTraceProcessors([slow, processor]);
  await new Runner().run(agent, "Note what the web says.");
  await slow.drained();
  assert.equal(refused.length, 1, "not guarded, and still held");
  assert.ok(refused[0] instanceof MemoryLocked && refused[0].quarantine === "policy:no-search-into-facts", String(refused[0]));
  const [denied] = wakeEvents(await spans(), "denied");
  assert.deepEqual(payload(denied).origins, ["tool:web_search"]);
});

// Hosted MCP calls the app approves itself: OpenAI runs each inside the model call whose input
// carries the approval, which checkApprovals() checks first, as the run's callModelInputFilter.

const UNFOUND = "Wake couldn't find the request this approves, so this tool call wasn't run.";
const FETCH_TERMS = call("fetch_page", JSON.stringify({ url: "https://acme.example/terms" }), "call_fetch_terms");

/** The answers to hosted MCP tools' requests in the input of each model call, as sent. */
function answers(model: NotingModel): Record<string, unknown>[][] {
  return model.inputs.map((input) =>
    (Array.isArray(input) ? (input as Record<string, unknown>[]) : [])
      .filter((i) => i.type === "hosted_tool_call" && i.name === "mcp_approval_response")
      .map((i) => JSON.parse(JSON.stringify(i.providerData)) as Record<string, unknown>),
  );
}

/**
 * Runs the guarded agent, with checkApprovals() as the run's filter, until a hosted MCP call
 * waits on the app, approves it as the app would, through the run's interruptions, and runs on.
 * A halt given lands while the app decides.
 */
async function approvedByTheApp(
  quarantine: Halts,
  agent: Agent<any, any>,
  { always = false, halt, previousResponseId, own, ...config }: { always?: boolean; halt?: Halt; previousResponseId?: string; own?: CallModelInputFilter; traceIncludeSensitiveData?: boolean } = {},
) {
  const { recorder, spans } = recorded("support");
  const processor = new WakeTracingProcessor(recorder, { quarantine });
  setTraceProcessors([processor]);
  const runner = new Runner({ ...config, callModelInputFilter: processor.checkApprovals(own) });
  const options = previousResponseId !== undefined ? { previousResponseId } : {};
  const asked = await runner.run(processor.guard(agent), "Check order 1234.", options);
  assert.equal(asked.interruptions.length, 1, "the app is asked");
  if (halt !== undefined) quarantine.halts.push(halt);
  asked.state.approve(asked.interruptions[0], { alwaysApprove: always });
  const result = await runner.run(agent, asked.state, options);
  return { result, events: wakeEvents(await spans()), processor };
}

const NO_CRM_AFTER_WEB = { rules: [{ id: "no-crm-after-web", tools: ["lookup_order"], forbid_origins: ["tool:fetch_page"] }] };

test("a hosted MCP call the app approves that the flow policy holds back is turned down before the model call that runs it", async () => {
  const [fetchPage] = webTools([]);
  const name = "oa-app-crm-after-web";
  const model = new NotingModel([[FETCH_BILLING_PAGE], [LOOKUP], [FETCH_TERMS], [say("Couldn't look it up.")]]);
  const agent = new Agent({ name, tools: [fetchPage, crm({ requireApproval: "always" })], model });
  const { result, events } = await approvedByTheApp(serving(NO_CRM_AFTER_WEB), agent);
  assert.equal(result.finalOutput, "Couldn't look it up.");
  const reason = `Wake's flow policy (policy:no-crm-after-web) holds ${name} back after tool:fetch_page, so this tool call wasn't run.`;
  const rejected = { approve: false, approval_request_id: "mcpr_1", reason };
  assert.deepEqual(answers(model), [[], [], [rejected], [rejected]], "turned down in the input sent, and again in each that carries it after");
  const denied = events.filter((e) => attr(e, "wake.event.kind") === "denied");
  assert.equal(denied.length, 1, "recorded once");
  assert.equal(denied[0].name, "execute_tool lookup_order");
  assert.equal(attr(denied[0], "gen_ai.tool.name"), "lookup_order");
  assert.equal(attr(denied[0], "wake.mcp.server"), "crm");
  assert.equal(attr(denied[0], "wake.brake.halt_id"), "policy:no-crm-after-web");
  assert.equal(attr(denied[0], "wake.agent.id"), name);
  assert.deepEqual(origins(denied[0]), []);
  assert.deepEqual([payload(denied[0]).input, payload(denied[0]).output, payload(denied[0]).origins], [JSON.stringify({ order: "1234" }), reason, ["tool:fetch_page"]]);
  const asked = events.filter((e) => attr(e, "wake.event.kind") === "model_call")[1];
  assert.deepEqual(parents(denied[0]), [id(asked)], "the response that asked, before the run resumed");
  assert.ok(!seenBy(name).includes("tool:lookup_order"), "it never ran");
});

test("a hosted MCP call the app approves that a halt covers is turned down", async () => {
  const model = new NotingModel([[LOOKUP], [say("Couldn't look it up.")]]);
  const agent = new Agent({ name: "oa-app-crm-halted", tools: [crm({ requireApproval: "always" })], model });
  const { events } = await approvedByTheApp(new Halts(), agent, { halt: { id: "halt-crm", agents: ["oa-app-crm-halted"] } });
  assert.deepEqual(answers(model).at(-1), [{ approve: false, approval_request_id: "mcpr_1", reason: "Wake halted oa-app-crm-halted (halt-crm), so this tool call wasn't run." }]);
  const [denied] = events.filter((e) => attr(e, "wake.event.kind") === "denied");
  assert.deepEqual([attr(denied, "gen_ai.tool.name"), attr(denied, "wake.brake.halt_id")], ["lookup_order", "halt-crm"]);
});

test("a hosted MCP call the app approves that Wake lets through enters the agent before the model call that runs it", async () => {
  const name = "oa-app-crm-open";
  const seen: string[][] = [];
  class Seeing extends NotingModel {
    override async getResponse(request: ModelRequest): Promise<ModelResponse> {
      seen.push(seenBy(name));
      return super.getResponse(request);
    }
  }
  const model = new Seeing([[LOOKUP], [mcpCall("crm", "lookup_order", "Order 1234: paid in full."), say("Paid in full.")]]);
  const agent = new Agent({ name, tools: [crm({ requireApproval: "always" })], model });
  const { result, events } = await approvedByTheApp(serving(NO_CRM_AFTER_WEB), agent, { traceIncludeSensitiveData: false });
  assert.equal(result.finalOutput, "Paid in full.");
  assert.deepEqual(answers(model), [[], [{ approve: true, approval_request_id: "mcpr_1" }]], "approved, as the app said");
  assert.deepEqual(seen, [[], ["tool:lookup_order"]], "noted before the model call that runs it, the trace or not");
  assert.ok(!kinds(events).includes("denied"));
});

test("each hosted MCP call the app approves for good is checked before the model call that runs it", async () => {
  const name = "oa-app-crm-for-good";
  const second = mcpRequest("crm", "lookup_order", JSON.stringify({ order: "5678" }), "mcpr_2");
  const model = new NotingModel([[LOOKUP], [mcpCall("crm", "lookup_order", "Order 1234: paid."), second], [say("Looked one up.")]]);
  const agent = new Agent({ name, tools: [crm({ requireApproval: "always" })], model });
  const oneLookup = serving({ rules: [{ id: "one-lookup", tools: ["lookup_order"], forbid_origins: ["tool:lookup_order"] }] });
  const { result, events } = await approvedByTheApp(oneLookup, agent, { always: true });
  assert.equal(result.interruptions.length, 0, "the second wasn't asked about: the app approved the tool for good");
  const reason = `Wake's flow policy (policy:one-lookup) holds ${name} back after tool:lookup_order, so this tool call wasn't run.`;
  assert.deepEqual(answers(model).at(-1), [
    { approve: true, approval_request_id: "mcpr_1" },
    { approve: false, approval_request_id: "mcpr_2", reason },
  ], "the first ran, and what it brought back holds the second back");
  const [denied, ...rest] = events.filter((e) => attr(e, "wake.event.kind") === "denied");
  assert.equal(rest.length, 0);
  assert.deepEqual([payload(denied).input, payload(denied).origins], [JSON.stringify({ order: "5678" }), ["tool:lookup_order"]]);
});

test("with the conversation kept at OpenAI, the request is the one the trace showed asked, and one it didn't show is turned down", async () => {
  for (const sensitive of [true, false]) {
    const name = `oa-app-crm-at-openai-${sensitive}`;
    const [fetchPage] = webTools([]);
    const model = new NotingModel([[FETCH_BILLING_PAGE], [LOOKUP], [say("Couldn't look it up.")]]);
    const agent = new Agent({ name, tools: [fetchPage, crm({ requireApproval: "always" })], model });
    const { events } = await approvedByTheApp(serving(NO_CRM_AFTER_WEB), agent, { previousResponseId: "resp_0", traceIncludeSensitiveData: sensitive });
    const input = model.inputs.at(-1) as Record<string, unknown>[];
    assert.deepEqual(input.map((i) => i.name), ["mcp_approval_response"], "the input holds the approval alone");
    const [denied, ...rest] = events.filter((e) => attr(e, "wake.event.kind") === "denied");
    assert.equal(rest.length, 0);
    if (sensitive) {
      const reason = `Wake's flow policy (policy:no-crm-after-web) holds ${name} back after tool:fetch_page, so this tool call wasn't run.`;
      assert.deepEqual(answers(model).at(-1), [{ approve: false, approval_request_id: "mcpr_1", reason }]);
      assert.deepEqual([attr(denied, "gen_ai.tool.name"), attr(denied, "wake.mcp.server")], ["lookup_order", "crm"]);
    } else {
      // The trace left the response out, and the input doesn't hold the request: Wake can't check it.
      assert.deepEqual(answers(model).at(-1), [{ approve: false, approval_request_id: "mcpr_1", reason: UNFOUND }]);
      assert.deepEqual([attr(denied, "gen_ai.tool.name"), attr(denied, "wake.brake.halt_id"), payload(denied).output], ["mcp", UNCHECKED, UNFOUND]);
    }
  }
});

test("an approval whose call the input shows ran, or that guard()'s own callback answered, is left as it is", async () => {
  // Its call ran in an earlier model call, whose response the input holds: nothing more runs.
  const history = [
    { type: "message", role: "user", content: "Check order 1234." },
    { type: "hosted_tool_call", id: "mcpr_1", name: "mcp_approval_request", status: "completed", providerData: { type: "mcp_approval_request", id: "mcpr_1", server_label: "crm", name: "lookup_order", arguments: "{}" } },
    { type: "hosted_tool_call", name: "mcp_approval_response", providerData: { approve: true, approval_request_id: "mcpr_1" } },
    { type: "hosted_tool_call", id: "mcp_1", name: "mcp_call", status: "completed", output: "Paid.", providerData: { type: "mcp_call", id: "mcp_1", server_label: "crm", name: "lookup_order", arguments: "{}", approval_request_id: "mcpr_1" } },
  ];
  const model = new NotingModel([[say("Paid in full.")]]);
  const agent = new Agent({ name: "oa-app-crm-ran", tools: [crm({ requireApproval: "always" })], model });
  const { recorder, spans } = recorded("support");
  const processor = new WakeTracingProcessor(recorder, { quarantine: new Halts({ id: "halt-crm", agents: ["oa-app-crm-ran"] }) });
  setTraceProcessors([processor]);
  await new Runner({ callModelInputFilter: processor.checkApprovals() }).run(processor.guard(agent), history as never);
  assert.deepEqual(answers(model), [[{ approve: true, approval_request_id: "mcpr_1" }]]);
  assert.ok(!kinds(wakeEvents(await spans())).includes("denied"));

  // guard()'s own answer is decided once: what the call it let through brought back doesn't hold it back after.
  const answered = new NotingModel([[LOOKUP], [mcpCall("crm", "lookup_order", "Paid."), say("Paid in full.")]]);
  const own = new Agent({ name: "oa-app-crm-wake-answered", tools: [crm()], model: answered });
  const oneLookup = serving({ rules: [{ id: "one-lookup", tools: ["lookup_order"], forbid_origins: ["tool:lookup_order"] }] });
  const second = recorded("support");
  const guarding = new WakeTracingProcessor(second.recorder, { quarantine: oneLookup });
  setTraceProcessors([guarding]);
  await new Runner({ callModelInputFilter: guarding.checkApprovals() }).run(guarding.guard(own), "Check order 1234.");
  assert.deepEqual(answers(answered).at(-1), [{ approve: true, approval_request_id: "mcpr_1" }]);
  assert.ok(!kinds(wakeEvents(await second.spans())).includes("denied"));
});

test("a call to a hosted MCP server deferred tool search found, which guard() never sees, is checked as the app approves it", async () => {
  const name = "oa-app-docs-found";
  const read = { type: "hosted_tool_call", id: "mcpr_9", name: "mcp_approval_request", status: "completed", providerData: { type: "mcp_approval_request", id: "mcpr_9", server_label: "docs", name: "read_doc", arguments: "{}" } };
  const found = [
    { type: "tool_search_call", id: "ts_1", callId: "call_ts", execution: "server", status: "completed", arguments: { query: "docs" } },
    { type: "tool_search_output", id: "tso_1", callId: "call_ts", execution: "server", status: "completed", tools: [{ type: "mcp", server_label: "docs", server_url: "https://docs.example/mcp", require_approval: "always" }] },
    read,
  ];
  const inputs: unknown[] = [];
  class Noting extends ItemModel {
    override async getResponse(request: ModelRequest): Promise<ModelResponse> {
      inputs.push(request.input);
      return super.getResponse(request);
    }
  }
  const [fetchPage] = webTools([]);
  const agent = new Agent({ name, tools: [fetchPage, toolSearchTool()], model: new Noting([asItems([FETCH_BILLING_PAGE]), found, DONE]) });
  const { events } = await approvedByTheApp(serving({ rules: [{ id: "no-docs-after-web", tools: ["read_doc"], forbid_origins: ["tool:fetch_page"] }] }), agent);
  const sent = (inputs.at(-1) as Record<string, unknown>[]).filter((i) => i.name === "mcp_approval_response").map((i) => JSON.parse(JSON.stringify(i.providerData)));
  const reason = `Wake's flow policy (policy:no-docs-after-web) holds ${name} back after tool:fetch_page, so this tool call wasn't run.`;
  assert.deepEqual(sent, [{ approve: false, approval_request_id: "mcpr_9", reason }]);
  const [denied] = events.filter((e) => attr(e, "wake.event.kind") === "denied");
  assert.deepEqual([attr(denied, "gen_ai.tool.name"), attr(denied, "wake.mcp.server")], ["read_doc", "docs"]);
});

test("given the app's own filter, checkApprovals() checks the input it returns, and never changes the SDK's own items", async () => {
  const own: CallModelInputFilter = Object.assign(async ({ modelData }: { modelData: { input: unknown[]; instructions?: string } }) => ({ ...modelData, instructions: "Be brief." }) as never, {
    preserveInputIdentity: true,
  });
  const name = "oa-app-crm-own-filter";
  const model = new NotingModel([[LOOKUP], [say("Couldn't look it up.")]]);
  const agent = new Agent({ name, tools: [crm({ requireApproval: "always" })], model });
  const { result } = await approvedByTheApp(new Halts(), agent, { own, halt: { id: "halt-crm", agents: [name] } });
  assert.equal(model.instructions.at(-1), "Be brief.", "the app's own filter ran");
  assert.deepEqual(answers(model).at(-1), [{ approve: false, approval_request_id: "mcpr_1", reason: `Wake halted ${name} (halt-crm), so this tool call wasn't run.` }]);
  const kept = result.history.find((i) => i.type === "hosted_tool_call" && i.name === "mcp_approval_response") as { providerData: { approve: boolean } } | undefined;
  assert.equal(kept?.providerData.approve, true, "the run's own item, given to the filters as it is, is left as it was");
});

test("guard() says once for each hosted MCP tool it leaves to the app that checkApprovals() checks the calls the app approves", () => {
  const warned: string[] = [];
  const warn = log.warn;
  log.warn = (message: string) => void warned.push(message);
  try {
    const tool = crm({ requireApproval: "always" });
    const processor = new WakeTracingProcessor(recorded("support").recorder, { quarantine: new Halts() });
    processor.guard(new Agent({ name: "oa-left-one", tools: [tool] }));
    processor.guard(new Agent({ name: "oa-left-two", tools: [tool] }));
    processor.guard(new Agent({ name: "oa-answered", tools: [crm()] }));
    assert.deepEqual(warned, [
      "guard() leaves hosted MCP server crm to the app, which approves its calls through run interruptions: give the run checkApprovals() as its callModelInputFilter, or the calls the app approves run unchecked",
    ]);
  } finally {
    log.warn = warn;
  }
  assert.throws(() => new WakeTracingProcessor(recorded("support").recorder).checkApprovals(), /checkApprovals\(\) needs the brake's rules/);
});
