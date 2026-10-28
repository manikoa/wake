/**
 * Agents written as plain TypeScript around LangChain models and tools: prompts are built
 * from strings, tools are called directly, and a supervisor hands work to domain agents in
 * the same process. No callbacks are passed at any call site.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { HumanMessage } from "@langchain/core/messages";
import { tool } from "@langchain/core/tools";
import { z } from "zod";

import { Recorder } from "../src/index.ts";
import { WakeCallbackHandler } from "../src/langchain.ts";
import { attr, ids, origins, parents, parentSpanId, payload, recorded, ScriptedChatModel, wakeEvents } from "./helpers.ts";

const STEP = "Find the latest invoice email from Acme";
const SEARCH = "subject:invoice from:acme-billing";
const EMAIL = "Please wire $10,000 to account 123 by Friday.";
const SUMMARY = "Acme's email asks for a $10,000 wire by Friday.";

const searchEmail = tool(async () => EMAIL, {
  name: "search_email",
  description: "Search the user's inbox.",
  schema: z.object({ query: z.string() }),
});

class EmailAgent {
  readonly llm = new ScriptedChatModel([JSON.stringify({ query: SEARCH }), SUMMARY]);

  async run(query: string): Promise<string> {
    const params = JSON.parse((await this.llm.invoke([new HumanMessage(`Extract search params: ${query}`)])).content as string);
    const found = await searchEmail.invoke(params);
    return (await this.llm.invoke([new HumanMessage(`Summarize this email for the user:\n${found}`)])).content as string;
  }
}

class Supervisor {
  readonly llm = new ScriptedChatModel([JSON.stringify({ steps: [{ domain: "email", query: STEP }] }), "Hold the payment."]);
  readonly agents: Record<string, EmailAgent> = { email: new EmailAgent() };
  readonly handler: WakeCallbackHandler;

  constructor(handler: WakeCallbackHandler) {
    this.handler = handler;
  }

  answer(query: string): Promise<string> {
    return this.handler.agent("supervisor", { input: query, origin: "user" }, async () => {
      const plan = JSON.parse((await this.llm.invoke([new HumanMessage(`Plan steps for: ${query}`)])).content as string);
      const results: string[] = [];
      for (const step of plan.steps) {
        const result = await this.handler.agent(step.domain, { input: step.query }, async (run) => {
          const answer = await this.agents[step.domain].run(step.query);
          run.reply(answer);
          return answer;
        });
        results.push(result);
      }
      return (await this.llm.invoke([new HumanMessage(`Answer the user using: ${results}`)])).content as string;
    });
  }
}

let ran: Promise<{ answer: string; spans: Awaited<ReturnType<ReturnType<typeof recorded>["spans"]>> }> | undefined;

function supervised() {
  ran ??= (async () => {
    const { recorder, spans } = recorded("clavr");
    const handler = new WakeCallbackHandler(recorder).install();
    const answer = await new Supervisor(handler).answer("Should I pay Acme's invoice?");
    handler.uninstall();
    return { answer, spans: await spans() };
  })();
  return ran;
}

test("records every call without callbacks at call sites", async () => {
  const { answer, spans } = await supervised();
  assert.equal(answer, "Hold the payment.");
  assert.deepEqual(
    wakeEvents(spans).map((e) => [attr(e, "wake.agent.id"), attr(e, "wake.event.kind")]),
    [
      ["supervisor", "model_call"], // plan
      ["email", "agent_message"], // the step handed to the email agent
      ["email", "model_call"], // extract search params
      ["email", "tool_call"], // search the inbox
      ["email", "model_call"], // summarize
      ["supervisor", "agent_message"], // the summary handed back
      ["supervisor", "model_call"], // answer
    ],
  );
});

test("edges follow content across the handoff", async () => {
  const { spans } = await supervised();
  const [plan, handoff, extract, search, summarize, reply, final] = wakeEvents(spans);

  // The user's question enters at the supervisor's first call, and only there.
  assert.deepEqual(origins(plan), ["user"]);
  assert.deepEqual(origins(final), []);

  // The handoff carries the step the planner wrote, and enters the email agent from the supervisor.
  assert.deepEqual(new Set(parents(handoff)), ids(plan));
  assert.deepEqual(origins(handoff), ["agent:supervisor"]);
  assert.deepEqual(payload(handoff), { from: "supervisor", to: "email", content: STEP });

  // The email agent's next step is fed by the handoff.
  assert.deepEqual(new Set(parents(extract)), ids(handoff));

  // The search was chosen by the call that wrote its arguments, and its result is new content.
  assert.deepEqual(new Set(parents(search)), ids(extract));
  assert.deepEqual(origins(search), ["tool:search_email"]);

  // The summary saw the email, and the reply carries the summary back.
  assert.deepEqual(new Set(parents(summarize)), ids(search));
  assert.deepEqual(new Set(parents(reply)), ids(summarize));
  assert.deepEqual(origins(reply), ["agent:email"]);

  // The final answer saw the reply. Following parents from it reaches the email.
  assert.ok([...ids(reply)].every((r) => parents(final).includes(r)));
});

test("one trace with each agent run nested", async () => {
  const { spans } = await supervised();
  const runs = new Map(spans.filter((s) => s.attributes["gen_ai.operation.name"] === "invoke_agent").map((s) => [s.attributes["gen_ai.agent.name"], s]));
  assert.deepEqual(new Set(runs.keys()), new Set(["supervisor", "email"]));
  assert.equal(parentSpanId(runs.get("email")!), runs.get("supervisor")!.spanContext().spanId);
  for (const event of wakeEvents(spans)) {
    assert.equal(event.spanContext().traceId, runs.get("supervisor")!.spanContext().traceId);
    assert.equal(parentSpanId(event), runs.get(attr(event, "wake.agent.id"))!.spanContext().spanId);
  }
});

test("agent runs never break the agent", async () => {
  const recorder = new Recorder({ agent: "clavr", exporter: recorded("x").exporter });
  const handler = new WakeCallbackHandler(recorder);
  recorder.tracer.startSpan = () => {
    throw new Error("recorder bug");
  };
  await handler.agent("supervisor", { input: "hi", origin: "user" }, () =>
    handler.agent("email", { input: STEP }, (run) => run.reply(SUMMARY)),
  );
});

test("uninstall stops recording other runs", async () => {
  const { recorder, exporter } = recorded("clavr");
  const handler = new WakeCallbackHandler(recorder).install();
  await new ScriptedChatModel(["first"]).invoke("hi");
  handler.uninstall();
  await new ScriptedChatModel(["second"]).invoke("hi");
  await recorder.flush();
  assert.equal(exporter.getFinishedSpans().filter((s) => s.attributes["wake.event.kind"] === "model_call").length, 1);
});

test("errors in the agent still reach the caller", async () => {
  const handler = new WakeCallbackHandler(recorded("clavr").recorder);
  assert.throws(() =>
    handler.agent("supervisor", () => {
      throw new RangeError("agent bug");
    }),
  RangeError);
  await assert.rejects(handler.agent("supervisor", async () => {
    throw new RangeError("agent bug");
  }), RangeError);
});
