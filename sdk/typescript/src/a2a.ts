/**
 * Links A2A messages between agents, so the receiving agent's steps join the sender's trace
 * and descend from the step that sent the message. See wake-sdk/links and docs/proxy.md.
 *
 * Sending, with the @a2a-js/sdk client, from inside a recorded tool call: give the client
 * Wake's fetch and link the message.
 *
 *     const factory = new ClientFactory({ transports: [new JsonRpcTransportFactory({ fetchImpl: a2a.fetch() })] });
 *     const client = await factory.createFromAgentCard(card);
 *     const reply = await client.sendMessage({ message: a2a.link(message) });
 *
 * The headers reach the proxy, which records the message as a child of the tool call and
 * passes its own link on. The message metadata carries the same link end to end, for agents
 * that talk without the proxy between them.
 *
 * The reply comes back the same way. The proxy records it and names its record in the
 * response's wake-parents header, which Wake's fetch reads, so the step that reads the
 * tool's result descends from the reply and from whatever the receiver did to produce it.
 * Without the proxy, call replied(reply) on what the client returns.
 *
 * Receiving, in an @a2a-js/sdk AgentExecutor:
 *
 *     async execute(context, bus) {
 *       const reply = await a2a.received(context, async () => {
 *         const answer = await agent.run(textOf(context.userMessage));
 *         return a2a.link(agentMessage(answer, context.contextId));
 *       });
 *       bus.publish(AgentEvent.message(reply));
 *       bus.finished();
 *     }
 *
 * A run the agent starts inside received() descends from the message: from the proxy's
 * record of it when there is one, else from the sender's tool call. Linked inside it, the
 * reply names the run's last step.
 */

import * as links from "./links.ts";
import { log } from "./log.ts";

type Fetch = typeof globalThis.fetch;

/**
 * Adds the current event's link to an outgoing A2A message's metadata, and returns the
 * message. Works on any message object with a metadata field, or none yet.
 */
export function link<T>(message: T): T {
  try {
    const meta = links.meta();
    if (Object.keys(meta).length === 0 || message === null || typeof message !== "object") return message;
    const target = message as { metadata?: Record<string, unknown> | null };
    if (target.metadata === null || target.metadata === undefined || typeof target.metadata !== "object") target.metadata = { ...meta };
    else Object.assign(target.metadata, meta);
  } catch (error) {
    log.warn("failed to link an A2A message", error);
  }
  return message;
}

/**
 * A fetch that adds the current event's link headers to each request, and notes the events
 * a response names in its wake-parents header as where the call's result came from.
 */
export function fetch(base: Fetch = globalThis.fetch): Fetch {
  return async (input, init) => {
    try {
      const extra = links.headers();
      if (Object.keys(extra).length > 0) {
        const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
        for (const [name, value] of Object.entries(extra)) if (!headers.has(name)) headers.set(name, value);
        init = { ...init, headers };
      }
    } catch (error) {
      log.warn("failed to link an A2A request", error);
    }
    const response = await base(input, init);
    try {
      const reply = links.fromHeaders(response.headers);
      if (reply !== undefined) links.returned(reply.parents);
    } catch (error) {
      log.warn("failed to read an A2A reply's link", error);
    }
    return response;
  };
}

/**
 * Notes the events an A2A reply names in its metadata as where the current call's result
 * came from. Takes what a client returns: a Message or Task, a SendMessage result holding
 * one, or the JSON-RPC response itself. Through the proxy, fetch() already did this from
 * the response headers.
 */
export function replied(reply: unknown): void {
  try {
    const frame = links._top();
    if (frame === undefined || frame.returned.length > 0) return;
    for (const message of messagesIn(reply)) {
      const found = links.fromMeta(get(message, "metadata") as Record<string, unknown> | undefined);
      if (found !== undefined) links.returned(found.parents);
    }
  } catch (error) {
    log.warn("failed to read an A2A reply's link", error);
  }
}

function get(value: unknown, name: string): unknown {
  return value !== null && typeof value === "object" ? (value as Record<string, unknown>)[name] : undefined;
}

/** The messages a reply carries: itself, or those in a task's or update's status. */
function messagesIn(reply: unknown): unknown[] {
  const out: unknown[] = [];
  const result = get(reply, "result") ?? reply;
  const oneOf = get(get(result, "payload"), "value"); // a protobuf SendMessageResponse
  for (const payload of [result, oneOf, get(result, "message"), get(result, "task"), get(result, "statusUpdate"), get(result, "status_update")]) {
    if (payload === undefined || payload === null) continue;
    if (get(payload, "parts") !== undefined || get(payload, "messageId") !== undefined || get(payload, "message_id") !== undefined) {
      out.push(payload);
    }
    const statusMessage = get(get(payload, "status"), "message");
    if (statusMessage !== undefined && statusMessage !== null) out.push(statusMessage);
  }
  return out;
}

/**
 * Runs fn with the link an incoming A2A message carries current, for an @a2a-js/sdk
 * RequestContext: the request's headers, which its server keeps in the call context's
 * state, and the message's metadata. For other servers, use links.received with the
 * request's headers and the message's metadata.
 */
export function received<T>(context: unknown, fn: (link: links.Link | undefined) => T): T {
  let headers: unknown;
  let metadata: unknown;
  try {
    const state = get(get(context, "context"), "state");
    headers = state instanceof Map ? state.get("headers") : get(state, "headers");
    metadata = get(get(context, "userMessage") ?? get(context, "message"), "metadata");
  } catch (error) {
    log.warn("failed to read an A2A message's link", error);
  }
  return links.received(
    {
      headers: headers as Record<string, unknown> | undefined,
      metadata: metadata !== null && typeof metadata === "object" ? (metadata as Record<string, unknown>) : undefined,
    },
    fn,
  );
}
