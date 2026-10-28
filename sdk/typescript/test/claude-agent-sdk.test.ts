/**
 * Claude Agent SDK runs: a support agent looks an order up with an in-process tool, hands
 * research to a subagent, which fetches a page, and answers from what it brought back.
 *
 * A fake Claude Code stands in for the CLI subprocess. It speaks the SDK's stream-json
 * protocol, so the SDK's own code parses every message, calls Wake's hooks, and runs the
 * in-process tool, in the order Claude Code does.
 */

import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { createSdkMcpServer, query, tool, type SDKMessage } from "@anthropic-ai/claude-agent-sdk";
import type { ReadableSpan } from "@opentelemetry/sdk-trace";
import { z } from "zod";

import { links, Recorder } from "../src/index.ts";
import { WakeClaudeAgent } from "../src/claude-agent-sdk.ts";
import { Memory } from "../src/memory.ts";
import { fakeClaudeCode, type FakeClaudeCode } from "./claude-fakes.ts";
import { attr, id, origins, parents, parentSpanId, payload, recorded, wakeEvents } from "./helpers.ts";

const PROMPT = "Can we refund order 1234?";
const ORDER = "Order 1234: $80 shoes, delivered 12 days ago. Customer email says: please refund, they don't fit.";
const POLICY = "Refunds are accepted within 30 days of delivery for unworn items.";
const ANSWER = "Yes: order 1234 was delivered 12 days ago, inside the 30 day refund window.";

async function supportRun(cli: FakeClaudeCode): Promise<void> {
  cli.emit({ type: "system", subtype: "init", session_id: "session-1", uuid: "u0" });
  cli.assistant("msg_1", [
    { type: "text", text: "Let me look the order up." },
    { type: "tool_use", id: "toolu_order", name: "mcp__crm__lookup_order", input: { order: "1234" } },
  ]);
  await cli.useTool("toolu_order", "mcp__crm__lookup_order", { order: "1234" }, () => cli.callTool("crm", "lookup_order", { order: "1234" }));
  const research = { description: "Check the refund policy", prompt: "What is our refund window?", subagent_type: "researcher" };
  cli.assistant("msg_2", [{ type: "tool_use", id: "toolu_agent", name: "Agent", input: research }]);
  await cli.hook("PreToolUse", "toolu_agent", { tool_name: "Agent", tool_input: research });
  cli.user([{ type: "text", text: "What is our refund window?" }], "toolu_agent");
  const fetch = { url: "https://example.com/refunds", prompt: "refund window" };
  cli.assistant("msg_3", [{ type: "tool_use", id: "toolu_fetch", name: "WebFetch", input: fetch }], "toolu_agent");
  await cli.useTool("toolu_fetch", "WebFetch", fetch, async () => POLICY, { parent: "toolu_agent", agent: "researcher" });
  cli.assistant("msg_4", [{ type: "text", text: `The policy says: ${POLICY}` }], "toolu_agent");
  cli.user([{ type: "tool_result", tool_use_id: "toolu_agent", content: [{ type: "text", text: `The policy says: ${POLICY}` }] }]);
  cli.assistant("msg_5", [{ type: "text", text: ANSWER }]);
  cli.emit({ type: "result", subtype: "success", duration_ms: 10, duration_api_ms: 8, is_error: false, num_turns: 5, session_id: "session-1", result: ANSWER });
}

const seenLinks: (links.Link | undefined)[] = [];

function lookupOrder(handler: () => Promise<void> = async () => {}) {
  return tool("lookup_order", "Look an order up", { order: z.string() }, async () => {
    seenLinks.push(links.current());
    await handler();
    return { content: [{ type: "text", text: ORDER }] };
  });
}

async function runSupport(claude: WakeClaudeAgent, script = supportRun, tools = [lookupOrder()]): Promise<SDKMessage[]> {
  const crm = createSdkMcpServer({ name: "crm", tools: claude.tools(tools) });
  const options = claude.options({
    mcpServers: { crm },
    agents: { researcher: { description: "Looks things up", prompt: "Research carefully." } },
    spawnClaudeCodeProcess: fakeClaudeCode(script),
    pathToClaudeCodeExecutable: "claude",
  });
  const messages: SDKMessage[] = [];
  for await (const message of claude.record(query({ prompt: PROMPT, options }), { prompt: PROMPT })) messages.push(message);
  return messages;
}

