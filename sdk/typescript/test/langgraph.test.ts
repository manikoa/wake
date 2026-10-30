import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { test } from "node:test";

import { AIMessage, HumanMessage, type ToolMessage } from "@langchain/core/messages";
import { RunnableLambda } from "@langchain/core/runnables";
import { tool } from "@langchain/core/tools";
import { MessagesAnnotation, START, StateGraph } from "@langchain/langgraph";
import { ToolNode, toolsCondition } from "@langchain/langgraph/prebuilt";
import { SpanStatusCode } from "@opentelemetry/api";
import { z } from "zod";

import { links, Recorder } from "../src/index.ts";
import { AgentHalted, WakeCallbackHandler, WakeToolNode } from "../src/langgraph.ts";
import { Quarantine } from "../src/quarantine.ts";
import { UNCHECKED } from "../src/stop.ts";
import { ANSWER, ask, emailAgentGraph } from "./email-agent.ts";
import { attr, id, ids, origins, parents, parentSpanId, recorded, ScriptedChatModel, wakeEvents } from "./helpers.ts";

function setup() {
  const { recorder, spans } = recorded("email-agent");
  return { handler: new WakeCallbackHandler(recorder), spans, recorder };
}

test("records model and tool calls with causal edges", async () => {
  const { handler, spans } = setup();
  const result = await ask(emailAgentGraph(), handler);
  assert.equal(result.messages.at(-1)?.content, ANSWER);

  const [first, toolCall, second, ...rest] = wakeEvents(await spans());
  assert.equal(rest.length, 0);
  assert.deepEqual([first, toolCall, second].map((e) => attr(e, "wake.event.kind")), ["model_call", "tool_call", "model_call"]);

  // The user's message enters at the first model call.
  assert.deepEqual(origins(first), ["user"]);
  assert.deepEqual(parents(first), []);

  // The tool call was requested by the first model call, and its result is new content.
  assert.deepEqual(parents(toolCall), [id(first)]);
  assert.deepEqual(origins(toolCall), ["tool:read_email"]);
  assert.equal(attr(toolCall, "gen_ai.tool.call.id"), "call_1");

  // The second model call saw both the first call's output and the tool result.
  assert.deepEqual(new Set(parents(second)), ids(first, toolCall));
  assert.deepEqual(origins(second), []);
});

test("events share one trace under the agent run", async () => {
  const { handler, spans } = setup();
  await ask(emailAgentGraph(), handler);
  const all = await spans();
  const roots = all.filter((s) => s.attributes["gen_ai.operation.name"] === "invoke_agent");
  assert.equal(roots.length, 1);
  const [root] = roots;
  for (const event of wakeEvents(all)) {
    assert.equal(event.spanContext().traceId, root.spanContext().traceId);
    assert.equal(parentSpanId(event), root.spanContext().spanId);
    assert.equal(attr(event, "wake.agent.id"), "email-agent");
    assert.equal(attr(event, "wake.capture"), "sdk");
  }
});

test("the payload digest matches and the SDK never signs", async () => {
  const { handler, spans } = setup();
  await ask(emailAgentGraph(), handler);
  const events = wakeEvents(await spans());
  for (const event of events) {
    const payload = attr(event, "wake.payload") as string;
    assert.equal(attr(event, "wake.payload.sha256"), createHash("sha256").update(payload).digest("hex"));
    assert.equal(attr(event, "wake.signature"), undefined);
  }
  assert.match(attr(events[1], "wake.payload") as string, /Please wire \$10,000/);
});

test("edges cross agents sharing a graph", async () => {
  const { handler, spans } = setup();
  const researcher = new ScriptedChatModel(["Vendor is unverified."]);
  const writer = new ScriptedChatModel(["Do not pay yet."]);
  const graph = new StateGraph(MessagesAnnotation)
    .addNode("researcher", async (s) => ({ messages: [await researcher.invoke(s.messages)] }))
    .addNode("writer", async (s) => ({ messages: [await writer.invoke(s.messages)] }))
    .addEdge(START, "researcher")
    .addEdge("researcher", "writer")
    .compile();
  await graph.invoke({ messages: [new HumanMessage("Should we pay this vendor?")] }, { callbacks: [handler] });

  const [researchCall, writeCall] = wakeEvents(await spans());
  assert.equal(attr(researchCall, "wake.langgraph.node"), "researcher");
  assert.equal(attr(writeCall, "wake.langgraph.node"), "writer");
  assert.deepEqual(parents(writeCall), [id(researchCall)]);
  // The user's message was already seen by the researcher, so it doesn't enter again.
  assert.deepEqual(origins(writeCall), []);
});

