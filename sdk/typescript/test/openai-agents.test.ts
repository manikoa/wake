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
