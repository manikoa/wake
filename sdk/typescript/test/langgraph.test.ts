import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { test } from "node:test";

import { AIMessage, HumanMessage } from "@langchain/core/messages";
import { RunnableLambda } from "@langchain/core/runnables";
import { tool } from "@langchain/core/tools";
import { MessagesAnnotation, START, StateGraph } from "@langchain/langgraph";
import { ToolNode, toolsCondition } from "@langchain/langgraph/prebuilt";
import { SpanStatusCode } from "@opentelemetry/api";
import { z } from "zod";

import { links, Recorder } from "../src/index.ts";
import { WakeCallbackHandler } from "../src/langgraph.ts";
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