test("the agent keeps working when the collector is down", async () => {
  const recorder = new Recorder({
    agent: "email-agent",
    exporter: {
      export: () => {
        throw new Error("collector is down");
      },
      shutdown: async () => {},
    },
  });
  const result = await ask(emailAgentGraph(), new WakeCallbackHandler(recorder));
  await recorder.flush();
  assert.equal(result.messages.at(-1)?.content, ANSWER);
});

test("the agent keeps working when recording fails", async () => {
  const { handler, recorder } = setup();
  recorder.tracer.startSpan = () => {
    throw new Error("recorder bug");
  };
  const result = await ask(emailAgentGraph(), handler);
  assert.equal(result.messages.at(-1)?.content, ANSWER);
});

test("a graph run inside a tool descends from the tool call", async () => {
  const { handler, spans } = setup();
  const summarizer = emailAgentGraph();
  const summarize = tool(
    async ({ subject }) => {
      // The tool's own prompt: LangChain passes its callbacks on to the nested run.
      const result = await summarizer.invoke({ messages: [new HumanMessage(`Summarize the ${subject} email`)] });
      return result.messages.at(-1)?.content as string;
    },
    { name: "summarize", description: "Summarize the latest email with this subject.", schema: z.object({ subject: z.string() }) },
  );
  const model = new ScriptedChatModel([
    new AIMessage({ content: "", tool_calls: [{ name: "summarize", args: { subject: "invoice" }, id: "call_s" }] }),
    "It asks for a $10,000 wire.",
  ]);
  const graph = new StateGraph(MessagesAnnotation)
    .addNode("agent", async (state) => ({ messages: [await model.invoke(state.messages)] }))
    .addNode("tools", new ToolNode([summarize]))
    .addEdge(START, "agent")
    .addConditionalEdges("agent", toolsCondition)
    .addEdge("tools", "agent")
    .compile();
  await ask(graph, handler);

  const events = wakeEvents(await spans());
  assert.deepEqual(
    events.map((e) => attr(e, "wake.event.kind")),
    ["model_call", "tool_call", "model_call", "tool_call", "model_call", "model_call"],
  );
  const [outer, summarizeCall, nestedFirst] = events;
  assert.equal(attr(summarizeCall, "gen_ai.tool.name"), "summarize");
  // The nested run's first step descends from the tool call that ran it, and its input is
  // the tool's prompt, not the user's.
  assert.ok(parents(nestedFirst).includes(id(summarizeCall)));
  assert.ok(!origins(nestedFirst).includes("user"));
  assert.deepEqual(origins(outer), ["user"]);
  assert.equal(new Set(events.map((e) => e.spanContext().traceId)).size, 1);
});

test("a user message with no id is new only once in a trace", async () => {
  const { handler, spans } = setup();
  const model = new ScriptedChatModel(["Draft one.", "Draft two.", "Draft three."]);
  const question = new HumanMessage("Write a reply to the invoice email"); // no id, as plain LangChain apps send

  await RunnableLambda.from(async () => {
    const first = await model.invoke([question]);
    return model.invoke([question, first]);
  }).invoke({}, { callbacks: [handler] });
  const [first, second] = wakeEvents(await spans());
  assert.deepEqual(origins(first), ["user"]);
  // Asked again in the same run, it isn't new: the second call descends from the first.
  assert.deepEqual(origins(second), []);
  assert.ok(parents(second).includes(id(first)));

  // A run of its own sees it for the first time.
  await RunnableLambda.from(() => model.invoke([question])).invoke({}, { callbacks: [handler] });
  assert.deepEqual(origins(wakeEvents(await spans()).at(-1)!), ["user"]);
});