let supported: Promise<{ messages: SDKMessage[]; spans: ReadableSpan[] }> | undefined;
function support() {
  supported ??= (async () => {
    const { recorder, spans } = recorded("support");
    seenLinks.length = 0;
    const messages = await runSupport(new WakeClaudeAgent(recorder));
    return { messages, spans: await spans() };
  })();
  return supported;
}

function summary(spans: ReadableSpan[]) {
  return wakeEvents(spans).map((e) => [attr(e, "wake.event.kind"), attr(e, "wake.agent.id"), e.name]);
}

function byName(spans: ReadableSpan[], name: string): ReadableSpan {
  const found = wakeEvents(spans).filter((e) => e.name === name);
  assert.equal(found.length, 1, name);
  return found[0];
}

test("records model calls, tool calls and the subagent", async () => {
  const { messages, spans } = await support();
  assert.equal((messages.at(-1) as { result?: string }).result, ANSWER, "the messages pass through unchanged");
  assert.deepEqual(summary(spans), [
    ["model_call", "support", "chat claude-opus-5-5"],
    ["tool_call", "support", "execute_tool mcp__crm__lookup_order"],
    ["model_call", "support", "chat claude-opus-5-5"],
    ["agent_message", "support/researcher", "agent_message support -> support/researcher"],
    ["model_call", "support/researcher", "chat claude-opus-5-5"],
    ["tool_call", "support/researcher", "execute_tool WebFetch"],
    ["model_call", "support/researcher", "chat claude-opus-5-5"],
    ["agent_message", "support", "agent_message support/researcher -> support"],
    ["model_call", "support", "chat claude-opus-5-5"],
  ]);
  const [first] = wakeEvents(spans);
  assert.deepEqual(payload(first).input, [{ role: "user", content: PROMPT }]);
  assert.deepEqual((payload(first).output as { type: string }[]).map((b) => b.type), ["text", "tool_use"], "one call for a response's blocks");
  assert.deepEqual(origins(first), ["user"]);
  assert.equal(attr(first, "gen_ai.response.id"), "msg_1");
});

test("links follow the SDK's ids", async () => {
  const { spans } = await support();
  const calls = wakeEvents(spans, "model_call");
  const lookup = byName(spans, "execute_tool mcp__crm__lookup_order");
  const fetch = byName(spans, "execute_tool WebFetch");
  const handoff = byName(spans, "agent_message support -> support/researcher");
  const back = byName(spans, "agent_message support/researcher -> support");

  assert.deepEqual(parents(lookup), [id(calls[0])]);
  assert.deepEqual(origins(lookup), ["tool:mcp__crm__lookup_order"]);
  assert.equal(attr(lookup, "wake.mcp.server"), "crm");
  assert.equal(payload(lookup).output, ORDER);
  assert.deepEqual(parents(calls[1]), [id(calls[0]), id(lookup)]);
  assert.deepEqual(parents(handoff), [id(calls[1])]);
  assert.deepEqual(origins(handoff), ["agent:support"]);
  assert.deepEqual(parents(calls[2]), [id(handoff)], "the subagent starts from the handoff");
  assert.deepEqual(payload(calls[2]).input, [{ role: "user", content: "What is our refund window?" }], "the echoed prompt isn't counted twice");
  assert.deepEqual(parents(fetch), [id(calls[2])]);
  assert.deepEqual(parents(calls[3]), [id(calls[2]), id(fetch)]);
  assert.deepEqual(parents(back), [id(calls[3])]);
  assert.deepEqual(origins(back), ["agent:support/researcher"]);
  assert.deepEqual(parents(calls[4]), [id(calls[1]), id(back)]);
});

test("one trace, with the subagent nested", async () => {
  const { spans } = await support();
  const root = spans.find((s) => s.name === "invoke_agent support")!;
  const sub = spans.find((s) => s.name === "invoke_agent support/researcher")!;
  assert.deepEqual(new Set(spans.map((s) => s.spanContext().traceId)), new Set([root.spanContext().traceId]));
  assert.equal(attr(root, "wake.claude.session_id"), "session-1");
  assert.equal(parentSpanId(sub), root.spanContext().spanId);
  const calls = wakeEvents(spans, "model_call").filter((e) => attr(e, "wake.agent.id") === "support/researcher");
  assert.ok(calls.every((c) => parentSpanId(c) === sub.spanContext().spanId));
});

