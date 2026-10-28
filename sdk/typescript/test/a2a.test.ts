/**
 * Messages between agents: a run an agent starts for a message descends from what sent it,
 * over the @a2a-js/sdk's own client and server, through the proxy or without it.
 */

import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import type { Server } from "node:http";
import { after, test } from "node:test";

import { AIMessage } from "@langchain/core/messages";
import { RunnableLambda } from "@langchain/core/runnables";
import { tool } from "@langchain/core/tools";
import { MessagesAnnotation, START, StateGraph } from "@langchain/langgraph";
import { ToolNode, toolsCondition } from "@langchain/langgraph/prebuilt";
import { Role, type AgentCard, type Message } from "@a2a-js/sdk";
import { Agent, run, setTraceProcessors } from "@openai/agents";
import { ClientFactory, JsonRpcTransportFactory } from "@a2a-js/sdk/client";
import { AgentEvent, DefaultRequestHandler, InMemoryTaskStore, type AgentExecutor, type ExecutionEventBus, type RequestContext } from "@a2a-js/sdk/server";
import { jsonRpcHandler, UserBuilder } from "@a2a-js/sdk/server/express";
import express from "express";
import { z } from "zod";

import { links } from "../src/index.ts";
import * as a2a from "../src/a2a.ts";
import { WakeCallbackHandler } from "../src/langchain.ts";
import { WakeTracingProcessor } from "../src/openai-agents.ts";
import { attr, id, origins, parents, parentSpanId, recorded, ScriptedChatModel, wakeEvents } from "./helpers.ts";
import { FakeModel, say } from "./openai-fakes.ts";

const TRACE = "4bf92f3577b34da6a3ce929d0e0e4736";
export const SENDER = new links.Link("sender-tool-call", TRACE, "00f067aa0ba902b7");
const PROXY_HEADERS = { traceparent: `00-${TRACE}-1111111111111111-01`, "wake-parents": "proxy-message" };

test("received prefers the proxy's headers", () => {
  const metadata = SENDER.meta();
  links.received({ headers: { Traceparent: PROXY_HEADERS.traceparent, "Wake-Parents": "proxy-message" }, metadata }, (link) => {
    assert.equal(links.current(), link);
    assert.deepEqual(link?.parents, ["proxy-message"]);
    assert.deepEqual([link?.traceId, link?.spanId], [TRACE, "1111111111111111"]);
  });
  assert.equal(links.current(), undefined);
  links.received({ metadata }, (link) => assert.deepEqual(link, SENDER));
  // A fetch Headers, as a server built on fetch hands them over.
  links.received({ headers: new Headers(PROXY_HEADERS) }, (link) => assert.deepEqual(link?.parents, ["proxy-message"]));
});

test("received keeps only what looks like a link", () => {
  links.received({ headers: { "wake-parents": "a, b,a,  ,bad id!,c" } }, (link) => {
    assert.deepEqual(link?.parents, ["a", "b", "c"]);
    assert.ok(!link?.hasTrace, "no traceparent, no trace");
    assert.deepEqual(link?.headers(), { "wake-parents": "a,b,c" });
  });
  links.received({ headers: { traceparent: "garbage", "wake-parents": "x" } }, (link) => assert.ok(!link?.hasTrace));
  const good = PROXY_HEADERS.traceparent;
  for (const [traceparent, joins] of [
    [good, true],
    [good.toUpperCase(), false],
    [`${good}-extra`, false],
    [`ff${good.slice(2)}`, false],
    [`01${good.slice(2)}-future`, true],
  ] as const) {
    links.received({ headers: { traceparent, "wake-parents": "x" } }, (link) => assert.equal(link?.traceId === TRACE, joins, traceparent));
  }
  links.received({ headers: { traceparent: good } }, (link) => assert.equal(link, undefined, "a trace alone names no event"));
  links.received({ headers: { "wake-parents": Array.from({ length: 500 }, (_, i) => `e${i}`).join(",") } }, (link) => assert.equal(link?.parents.length, 64));
  links.received({ metadata: { "dev.wake/parents": "not-a-list-but-a-string" } }, (link) => assert.deepEqual(link?.parents, ["not-a-list-but-a-string"]));
  links.received({ headers: null, metadata: { "dev.wake/parents": [1, null] } }, (link) => assert.equal(link, undefined));
});

export function textMessage(text: string, role: Role, contextId = ""): Message {
  return {
    messageId: randomUUID(),
    contextId,
    taskId: "",
    role,
    parts: [{ content: { $case: "text", value: text } }],
    metadata: undefined,
    extensions: [],
    referenceTaskIds: [],
  } as unknown as Message;
}

export function textOf(message: Message | undefined): string {
  return (message?.parts ?? []).map((p) => (p.content?.$case === "text" ? p.content.value : "")).join("\n");
}

/** Answers with a LangChain model call, recorded by its own Wake handler. */
class Researcher implements AgentExecutor {
  readonly seen: (links.Link | undefined)[] = [];
  readonly handler: WakeCallbackHandler;