test("a cancelled tool call is still recorded, and its link ends", async () => {
  const { handler, spans } = setup();
  let inside: links.Link | undefined;
  const waitForReply = tool(
    async () => {
      inside = links.current();
      return new Promise<string>(() => {}); // never answers
    },
    { name: "wait_for_reply", description: "Wait for a reply to an email.", schema: z.object({ subject: z.string() }) },
  );
  const controller = new AbortController();
  setTimeout(() => controller.abort(), 50);
  await assert.rejects(waitForReply.invoke({ subject: "invoice" }, { callbacks: [handler], signal: controller.signal }));
  assert.notEqual(inside, undefined);
  // Whatever ran in the cancelled call, nothing outside it is under the tool's link.
  assert.equal(links.current(), undefined);
  const [call, ...more] = wakeEvents(await spans());
  assert.equal(more.length, 0);
  assert.equal(attr(call, "wake.event.kind"), "tool_call");
  assert.equal(call.status.code, SpanStatusCode.ERROR);
  assert.match(String(JSON.parse(attr(call, "wake.payload") as string).error), /abort/i);
});

test("a tool call that ends isn't ended again", async () => {
  const { handler, spans } = setup();
  const lookUp = tool(async () => "shipped", { name: "look_up", description: "Look up an order.", schema: z.object({ order: z.string() }) });
  await lookUp.invoke({ order: "1234" }, { callbacks: [handler] });
  const [call, ...more] = wakeEvents(await spans());
  assert.equal(more.length, 0);
  assert.notEqual(call.status.code, SpanStatusCode.ERROR);
  assert.equal(JSON.parse(attr(call, "wake.payload") as string).output, "shipped");
});

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