function identity(span: ReadableSpan) {
  return [attr(span, "wake.agent.instance"), attr(span, "wake.agent.lineage")];
}

test("the subagent is an instance started by its agent", async () => {
  const { spans } = await support();
  for (const event of wakeEvents(spans)) {
    if (attr(event, "wake.agent.id") === "support/researcher") {
      assert.deepEqual(identity(event), ["support/researcher#toolu_agent", ["support"]], event.name);
    } else {
      assert.deepEqual(identity(event), [undefined, undefined], `${event.name} is the main agent's own`);
    }
  }
  const sub = spans.find((s) => s.name === "invoke_agent support/researcher")!;
  assert.deepEqual(identity(sub), ["support/researcher#toolu_agent", ["support"]]);
});

async function twoResearchers(cli: FakeClaudeCode): Promise<void> {
  cli.emit({ type: "system", subtype: "init", session_id: "session-1", uuid: "u0" });
  const asks: [string, string, string][] = [
    ["toolu_window", "Check the refund window", "Thirty days."],
    ["toolu_shipping", "Check who pays return shipping", "We do."],
  ];
  cli.assistant("msg_1", asks.map(([use, ask]) => ({ type: "tool_use", id: use, name: "Agent", input: { description: ask, prompt: ask, subagent_type: "researcher" } })));
  for (const [use, ask, answer] of asks) {
    cli.user([{ type: "text", text: ask }], use);
    cli.assistant(`msg_${use}`, [{ type: "text", text: answer }], use);
  }
  for (const [use, , answer] of asks) cli.user([{ type: "tool_result", tool_use_id: use, content: [{ type: "text", text: answer }] }]);
  cli.assistant("msg_2", [{ type: "text", text: ANSWER }]);
  cli.emit({ type: "result", subtype: "success", duration_ms: 10, duration_api_ms: 8, is_error: false, num_turns: 2, session_id: "session-1", result: ANSWER });
}

test("two subagents of one type are two instances", async () => {
  const { recorder, spans } = recorded("support");
  await runSupport(new WakeClaudeAgent(recorder), twoResearchers);
  const events = wakeEvents(await spans());
  const kinds = new Map<string, string[]>();
  for (const event of events.filter((e) => attr(e, "wake.agent.id") === "support/researcher")) {
    const [instance, lineage] = identity(event);
    assert.deepEqual(lineage, ["support"]);
    kinds.set(instance as string, [...(kinds.get(instance as string) ?? []), attr(event, "wake.event.kind") as string]);
  }
  assert.deepEqual(
    Object.fromEntries([...kinds].map(([instance, k]) => [instance, k.sort()])),
    {
      "support/researcher#toolu_window": ["agent_message", "model_call"],
      "support/researcher#toolu_shipping": ["agent_message", "model_call"],
    },
    "each started its own run, under one agent name",
  );
  const answers = events.filter((e) => e.name === "agent_message support/researcher -> support");
  assert.equal(answers.length, 2);
  assert.ok(answers.every((a) => identity(a).every((v) => v === undefined)), "each answer reaches the main agent");
});

test("an in-process tool runs with its event current", async () => {
  const { spans } = await support();
  const lookup = byName(spans, "execute_tool mcp__crm__lookup_order");
  assert.equal(seenLinks.length, 1);
  assert.equal(seenLinks[0]?.eventId, id(lookup));
  assert.equal(seenLinks[0]?.spanId, lookup.spanContext().spanId);
  assert.equal(links.current(), undefined);
});

test("a later turn continues from the last", async () => {
  const { recorder, spans } = recorded("support");
  const claude = new WakeClaudeAgent(recorder);
  const followUp = async (cli: FakeClaudeCode) => {
    cli.emit({ type: "system", subtype: "init", session_id: "session-1", uuid: "u9" });
    cli.assistant("msg_6", [{ type: "text", text: "Done." }]);
    cli.emit({ type: "result", subtype: "success", duration_ms: 1, duration_api_ms: 1, is_error: false, num_turns: 1, session_id: "session-1", result: "Done." });
  };
  await runSupport(claude);
  await runSupport(claude, followUp);
  const calls = wakeEvents(await spans(), "model_call").filter((e) => attr(e, "wake.agent.id") === "support");
  assert.equal(parents(calls.at(-1)!)[0], id(calls.at(-2)!));
});