  constructor(handler: WakeCallbackHandler) {
    this.handler = handler;
  }

  async execute(context: RequestContext, bus: ExecutionEventBus): Promise<void> {
    const reply = await a2a.received(context, async (link) => {
      this.seen.push(link);
      const model = new ScriptedChatModel(["Refunds within 30 days."]);
      const answer = await model.invoke(textOf(context.userMessage), { callbacks: [this.handler] });
      // Linked here, the reply names the step that produced it.
      return a2a.link(textMessage(answer.content as string, Role.ROLE_AGENT, context.contextId));
    });
    bus.publish(AgentEvent.message(reply));
    bus.finished();
  }

  async cancelTask(): Promise<void> {}
}

function card(url: string): AgentCard {
  return {
    name: "researcher",
    description: "Answers questions about shop policy.",
    version: "0.1.0",
    supportedInterfaces: [{ url, protocolBinding: "JSONRPC", protocolVersion: "1.0", tenant: "" }],
    capabilities: { streaming: false, pushNotifications: false, extensions: [] },
    defaultInputModes: ["text/plain"],
    defaultOutputModes: ["text/plain"],
    skills: [],
    securitySchemes: {},
    securityRequirements: [],
    signatures: [],
  } as unknown as AgentCard;
}

const servers: Server[] = [];
after(() => {
  for (const server of servers) {
    server.closeAllConnections();
    server.close();
  }
});

/** Serves the researcher over A2A JSON-RPC; through the proxy, replies carry its record's id. */
async function serve(researcher: Researcher, throughProxy = false): Promise<AgentCard> {
  const app = express();
  if (throughProxy) {
    app.use((_request, response, next) => {
      response.setHeader("wake-parents", "proxy-reply");
      next();
    });
  }
  const placeholder = card("http://127.0.0.1/");
  const handler = new DefaultRequestHandler(placeholder, new InMemoryTaskStore(), researcher);
  app.use("/a2a", jsonRpcHandler({ requestHandler: handler, userBuilder: UserBuilder.noAuthentication }));
  const server = await new Promise<Server>((resolve) => {
    const s = app.listen(0, "127.0.0.1", () => resolve(s));
  });
  servers.push(server);
  const url = `http://127.0.0.1:${(server.address() as { port: number }).port}/a2a`;
  (placeholder.supportedInterfaces[0] as { url: string }).url = url;
  return placeholder;
}

function client(agentCard: AgentCard, base: typeof fetch = fetch) {
  return new ClientFactory({ transports: [new JsonRpcTransportFactory({ fetchImpl: a2a.fetch(base) })] }).createFromAgentCard(agentCard);
}

/** The support agent asks the researcher, from inside a recorded tool call. */
async function exchange(proxyHeaders?: Record<string, string>) {
  const { recorder, spans } = recorded("researcher");
  const researcher = new Researcher(new WakeCallbackHandler(recorder));
  const agentCard = await serve(researcher);
  const sent: { headers: Headers; body: Record<string, unknown> }[] = [];
  // What reaches the receiver; through the proxy, its link replaces the sender's.
  const capture: typeof fetch = async (input, init) => {
    const headers = new Headers(init?.headers);
    for (const [name, value] of Object.entries(proxyHeaders ?? {})) headers.set(name, value);
    sent.push({ headers, body: JSON.parse(String(init?.body)) });
    return fetch(input, { ...init, headers });
  };
  const a2aClient = await client(agentCard, capture);
  const reply = await links._run(links._frame(SENDER), () =>
    a2aClient.sendMessage({ message: a2a.link(textMessage("What is our refund window?", Role.ROLE_USER)) } as never),
  );
  return { reply: reply as Message, seen: researcher.seen, sent, spans: await spans() };
}

test("a message without the proxy links to the sender", async () => {
  const { reply, seen, sent, spans } = await exchange();
  assert.equal(textOf(reply), "Refunds within 30 days.");
  assert.equal(sent.length, 1);
  const [request] = sent;
  assert.equal(request.headers.get("wake-parents"), "sender-tool-call");
  assert.equal(request.headers.get("traceparent"), SENDER.traceparent);
  assert.equal(request.body.method, "SendMessage");
  assert.deepEqual((request.body.params as { message: { metadata: unknown } }).message.metadata, SENDER.meta());
  assert.deepEqual(seen, [SENDER]);
  const [call, ...more] = wakeEvents(spans, "model_call");
  assert.equal(more.length, 0);
  assert.deepEqual(parents(call), ["sender-tool-call"]);
  assert.deepEqual(origins(call), [], "the content came from the sender, not a user");
  assert.equal(call.spanContext().traceId, TRACE);
});

test("a message through the proxy links to its record", async () => {
  // The proxy replaces the link headers with its own record of the message.
  const { seen, spans } = await exchange(PROXY_HEADERS);
  assert.deepEqual(seen.map((l) => l?.parents), [["proxy-message"]]);
  const [call] = wakeEvents(spans, "model_call");
  assert.deepEqual(parents(call), ["proxy-message"]);
  // Under the proxy's message span, by way of the run's span.
  assert.equal(call.spanContext().traceId, TRACE);
  const run = spans.find((s) => s.attributes["gen_ai.operation.name"] === "invoke_agent");
  assert.equal(parentSpanId(run ?? call), "1111111111111111");
});

