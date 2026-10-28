/**
 * Links an outgoing call to the Wake event making it. See docs/linking.md.
 *
 * While the SDK records a tool call, the call's event is current here. A call that leaves
 * the process through the proxy, such as an MCP tool call through agentgateway, carries a
 * link to it: W3C trace context, and the event's id as the parent of the proxy's record of
 * the same call. The proxy's event then joins the caller's trace and provenance instead of
 * standing alone.
 *
 *     const headers = links.headers(); // {traceparent, "wake-parents"}, or {}
 *
 * The receiving end does the reverse. While an agent handles a message from another agent,
 * the link the message carried is current, and a run the agent starts descends from it:
 *
 *     await links.received({ headers: request.headers, metadata: message.metadata }, async () => {
 *       const answer = await agent.run(...);
 *       reply = { text: answer, metadata: links.meta() }; // names the run's last step
 *     });
 *
 * Outside any recorded tool call, an outgoing call links to the latest event recorded under
 * the current link, so a reply names the step that produced it. And when what a call gets
 * back came from recorded events, such as an A2A reply the proxy recorded, returned() says
 * so, and the step that reads the result descends from them.
 *
 * The current link is kept per task of execution in an AsyncLocalStorage. A framework that
 * runs tools in a context of its own, such as LangChain's or the OpenAI Agents SDK's, has
 * its integration resolve the tool call from that context instead.
 */

import { AsyncLocalStorage } from "node:async_hooks";

import { log } from "./log.ts";

export const HEADER_TRACEPARENT = "traceparent";
export const HEADER_PARENTS = "wake-parents";
export const META_TRACEPARENT = "traceparent";
export const META_PARENTS = "dev.wake/parents";

const INVALID_TRACE_ID = "0".repeat(32);
const INVALID_SPAN_ID = "0".repeat(16);

/**
 * The event an outgoing call belongs to. A received message can name more than one event
 * it came from, and may carry no trace (traceId all zeros).
 */
export class Link {
  readonly eventId: string;
  readonly traceId: string;
  readonly spanId: string;
  readonly otherParents: readonly string[];

  constructor(eventId: string, traceId = INVALID_TRACE_ID, spanId = INVALID_SPAN_ID, otherParents: readonly string[] = []) {
    this.eventId = eventId;
    this.traceId = traceId;
    this.spanId = spanId;
    this.otherParents = otherParents;
  }

  get parents(): string[] {
    return [this.eventId, ...this.otherParents];
  }

  get hasTrace(): boolean {
    return this.traceId !== INVALID_TRACE_ID && this.spanId !== INVALID_SPAN_ID && this.traceId !== "" && this.spanId !== "";
  }

  get traceparent(): string | null {
    return this.hasTrace ? `00-${this.traceId}-${this.spanId}-01` : null;
  }

  headers(): Record<string, string> {
    const headers: Record<string, string> = { [HEADER_PARENTS]: this.parents.join(",") };
    if (this.traceparent) headers[HEADER_TRACEPARENT] = this.traceparent;
    return headers;
  }

  meta(): Record<string, unknown> {
    const meta: Record<string, unknown> = { [META_PARENTS]: this.parents };
    if (this.traceparent) meta[META_TRACEPARENT] = this.traceparent;
    return meta;
  }
}

/**
 * One recorded call's link, on top of whatever link was current when it began, with the
 * latest event recorded while it was on top and the events its result came from.
 */
export class Frame {
  readonly link: Link;
  readonly previous: Frame | undefined;
  // Frames visible from one place all enclose it, so the latest begun is innermost.
  readonly order: number;
  ended = false;
  latest: Link | undefined;
  readonly returned: string[] = [];

  constructor(link: Link, previous: Frame | undefined) {
    this.link = link;
    this.previous = previous;
    this.order = ++begun;
  }
}

let begun = 0;
const storage = new AsyncLocalStorage<Frame | undefined>();
const resolvers = new Set<() => Frame | undefined>();

/**
 * The frame current here. Frames can end in any order, such as when two integrations
 * record the same call, so ended ones are skipped. A frame a framework's context names
 * (see _addResolver) is current when it began after the one this task carries.
 */
export function _top(): Frame | undefined {
  let frame = storage.getStore();
  while (frame !== undefined && frame.ended) frame = frame.previous;
  for (const resolve of resolvers) {
    let found: Frame | undefined;
    try {
      found = resolve();
    } catch (error) {
      log.warn("failed to find the current tool call", error);
    }
    if (found !== undefined && !found.ended && (frame === undefined || found.order > frame.order)) frame = found;
  }
  return frame;
}

/** The event the code running now belongs to, if the SDK is recording one. */
export function current(): Link | undefined {
  return _top()?.link;
}

/**
 * What a call made now comes from: the latest event recorded under the current link, such
 * as a run's last step, else the link's own event, such as the tool call.
 */
function outgoing(): Link | undefined {
  const frame = _top();
  return frame === undefined ? undefined : (frame.latest ?? frame.link);
}

/** HTTP headers linking an outgoing call to the current event, or {} if there's none. */
export function headers(): Record<string, string> {
  return outgoing()?.headers() ?? {};
}

/** The same link as MCP params._meta or A2A metadata fields, or {} if there's no current event. */
export function meta(): Record<string, unknown> {
  return outgoing()?.meta() ?? {};
}

/**
 * Records that what the current call got back came from these events, such as the
 * proxy's record of an A2A reply. The step that reads the call's result descends from
 * them. Ids that don't look like event ids are dropped.
 */