test("a run inside a recorded tool links to it", async () => {
  const { recorder, spans } = recorded("support");
  const outer = new links.Link("outer-tool-call", "1234".padStart(32, "0"), "5678".padStart(16, "0"));
  await links.within(links._frame(outer), () => runSupport(new WakeClaudeAgent(recorder)));
  const [first] = wakeEvents(await spans());
  assert.deepEqual(parents(first), ["outer-tool-call"]);
  assert.deepEqual(origins(first), [], "the calling tool's lineage carries the origins");
  assert.equal(first.spanContext().traceId, outer.traceId);
});

test("recording failures never break the run", async () => {
  const recorder = new Recorder({ agent: "support", exporter: recorded("x").exporter });
  recorder.tracer.startSpan = () => {
    throw new Error("recorder bug");
  };
  seenLinks.length = 0;
  const messages = await runSupport(new WakeClaudeAgent(recorder));
  assert.equal((messages.at(-1) as { result?: string }).result, ANSWER);
  assert.deepEqual(seenLinks, [undefined], "the tool still ran");
});

test("a memory write in a tool is labeled with its origins", async () => {
  const { recorder, spans } = recorded("support");
  const claude = new WakeClaudeAgent(recorder);
  const memory = new Memory(claude);
  const dir = await mkdtemp(join(tmpdir(), "wake-claude-"));
  await runSupport(claude, supportRun, [lookupOrder(() => memory.writeFile(join(dir, "notes.md"), ORDER))]);
  const all = await spans();
  const lookup = byName(all, "execute_tool mcp__crm__lookup_order");
  const [write] = wakeEvents(all, "memory_write");
  assert.equal(parents(write)[0], id(lookup));
  assert.equal(attr(write, "wake.agent.id"), "support");
  assert.deepEqual(identity(write), [undefined, undefined], "the main agent wrote it");
  assert.deepEqual(new Set(attr(write, "wake.memory.origins") as string[]), new Set(["tool:mcp__crm__lookup_order", "user"]));
});

async function researcherRemembers(cli: FakeClaudeCode): Promise<void> {
  cli.emit({ type: "system", subtype: "init", session_id: "session-1", uuid: "u0" });
  const research = { description: "Look the order up", prompt: "Look order 1234 up", subagent_type: "researcher" };
  cli.assistant("msg_1", [{ type: "tool_use", id: "toolu_agent", name: "Agent", input: research }]);
  await cli.hook("PreToolUse", "toolu_agent", { tool_name: "Agent", tool_input: research });
  cli.user([{ type: "text", text: "Look order 1234 up" }], "toolu_agent");
  cli.assistant("msg_2", [{ type: "tool_use", id: "toolu_order", name: "mcp__crm__lookup_order", input: { order: "1234" } }], "toolu_agent");
  await cli.useTool("toolu_order", "mcp__crm__lookup_order", { order: "1234" }, () => cli.callTool("crm", "lookup_order", { order: "1234" }), {
    parent: "toolu_agent",
    agent: "researcher",
  });
  cli.assistant("msg_3", [{ type: "text", text: ORDER }], "toolu_agent");
  cli.user([{ type: "tool_result", tool_use_id: "toolu_agent", content: [{ type: "text", text: ORDER }] }]);
  cli.assistant("msg_4", [{ type: "text", text: ANSWER }]);
  cli.emit({ type: "result", subtype: "success", duration_ms: 10, duration_api_ms: 8, is_error: false, num_turns: 4, session_id: "session-1", result: ANSWER });
}

test("a memory write in a subagent's tool names the instance", async () => {
  const { recorder, spans } = recorded("support");
  const claude = new WakeClaudeAgent(recorder);
  const memory = new Memory(claude);
  const dir = await mkdtemp(join(tmpdir(), "wake-claude-"));
  await runSupport(claude, researcherRemembers, [lookupOrder(() => memory.writeFile(join(dir, "notes.md"), ORDER))]);
  const all = await spans();
  const lookup = byName(all, "execute_tool mcp__crm__lookup_order");
  const [write] = wakeEvents(all, "memory_write");
  assert.equal(parents(write)[0], id(lookup));
  assert.equal(attr(write, "wake.agent.id"), "support/researcher");
  assert.deepEqual(identity(lookup), ["support/researcher#toolu_agent", ["support"]]);
  assert.deepEqual(identity(write), ["support/researcher#toolu_agent", ["support"]]);
});