test("link leaves a message alone without a current event", () => {
  const message = a2a.link(textMessage("hi", Role.ROLE_USER));
  assert.equal(message.metadata, undefined);
  assert.deepEqual(a2a.link({ parts: [] }), { parts: [] });
  links.received({ metadata: SENDER.meta() }, () => {
    assert.deepEqual(a2a.link({ parts: [] } as { parts: never[]; metadata?: unknown }).metadata, SENDER.meta());
  });
});

test("a chain under a link descends from it once", async () => {
  const { recorder, spans } = recorded("researcher");
  const handler = new WakeCallbackHandler(recorder);
  const model = new ScriptedChatModel(["Draft answer about refunds.", "Final answer about refunds."]);
  const chain = model.pipe(RunnableLambda.from((m: AIMessage) => `Improve this: ${m.content}`)).pipe(model);
  await links.received({ metadata: SENDER.meta() }, () => chain.invoke("What is our refund window?", { callbacks: [handler] }));
  const all = await spans();
  const [first, second] = wakeEvents(all, "model_call");
  assert.deepEqual(parents(first), ["sender-tool-call"]);
  assert.ok(!parents(second).includes("sender-tool-call"));
  assert.deepEqual(new Set(all.map((s) => s.spanContext().traceId)), new Set([TRACE]));
});

test("an OpenAI Agents workflow under a link descends from it", async () => {
  const { recorder, spans } = recorded("researcher");
  setTraceProcessors([new WakeTracingProcessor(recorder)]);
  try {
    const agent = new Agent({ name: "researcher", model: new FakeModel([[say("Refunds within 30 days.")]]) });
    await links.received({ metadata: SENDER.meta() }, () => run(agent, "What is our refund window?"));
  } finally {
    setTraceProcessors([]);
  }
  const all = await spans();
  const [call, ...more] = wakeEvents(all, "model_call");
  assert.equal(more.length, 0);
  assert.deepEqual(parents(call), ["sender-tool-call"]);
  assert.deepEqual(origins(call), []);
  assert.deepEqual(new Set(all.map((s) => s.spanContext().traceId)), new Set([TRACE]));
});

/** A LangGraph support agent asks the researcher from a tool, then answers. */
async function askFromAGraph(throughProxy: boolean) {
  const research = recorded("researcher");
  const support = recorded("support");
  const agentCard = await serve(new Researcher(new WakeCallbackHandler(research.recorder)), throughProxy);
  const askResearch = tool(
    async ({ question }) => {
      const reply = await (await client(agentCard)).sendMessage({ message: a2a.link(textMessage(question, Role.ROLE_USER)) } as never);
      a2a.replied(reply);
      return textOf(reply as Message);
    },
    { name: "ask_research", description: "Ask the research agent.", schema: z.object({ question: z.string() }) },
  );
  const model = new ScriptedChatModel([
    new AIMessage({ content: "", tool_calls: [{ name: "ask_research", args: { question: "Refund window?" }, id: "c1" }] }),
    "We can refund within 30 days.",
  ]);
  const graph = new StateGraph(MessagesAnnotation)
    .addNode("model", async (state) => ({ messages: [await model.invoke(state.messages)] }))
    .addNode("tools", new ToolNode([askResearch]))
    .addEdge(START, "model")
    .addConditionalEdges("model", toolsCondition)
    .addEdge("tools", "model")
    .compile();
  await graph.invoke({ messages: [{ role: "user", content: "Can we refund order 1234?" }] }, { callbacks: [new WakeCallbackHandler(support.recorder)] });
  return [...(await research.spans()), ...(await support.spans())];
}

function eventsOf(spans: Awaited<ReturnType<typeof askFromAGraph>>, agent: string, kind: string) {
  return wakeEvents(spans, kind).filter((s) => attr(s, "wake.agent.id") === agent);
}

test("the reply through the proxy feeds the sender's next step", async () => {
  const spans = await askFromAGraph(true);
  const [ask] = eventsOf(spans, "support", "tool_call");
  const [, second] = eventsOf(spans, "support", "model_call");
  assert.ok(parents(second).includes(id(ask)) && parents(second).includes("proxy-reply"));
  assert.ok(!parents(ask).includes("proxy-reply"), "the tool call can't descend from its own reply");
});

test("the reply without the proxy names the receiver's last step", async () => {
  const spans = await askFromAGraph(false);
  const [answer] = eventsOf(spans, "researcher", "model_call");
  const [ask] = eventsOf(spans, "support", "tool_call");
  assert.deepEqual(parents(answer), [id(ask)], "the receiver descends from the tool call");
  const [, second] = eventsOf(spans, "support", "model_call");
  assert.deepEqual(parents(second).slice(-2), [id(ask), id(answer)]);
});