/** Halts the first run of the email agent it's asked about. */
class HaltOneEmailRun {
  instance: string | undefined;
  halted(names: Iterable<string>): string | null {
    for (const name of [...names].sort()) {
      if (!name.startsWith("email#")) continue;
      this.instance ??= name;
      if (name === this.instance) return "halt-email-run";
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

const wired: string[] = [];

const wire = tool(
  async ({ account }) => {
    wired.push(account);
    return "Wired.";
  },
  { name: "wire", description: "Wire a refund to an account.", schema: z.object({ account: z.string() }) },
);

function wiringGraph(handler: WakeCallbackHandler) {
  const model = new ScriptedChatModel([new AIMessage({ content: "", tool_calls: [{ name: "wire", args: { account: "123" }, id: "call_wire" }] }), "Done."]);
  return new StateGraph(MessagesAnnotation)
    .addNode("agent", async (state) => ({ messages: [await model.invoke(state.messages)] }))
    .addNode("tools", new WakeToolNode(handler, [wire]))
    .addEdge(START, "agent")
    .addConditionalEdges("agent", toolsCondition)
    .addEdge("tools", "agent")
    .compile();
}

function guarded(quarantine: { halted(names: Iterable<string>): string | null }) {
  const { recorder, spans } = recorded("clavr");
  return { handler: new WakeCallbackHandler(recorder, { quarantine }), events: async () => wakeEvents(await spans()) };
}

function wireIt(handler: WakeCallbackHandler) {
  return wiringGraph(handler).invoke({ messages: [new HumanMessage("Wire the refund")] }, { callbacks: [handler] });
}

const kinds = (found: { attributes: Record<string, unknown> }[]) => found.map((e) => e.attributes["wake.event.kind"]);

test("a halted agent's tool call is refused before it runs", async () => {
  wired.length = 0;
  const { handler, events } = guarded(new Halts({ id: "halt-clavr", agents: ["clavr"] }));
  const result = await wireIt(handler);
  assert.deepEqual(wired, [], "the tool never ran");
  const refusal = result.messages.at(-2) as ToolMessage;
  assert.equal(refusal.content, "Wake halted clavr (halt-clavr), so this tool call wasn't run.");
  assert.equal(refusal.status, "error");
  const [first, denied, second, ...rest] = await events();
  assert.equal(rest.length, 0);
  assert.deepEqual(kinds([first, denied, second]), ["model_call", "denied", "model_call"]);
  assert.equal(attr(denied, "wake.brake.halt_id"), "halt-clavr");
  assert.equal(attr(denied, "gen_ai.tool.name"), "wire");
  assert.equal(attr(denied, "wake.agent.id"), "clavr");
  assert.deepEqual(origins(denied), [], "the tool never ran: nothing of it entered");
  assert.deepEqual(JSON.parse(attr(denied, "wake.payload") as string).input, { account: "123" });
  assert.deepEqual(parents(denied), [id(first)], "the call that asked for it");
  assert.ok(parents(second).includes(id(denied)), "the model read the refusal");
  assert.equal(denied.spanContext().traceId, first.spanContext().traceId);
  // Without a halt the same graph runs its tool.
  const running = guarded(new Halts());
  await wireIt(running.handler);
  assert.deepEqual(wired, ["123"]);
  assert.deepEqual(kinds(await running.events()), ["model_call", "tool_call", "model_call"]);
});

test("a halted run is stopped in process, and its sibling goes on", async () => {
  wired.length = 0;
  const halts = new HaltOneEmailRun();
  const { handler, events } = guarded(halts);
  await handler.agent("supervisor", { input: "Refund both orders", origin: "user" }, async () => {
    for (const order of ["1234", "5678"]) {
      await handler.agent("email", { input: `Refund order ${order}` }, () => wireIt(handler));
    }
  });
  assert.equal(wired.length, 1, "only the sibling's tool ran");
  const denied = (await events()).filter((e) => attr(e, "wake.event.kind") === "denied");
  assert.equal(denied.length, 1);
  assert.equal(attr(denied[0], "wake.brake.halt_id"), "halt-email-run");
  assert.equal(attr(denied[0], "wake.agent.instance"), halts.instance, "recorded as the halted run's attempt");
  assert.deepEqual(attr(denied[0], "wake.agent.lineage"), ["supervisor"]);
});

test("a run a halt covers is never started", async () => {
  const halts = new Halts({ id: "halt-email", agents: ["email"] });
  const { handler, events } = guarded(halts);
  const started: string[] = [];
  let refused: unknown;
  handler.agent("supervisor", { input: "Refund order 1234", origin: "user" }, () => {
    try {
      handler.agent("email", { input: "Refund order 1234 to account 123, please" }, () => started.push("email"));
    } catch (error) {
      refused = error;
    }
    // A halt on what starts a run stops the runs it starts too.
    halts.halts = [{ id: "halt-supervisor", agents: ["supervisor"] }];
    assert.throws(() => handler.agent("billing", { input: "Bill it" }, () => started.push("billing")), AgentHalted);
  });
  assert.deepEqual(started, [], "nothing started");
  assert.ok(refused instanceof AgentHalted);
  assert.equal(refused.halt, "halt-email");
  assert.equal(refused.message, "Wake halted email (halt-email), so this run of email wasn't started.");
  const [first, second, ...rest] = await events();
  assert.equal(rest.length, 0);
  assert.deepEqual(kinds([first, second]), ["denied", "denied"]);
  assert.equal(first.name, "agent_message supervisor -> email");
  assert.equal(attr(first, "wake.agent.id"), "supervisor", "recorded as what tried to start it");
  assert.equal(attr(first, "wake.brake.receiver"), "email");
  assert.deepEqual(origins(first), [], "nothing entered the run");
  assert.equal(JSON.parse(attr(first, "wake.payload") as string).content, "Refund order 1234 to account 123, please");
  assert.equal(attr(second, "wake.brake.halt_id"), "halt-supervisor");
});

test("stale rules refuse every run and tool call", async () => {
  wired.length = 0;
  const { handler, events } = guarded(new Quarantine("http://127.0.0.1:9", { agent: "clavr", start: false }));
  assert.throws(
    () => handler.agent("supervisor", () => undefined),
    (error: unknown) => error instanceof AgentHalted && error.halt === "stale-rules",
  );
  await wireIt(handler);
  assert.deepEqual(wired, []);
  const denied = (await events()).filter((e) => attr(e, "wake.event.kind") === "denied");
  assert.deepEqual(new Set(denied.map((e) => attr(e, "wake.brake.halt_id"))), new Set(["stale-rules"]));
});

test("a halt check that fails refuses the call", async () => {
  wired.length = 0;
  const { handler, events } = guarded(brokenHalts);
  const result = await wireIt(handler);
  assert.deepEqual(wired, []);
  assert.equal((result.messages.at(-2) as ToolMessage).content, "Wake couldn't check its halts, so this tool call wasn't run.");
  const denied = (await events()).filter((e) => attr(e, "wake.event.kind") === "denied");
  assert.deepEqual(denied.map((e) => attr(e, "wake.brake.halt_id")), [UNCHECKED]);
});

/** A scripted model an agent can bind tools to. */
class ToolCallingModel extends ScriptedChatModel {
  override bindTools(): this {
    return this;
  }
}

function wiringModel() {
  return new ToolCallingModel([new AIMessage({ content: "", tool_calls: [{ name: "wire", args: { account: "123" }, id: "call_wire" }] }), "Done."]);
}

const REFUSAL = "Wake halted clavr (halt-clavr), so this tool call wasn't run.";

/** The one denied event a refused call is recorded as, checked. */
function refusedOnce(found: Awaited<ReturnType<ReturnType<typeof guarded>["events"]>>, halt = "halt-clavr") {
  const denied = found.filter((e) => attr(e, "wake.event.kind") === "denied");
  assert.equal(denied.length, 1, "recorded once");
  assert.ok(!found.some((e) => attr(e, "wake.event.kind") === "tool_call"), "the tool never started");
  assert.equal(attr(denied[0], "wake.brake.halt_id"), halt);
  assert.equal(attr(denied[0], "gen_ai.tool.name"), "wire");
  assert.deepEqual(origins(denied[0]), []);
  assert.deepEqual(JSON.parse(attr(denied[0], "wake.payload") as string).input, { account: "123" });
  return denied[0];
}

test("a tool call outside a WakeToolNode is refused as it starts", async () => {
  // createReactAgent builds its own ToolNode: the handler refuses the call as the tool starts,
  // and the ToolNode answers the model with the refusal, as it does any error by default.
  const { createReactAgent } = await import("@langchain/langgraph/prebuilt");
  wired.length = 0;
  const { handler, events } = guarded(new Halts({ id: "halt-clavr", agents: ["clavr"] }));
  const graph = createReactAgent({ llm: wiringModel(), tools: [wire] });
  const result = await graph.invoke({ messages: [new HumanMessage("Wire the refund")] }, { callbacks: [handler] });
  assert.deepEqual(wired, [], "the tool never ran");
  const refusal = result.messages.at(-2) as ToolMessage;
  assert.equal(refusal.status, "error");
  assert.ok(String(refusal.content).includes(REFUSAL), String(refusal.content));
  const found = await events();
  const denied = refusedOnce(found);
  const [first, second] = found.filter((e) => attr(e, "wake.event.kind") === "model_call");
  assert.deepEqual(parents(denied), [id(first)], "the call that asked for it");
  assert.ok(parents(second).includes(id(denied)), "the model read the refusal");
});

test("a ToolNode that passes errors on ends the run with the refusal", async () => {
  wired.length = 0;
  const { handler, events } = guarded(new Halts({ id: "halt-clavr", agents: ["clavr"] }));
  const model = wiringModel();
  const graph = new StateGraph(MessagesAnnotation)
    .addNode("agent", async (state) => ({ messages: [await model.invoke(state.messages)] }))
    .addNode("tools", new ToolNode([wire], { handleToolErrors: false }))
    .addEdge(START, "agent")
    .addConditionalEdges("agent", toolsCondition)
    .addEdge("tools", "agent")
    .compile();
  await assert.rejects(
    graph.invoke({ messages: [new HumanMessage("Wire the refund")] }, { callbacks: [handler] }),
    (error: unknown) => error instanceof AgentHalted && error.message === REFUSAL,
  );
  assert.deepEqual(wired, []);
  refusedOnce(await events());
});

test("a tool invoked directly is refused as it starts", async () => {
  wired.length = 0;
  const halts = new Halts();
  const { handler, events } = guarded(halts);
  await handler.agent("email", async () => {
    halts.halts = [{ id: "halt-email", agents: ["email"] }]; // lands once the run has started
    await assert.rejects(wire.invoke({ account: "123" }, { callbacks: [handler] }), AgentHalted);
  });
  assert.deepEqual(wired, []);
  assert.equal(attr(refusedOnce(await events(), "halt-email"), "wake.agent.id"), "email");
});

test("a call a WakeToolNode refuses is recorded once", async () => {
  // The node refuses it, so the tool never starts and the handler's own check never sees it.
  wired.length = 0;
  const { handler, events } = guarded(new Halts({ id: "halt-clavr", agents: ["clavr"] }));
  await wireIt(handler);
  assert.deepEqual(wired, []);
  refusedOnce(await events());
});
