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

import { Agent, Runner, run, setTraceProcessors, tool, Usage, withGenerationSpan, withResponseSpan, type ModelRequest, type ModelResponse } from "@openai/agents";
import type { ReadableSpan } from "@opentelemetry/sdk-trace";
import { z } from "zod";

import { links, Recorder } from "../src/index.ts";
import { Memory } from "../src/memory.ts";
import { WakeTracingProcessor } from "../src/openai-agents.ts";
import { Quarantine } from "../src/quarantine.ts";
import { UNCHECKED } from "../src/stop.ts";
import { attr, id, ids, origins, parents, recorded, wakeEvents } from "./helpers.ts";
import { asItems, call, FakeModel, say, type ApiItem } from "./openai-fakes.ts";

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

for (const sensitive of [true, false]) {
  test(`an agent called twice as a tool is two instances that never cross (sensitive data ${sensitive ? "on" : "off"})`, async () => {
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
        [call("research", JSON.stringify({ input: "refund window" }), "call_a"), call("research", JSON.stringify({ input: "refund window" }), "call_b")],
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

/** Stands in for the brake's rules (Quarantine): the halts in force. */
class Halts {
  halts: Halt[];
  constructor(...halts: Halt[]) {
    this.halts = halts;
  }
  halted(names: Iterable<string>): string | null {
    const named = new Set(names);
    return this.halts.find((h) => h.fleet || (h.agents ?? []).some((a) => named.has(a)))?.id ?? null;
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