export function returned(parents: unknown): void {
  const frame = _top();
  if (frame === undefined || !Array.isArray(parents)) return;
  for (const p of parents) {
    const id = typeof p === "string" ? p.trim() : "";
    if (EVENT_ID.test(id) && !frame.returned.includes(id) && frame.returned.length < MAX_PARENTS) frame.returned.push(id);
  }
}

/** Called by the integrations for each event they record. */
export function _note(link: Link): void {
  const frame = _top();
  if (frame !== undefined) frame.latest = link;
}

export function _returned(frame: Frame | undefined): string[] {
  return frame === undefined ? [] : [...frame.returned];
}

/** A frame for this link, on top of the current one. Run code under it with _run. */
export function _frame(link: Link): Frame {
  return new Frame(link, _top());
}

/** Runs fn with this frame current, in this task and every task it starts. */
export function _run<T>(frame: Frame, fn: () => T): T {
  return storage.run(frame, fn);
}

/** Ends a frame: code still running under it sees the frame below it instead. */
export function _exit(frame: Frame | undefined): void {
  if (frame !== undefined) frame.ended = true;
}

/**
 * Adds a way to find the current frame from a framework's own context, such as the tool
 * call whose function is running now. Returns a function that removes it.
 */
export function _addResolver(resolve: () => Frame | undefined): () => void {
  resolvers.add(resolve);
  return () => resolvers.delete(resolve);
}

// What a received link may carry (docs/linking.md). Headers and metadata come from another
// agent, so anything else is dropped rather than recorded. A traceparent is W3C's: version
// 00 has exactly four fields, a later version may add more, and ff is never valid.
const TRACEPARENT = /^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}(-.*)?$/s;
export const EVENT_ID = /^[A-Za-z0-9._:-]{1,128}$/;
export const MAX_PARENTS = 64;

function parse(traceparent: unknown, parents: unknown): Link | undefined {
  if (typeof parents === "string") parents = parents.split(",");
  if (!Array.isArray(parents)) return undefined;
  const ids = [
    ...new Set(parents.filter((p): p is string => typeof p === "string").map((p) => p.trim()).filter((p) => EVENT_ID.test(p))),
  ].slice(0, MAX_PARENTS);
  if (ids.length === 0) return undefined;
  let traceId = INVALID_TRACE_ID;
  let spanId = INVALID_SPAN_ID;
  const match = typeof traceparent === "string" ? TRACEPARENT.exec(traceparent.trim()) : null;
  if (match && match[1] !== "ff" && !(match[1] === "00" && match[4])) {
    if (match[2] !== INVALID_TRACE_ID && match[3] !== INVALID_SPAN_ID) [traceId, spanId] = [match[2], match[3]];
  }
  return new Link(ids[0], traceId, spanId, ids.slice(1));
}

type HeaderSource =
  | Headers
  | Map<string, unknown>
  | Iterable<[string, unknown]>
  | Record<string, unknown>
  | null
  | undefined;

/** A header's value, by any capitalization, from a fetch Headers, a Map, pairs or a plain object. */
export function header(source: HeaderSource, name: string): unknown {
  if (source === null || source === undefined) return undefined;
  if (typeof (source as Headers).get === "function" && typeof (source as Headers).has === "function" && !(source instanceof Map)) {
    return (source as Headers).get(name) ?? undefined;
  }
  const entries: Iterable<[string, unknown]> =
    source instanceof Map || Symbol.iterator in (source as object)
      ? (source as Iterable<[string, unknown]>)
      : Object.entries(source as Record<string, unknown>);
  for (const [key, value] of entries) {
    if (String(key).toLowerCase() === name) return Array.isArray(value) ? value.join(",") : value;
  }
  return undefined;
}

/** The link in a request's traceparent and wake-parents headers, if it has one. */
export function fromHeaders(source: HeaderSource): Link | undefined {
  if (source === null || source === undefined) return undefined;
  return parse(header(source, HEADER_TRACEPARENT), header(source, HEADER_PARENTS));
}

/** The link in message metadata or MCP _meta, if it has one. */
export function fromMeta(meta: Record<string, unknown> | null | undefined): Link | undefined {
  if (meta === null || meta === undefined || typeof meta !== "object") return undefined;
  return parse(meta[META_TRACEPARENT], meta[META_PARENTS]);
}

/**
 * Runs fn with the link an incoming message carries current, while the agent handles it.
 *
 * The headers win when both carry one: through the proxy, they name the proxy's record of
 * the message, which is itself a child of the sender's event named in the metadata. A
 * promise fn returns keeps the link current until it settles.
 */
export function received<T>(
  carriers: { headers?: HeaderSource; metadata?: Record<string, unknown> | null },
  fn: (link: Link | undefined) => T,
): T {
  let link: Link | undefined;
  try {
    link = fromHeaders(carriers.headers) ?? fromMeta(carriers.metadata);
  } catch (error) {
    log.warn("failed to read a received link", error);
  }
  if (link === undefined) return fn(undefined);
  const frame = _frame(link);
  return within(frame, () => fn(link));
}

/** Runs fn under the frame, and ends the frame when fn returns, or when its promise settles. */
export function within<T>(frame: Frame, fn: () => T): T {
  let result: T;
  try {
    result = _run(frame, fn);
  } catch (error) {
    _exit(frame);
    throw error;
  }
  if (result instanceof Promise) {
    return result.finally(() => _exit(frame)) as T;
  }
  _exit(frame);
  return result;
}
