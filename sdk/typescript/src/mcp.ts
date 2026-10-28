/**
 * Links MCP calls to the Wake events making them, so the proxy's record of each call joins
 * the caller's trace and provenance. See wake-sdk/links and docs/proxy.md.
 *
 * With the MCP client directly, give its transport Wake's fetch, and pass the link with each
 * call from inside a recorded tool call:
 *
 *     const transport = new StreamableHTTPClientTransport(new URL(url), { fetch: mcp.fetch() });
 *     await client.callTool({ name, arguments: args, _meta: links.meta() });
 *
 * Wake's fetch copies the link from params._meta into the traceparent and wake-parents
 * headers: agentgateway drops _meta before the proxy sees the call, but forwards those
 * headers. A request with no _meta link gets the current event's, if there is one.
 *
 * With LangChain's MCP adapters, add the hook:
 *
 *     const client = new MultiServerMCPClient({ mcpServers, beforeToolCall: mcp.beforeToolCall });
 */

import { log } from "./log.ts";
import * as links from "./links.ts";
import { toolCallFrame } from "./registry.ts";

type Fetch = typeof globalThis.fetch;

/**
 * A fetch that adds each MCP request's link to its headers: from the JSON-RPC request's
 * params._meta when the call carries one, else the current event's. Never fails a request
 * over it: one it can't read goes out unchanged.
 */
export function fetch(base: Fetch = globalThis.fetch): Fetch {
  return (input, init) => {
    try {
      const extra = linkFor(init);
      if (extra !== undefined) {
        const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
        for (const [name, value] of Object.entries(extra)) if (!headers.has(name)) headers.set(name, value);
        init = { ...init, headers };
      }
    } catch (error) {
      log.warn("failed to link an MCP request", error);
    }
    return base(input, init);
  };
}

function linkFor(init: RequestInit | undefined): Record<string, string> | undefined {
  if (init?.method?.toUpperCase() !== "POST") return undefined;
  if (typeof init.body === "string") {
    const message: unknown = JSON.parse(init.body);
    const meta = message !== null && typeof message === "object" ? (message as { params?: { _meta?: unknown } }).params?._meta : undefined;
    const link = links.fromMeta(meta as Record<string, unknown> | undefined);
    if (link !== undefined) return metaHeaders(meta as Record<string, unknown>);
  }
  const current = links.headers();
  return Object.keys(current).length > 0 ? current : undefined;
}

/** The link in _meta as headers, as the caller gave it: the proxy reads it as untrusted input. */
function metaHeaders(meta: Record<string, unknown>): Record<string, string> {
  const headers: Record<string, string> = {};
  const traceparent = meta[links.META_TRACEPARENT];
  if (typeof traceparent === "string") headers[links.HEADER_TRACEPARENT] = traceparent;
  const parents = meta[links.META_PARENTS];
  if (Array.isArray(parents)) {
    const ids = parents.filter((p): p is string => typeof p === "string" && p !== "" && !p.includes(","));
    if (ids.length > 0) headers[links.HEADER_PARENTS] = ids.join(",");
  }
  return headers;
}

/**
 * A @langchain/mcp-adapters beforeToolCall hook that adds the link headers to each call. The
 * adapters open a connection with them for the call. The tool call comes from the config the
 * adapters pass, which names the call LangChain is running, or else from the current event.
 */
export function beforeToolCall(_request: unknown, _state: unknown, config?: unknown): { headers: Record<string, string> } | undefined {
  try {
    const frame = toolCallFrame((config as { toolCall?: { id?: string } } | undefined)?.toolCall?.id);
    const link = frame !== undefined ? (frame.latest ?? frame.link) : undefined;
    const headers = link !== undefined ? link.headers() : links.headers();
    return Object.keys(headers).length > 0 ? { headers } : undefined;
  } catch (error) {
    log.warn("failed to link an MCP call", error);
    return undefined;
  }
}
