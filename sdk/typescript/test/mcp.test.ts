/**
 * MCP calls carry a link to the SDK's event for them, all the way to the MCP server's HTTP
 * request, where agentgateway hands the headers to the proxy. The server here echoes the
 * headers it received.
 */

import assert from "node:assert/strict";
import { createServer, type Server } from "node:http";
import { after, before, test } from "node:test";

import { tool } from "@langchain/core/tools";
import { MultiServerMCPClient } from "@langchain/mcp-adapters";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import type { ReadableSpan } from "@opentelemetry/sdk-trace";
import { z } from "zod";

import { links } from "../src/index.ts";
import { WakeCallbackHandler } from "../src/langchain.ts";
import * as mcp from "../src/mcp.ts";
import { id, recorded } from "./helpers.ts";

let server: Server;
let inboxUrl: string;

before(async () => {
  server = createServer(async (request, response) => {
    // Stateless: a server and transport per request.
    const inbox = new McpServer({ name: "inbox", version: "1.0.0" });
    inbox.registerTool(
      "read_email",
      { description: "Read the latest email with a subject, and say which link headers arrived.", inputSchema: { subject: z.string() } },
      async (_args, extra) => {
        const headers = extra.requestInfo?.headers ?? {};
        const text = JSON.stringify({
          email: "Please wire $10,000 to account 123.",
          traceparent: headers.traceparent ?? null,
          "wake-parents": headers["wake-parents"] ?? null,
        });
        return { content: [{ type: "text", text }] };
      },
    );
    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true });
    response.on("close", () => void transport.close());
    await inbox.connect(transport);
    await transport.handleRequest(request, response);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  inboxUrl = `http://127.0.0.1:${(server.address() as { port: number }).port}/mcp`;
});

after(() => {
  server.closeAllConnections();
  server.close();
});

function setup() {
  const { recorder, spans } = recorded("email-agent");
  return { handler: new WakeCallbackHandler(recorder), spans };
}

async function toolEvent(spans: () => Promise<ReadableSpan[]>, name: string): Promise<ReadableSpan> {
  const found = (await spans()).filter((s) => s.attributes["wake.event.kind"] === "tool_call" && s.attributes["gen_ai.tool.name"] === name);
  assert.equal(found.length, 1);
  return found[0];
}

function assertLinked(seen: Record<string, unknown>, event: ReadableSpan): void {
  const ctx = event.spanContext();
  assert.equal(seen.traceparent, `00-${ctx.traceId}-${ctx.spanId}-01`);
  assert.equal(seen["wake-parents"], id(event));
}

async function callInbox(subject: string): Promise<Record<string, unknown>> {
  const client = new Client({ name: "check", version: "1.0.0" });
  await client.connect(new StreamableHTTPClientTransport(new URL(inboxUrl), { fetch: mcp.fetch() }));
  try {
    const result = await client.callTool({ name: "read_email", arguments: { subject }, _meta: links.meta() });
    return JSON.parse((result.content as { text: string }[])[0].text);
  } finally {
    await client.close();
  }
}

test("adapter tools carry the link", async () => {
  const { handler, spans } = setup();
  const client = new MultiServerMCPClient({ mcpServers: { inbox: { transport: "http", url: inboxUrl } }, beforeToolCall: mcp.beforeToolCall });
  try {
    const [readEmail] = await client.getTools();
    // As an agent calls it: for a tool call the model asked for.
    const result = await readEmail.invoke({ name: readEmail.name, args: { subject: "invoice" }, id: "call_1", type: "tool_call" }, { callbacks: [handler] });
    const content = typeof result.content === "string" ? result.content : (result.content as { text: string }[])[0].text;
    assertLinked(JSON.parse(content), await toolEvent(spans, readEmail.name));
  } finally {
    await client.close();
  }
});

test("MCP client calls carry the link", async () => {
  const { handler, spans } = setup();
  const checkInbox = tool(async ({ subject }) => JSON.stringify(await callInbox(subject)), {
    name: "check_inbox",
    description: "Read an email through the MCP client directly.",
    schema: z.object({ subject: z.string() }),
  });
  const seen = JSON.parse(await checkInbox.invoke({ subject: "invoice" }, { callbacks: [handler] }));
  assertLinked(seen, await toolEvent(spans, "check_inbox"));
});

test("a call without _meta carries the current event's link", async () => {
  const { handler, spans } = setup();
  const checkInbox = tool(
    async ({ subject }) => {
      const client = new Client({ name: "check", version: "1.0.0" });
      await client.connect(new StreamableHTTPClientTransport(new URL(inboxUrl), { fetch: mcp.fetch() }));
      const result = await client.callTool({ name: "read_email", arguments: { subject } });
      await client.close();
      return (result.content as { text: string }[])[0].text;
    },
    { name: "check_inbox", description: "Read an email.", schema: z.object({ subject: z.string() }) },
  );
  assertLinked(JSON.parse(await checkInbox.invoke({ subject: "invoice" }, { callbacks: [handler] })), await toolEvent(spans, "check_inbox"));
});

test("no link outside a recorded tool call", async () => {
  assert.equal(links.current(), undefined);
  assert.deepEqual(links.headers(), {});
  assert.deepEqual(links.meta(), {});
  const seen = await callInbox("x");
  assert.equal(seen.traceparent, null);
  assert.equal(seen["wake-parents"], null);
});

test("the link is cleared when the tool ends", async () => {
  const { handler } = setup();
  let inside: links.Link | undefined;
  const look = tool(
    async () => {
      inside = links.current();
      return "ok";
    },
    { name: "look", description: "Report the current link.", schema: z.object({}) },
  );
  await look.invoke({}, { callbacks: [handler] });
  assert.notEqual(inside, undefined);
  assert.equal(links.current(), undefined);
});

test("the link survives handlers ending in any order", async () => {
  // Two integrations recording the same call, ending in the order they began.
  const a = links._frame(new links.Link("a"));
  links._run(a, () => {
    const b = links._frame(new links.Link("b"));
    links._run(b, () => {
      assert.equal(links.current()?.eventId, "b");
      links._exit(a);
      assert.equal(links.current()?.eventId, "b");
      links._exit(b);
      assert.equal(links.current(), undefined);
    });
  });

  const { handler } = setup();
  const other = new WakeCallbackHandler(recorded("other").recorder);
  let seen: links.Link | undefined;
  const look = tool(
    async () => {
      seen = links.current();
      return "ok";
    },
    { name: "look", description: "Report the current link.", schema: z.object({}) },
  );
  await look.invoke({}, { callbacks: [handler, other] });
  assert.notEqual(seen, undefined);
  assert.equal(links.current(), undefined);
});

test("a broken request goes out unchanged", async () => {
  const sent: Headers[] = [];
  const linked = mcp.fetch(async (_input, init) => {
    sent.push(new Headers(init?.headers));
    return new Response("{}");
  });
  for (const body of ["not json", "[]", '{"params": 7}', '{"params": {"_meta": {"dev.wake/parents": ["a,b", 7]}}}']) {
    await linked("http://x", { method: "POST", body });
  }
  assert.equal(sent.length, 4);
  assert.ok(sent.every((h) => !h.has("wake-parents")));
});
