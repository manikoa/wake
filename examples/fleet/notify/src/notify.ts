/**
 * The notify agent, on LangGraph.js and Wake's TypeScript SDK: emails customers what support
 * asks it to, served over A2A. The email goes out through the fleet's MCP server, behind
 * agentgateway, and each one is noted in notify's outbox, a memory file.
 *
 * The message from support is linked: notify's run descends from the proxy's record of it,
 * the email's MCP call carries a link to the tool call making it, and the reply names the
 * step that produced it.
 */

import { readFile, mkdir } from "node:fs/promises";
import { dirname, join } from "node:path";

import { Role, type Message } from "@a2a-js/sdk";
import {
  AgentEvent,
  DefaultRequestHandler,
  InMemoryTaskStore,
  type AgentExecutor,
  type ExecutionEventBus,
  type RequestContext,
} from "@a2a-js/sdk/server";
import { jsonRpcHandler, UserBuilder } from "@a2a-js/sdk/server/express";
import { HumanMessage } from "@langchain/core/messages";
import { tool } from "@langchain/core/tools";
import { MessagesAnnotation, START, StateGraph } from "@langchain/langgraph";
import { ToolNode, toolsCondition } from "@langchain/langgraph/prebuilt";
import { MultiServerMCPClient } from "@langchain/mcp-adapters";
import express from "express";
import { Recorder } from "wake-sdk";
import * as a2a from "wake-sdk/a2a";
import { WakeCallbackHandler } from "wake-sdk/langgraph";
import * as mcp from "wake-sdk/mcp";
import { Memory } from "wake-sdk/memory";
import { z } from "zod";

import { NotifyModel } from "./scripted.ts";

const NAME = "notify";
const PORT = 9103;
const GATEWAY = (process.env.FLEET_GATEWAY ?? "http://localhost:3000").replace(/\/+$/, "");
// Who the agent says it is to the gateway. A stand-in for the SPIFFE ID or JWT the gateway
// reads in production; see agentgateway.yaml.
const AGENT_HEADER = "x-fleet-agent";
// The stand-in identity provider, and where this agent's credentials are: its own volume.
const IDP = (process.env.FLEET_IDP ?? "http://localhost:8080/oauth2/v1").replace(/\/+$/, "");
const SECRETS = process.env.FLEET_SECRETS ?? "/etc/fleet/secrets";
const OUTBOX = process.env.FLEET_OUTBOX ?? "/tmp/fleet/notify-outbox.md";

// Dropped events are logged here; in production onDrop pages someone.
const recorder = new Recorder({ agent: NAME, onDrop: (count, reason) => console.error(`wake dropped ${count} events: ${reason}`) });
const handler = new WakeCallbackHandler(recorder);
const memory = new Memory(handler);

let access: { token: string; expires: number } = { token: "", expires: 0 };

/**
 * The agent's access token for the tools, swapped for the refresh token the identity
 * provider issued it, and swapped again a minute before it expires. If the swap fails, as it
 * does once the brake has revoked the refresh token, the last one is sent anyway: the tools
 * server decides whether it's still good.
 */
async function accessToken(): Promise<string> {
  if (Date.now() < access.expires - 60_000) return access.token;
  try {
    const refresh = (await readFile(join(SECRETS, "refresh-token"), "utf8")).trim();
    const response = await fetch(`${IDP}/token`, {
      method: "POST",
      body: new URLSearchParams({ grant_type: "refresh_token", refresh_token: refresh, client_id: NAME }),
      signal: AbortSignal.timeout(10_000),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const body = (await response.json()) as { access_token: string; expires_in?: number };
    access = { token: body.access_token, expires: Date.now() + (body.expires_in ?? 0) * 1000 };
  } catch (error) {
    console.warn(`${NAME} couldn't get an access token: ${error}`);
  }
  return access.token;
}

/** Notes each email in the outbox. Its memory adapter labels the note with where it came from. */
const remember = tool(
  async ({ note }) => {
    await mkdir(dirname(OUTBOX), { recursive: true });
    await memory.writeFile(OUTBOX, note);
    return "Noted.";
  },
  { name: "remember", description: "Keep a note of an email sent.", schema: z.object({ note: z.string() }) },
);

async function handle(request: string): Promise<string> {
  // The adapters open a connection for each call, with the link to the tool call making it.
  const token = await accessToken();
  const client = new MultiServerMCPClient({
    mcpServers: {
      fleet: {
        transport: "http",
        url: `${GATEWAY}/mcp`,
        headers: { [AGENT_HEADER]: NAME, ...(token ? { authorization: `Bearer ${token}` } : {}) },
      },
    },
    beforeToolCall: mcp.beforeToolCall,
  });
  try {
    const email = (await client.getTools()).filter((t) => t.name.endsWith("send_email"));
    const model = new NotifyModel(email[0].name);
    const graph = new StateGraph(MessagesAnnotation)
      .addNode("model", async (state) => ({ messages: [await model.invoke(state.messages)] }))
      .addNode("tools", new ToolNode([...email, remember]))
      .addEdge(START, "model")
      .addConditionalEdges("model", toolsCondition)
      .addEdge("tools", "model")
      .compile();
    const result = await graph.invoke({ messages: [new HumanMessage(request)] }, { callbacks: [handler] });
    return String(result.messages.at(-1)?.content ?? "");
  } finally {
    await client.close();
  }
}

function textOf(message: Message | undefined): string {
  return (message?.parts ?? []).map((p) => (p.content?.$case === "text" ? p.content.value : "")).join("\n");
}

function reply(text: string, contextId: string): Message {
  return {
    messageId: crypto.randomUUID(),
    contextId,
    taskId: "",
    role: Role.ROLE_AGENT,
    parts: [{ content: { $case: "text", value: text } }],
    metadata: undefined,
    extensions: [],
    referenceTaskIds: [],
  } as unknown as Message;
}

class Notify implements AgentExecutor {
  async execute(context: RequestContext, bus: ExecutionEventBus): Promise<void> {
    // The run descends from the message, and the reply, linked inside, names its last step.
    const answer = await a2a.received(context, async () => a2a.link(reply(await handle(textOf(context.userMessage)), context.contextId)));
    bus.publish(AgentEvent.message(answer));
    bus.finished();
  }

  async cancelTask(): Promise<void> {}
}

const card = {
  name: NAME,
  description: "Emails customers.",
  version: "0.1.0",
  supportedInterfaces: [{ url: `${GATEWAY}/agents/${NAME}`, protocolBinding: "JSONRPC", protocolVersion: "1.0", tenant: "" }],
  capabilities: { streaming: false, pushNotifications: false, extensions: [] },
  defaultInputModes: ["text/plain"],
  defaultOutputModes: ["text/plain"],
  skills: [{ id: NAME, name: NAME, description: "Emails customers.", tags: ["fleet"], examples: [], inputModes: [], outputModes: [], securityRequirements: [] }],
  securitySchemes: {},
  securityRequirements: [],
  signatures: [],
} as unknown as ConstructorParameters<typeof DefaultRequestHandler>[0];

const app = express();
app.use("/", jsonRpcHandler({ requestHandler: new DefaultRequestHandler(card, new InMemoryTaskStore(), new Notify()), userBuilder: UserBuilder.noAuthentication }));
const server = app.listen(PORT, "0.0.0.0");

// Send the events still queued before exiting.
for (const signal of ["SIGTERM", "SIGINT"] as const) {
  process.once(signal, async () => {
    server.close();
    await recorder.shutdown();
    console.error(JSON.stringify({ wake: recorder.health() }));
    process.exit(0);
  });
}
