/** Attribute names and helpers for Wake events. See docs/events.md. */

import { createHash } from "node:crypto";
import { inspect } from "node:util";

export const EVENT_ID = "wake.event.id";
export const EVENT_KIND = "wake.event.kind";
export const AGENT_ID = "wake.agent.id";
export const AGENT_INSTANCE = "wake.agent.instance";
export const AGENT_LINEAGE = "wake.agent.lineage";
export const CAPTURE = "wake.capture";
export const PARENTS = "wake.parents";
export const ORIGINS = "wake.origins";
export const PAYLOAD = "wake.payload";
export const PAYLOAD_SHA256 = "wake.payload.sha256";

export const MODEL_CALL = "model_call";
export const TOOL_CALL = "tool_call";
export const AGENT_MESSAGE = "agent_message";
export const MEMORY_READ = "memory_read";
export const MEMORY_WRITE = "memory_write";
export const DENIED = "denied";

export const ORIGIN_USER = "user";

// The largest payload sent whole. A larger one, such as a model's context late in a long
// run, is cut to a prefix and marked truncated (docs/events.md), so one event always fits a
// batch the collector takes. wake-proxy keeps 1 MiB of each body it records.
export const MAX_PAYLOAD_BYTES = 4 << 20;

/**
 * JSON for any value, with keys sorted. Never throws.
 *
 * What JSON can hold comes out as Python's json.dumps(value, sort_keys=True) writes it, so
 * a memory label's digest is the same whichever SDK wrote the entry: ", " and ": " between
 * items, keys in code point order, and non-ASCII escaped unless ensureAscii is false. Past
 * that, a value with toJSON() is what it returns, a Map an object, a Set an array, a value
 * inside itself its inspect(), and anything else its String(). JavaScript keeps no
 * difference between 1 and 1.0, so a whole number comes out as an integer, every digit of it:
 * a float Python wrote as 500.0 is 500, as the Python SDK writes a memory entry's content too.
 */
export function dumps(value: unknown, { ensureAscii = true }: { ensureAscii?: boolean } = {}): string {
  try {
    return encode(value, ensureAscii, new Set());
  } catch {
    // Nested too deep for the stack, or a value that throws when looked at.
  }
  try {
    return quote(inspect(value, { depth: 4, breakLength: Infinity }), ensureAscii);
  } catch {
    return quote(`<${typeName(value)}>`, ensureAscii);
  }
}

/**
 * The attributes naming one running instance of an agent, and what started it, for an agent
 * that can run as several at once, such as a subagent. Empty for any other.
 */
export function instanceAttributes(instance: string | undefined, lineage: readonly string[]): Record<string, string | string[]> {
  if (!instance) return {};
  return { [AGENT_INSTANCE]: instance, [AGENT_LINEAGE]: [...lineage] };
}

/** The payload and its digest as span attributes. Never throws. */
export function payloadAttributes(payload: unknown): Record<string, string> {
  let body = dumps(payload);
  if (body.length > MAX_PAYLOAD_BYTES) body = truncated(body); // ASCII, so its length is its size in bytes
  return { [PAYLOAD]: body, [PAYLOAD_SHA256]: sha256(body) };
}

/**
 * A payload too large to send whole: its size and digest, and as much of its start as fits,
 * as a string. Escaping at most doubles the prefix, so half the limit always fits.
 */
function truncated(body: string): string {
  return dumps({ truncated: true, bytes: body.length, sha256: sha256(body), prefix: body.slice(0, MAX_PAYLOAD_BYTES / 2 - 256) });
}

/** Hex SHA-256 of a string's UTF-8 bytes. */
export function sha256(text: string): string {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

function encode(value: unknown, ascii: boolean, inside: Set<object>): string {
  if (value === null || value === undefined) return "null";
  switch (typeof value) {
    case "string":
      return quote(value, ascii);
    case "boolean":
      return value ? "true" : "false";
    case "number":
      return number(value);
    case "bigint":
      return value.toString();
    case "function":
    case "symbol":
      return quote(text(value), ascii);
  }
  const object = value as object;
  if (inside.has(object)) return quote(inspect(object, { depth: 0, breakLength: Infinity }), ascii);
  inside.add(object);
  try {
    if (typeof (object as { toJSON?: unknown }).toJSON === "function") {
      return encode((object as { toJSON: () => unknown }).toJSON(), ascii, inside);
    }
    if (Array.isArray(object) || object instanceof Set) {
      const items = [...(object as Iterable<unknown>)].map((v) => (v === undefined ? "null" : encode(v, ascii, inside)));
      return `[${items.join(", ")}]`;
    }
    if (ArrayBuffer.isView(object) || object instanceof Error || object instanceof RegExp) {
      return quote(text(object), ascii);
    }
    const entries: [string, unknown][] =
      object instanceof Map ? [...object.entries()].map(([k, v]) => [text(k), v]) : Object.entries(object);
    const items = entries
      .filter(([, v]) => v !== undefined && typeof v !== "function" && typeof v !== "symbol")
      .sort(([a], [b]) => byCodePoint(a, b))
      .map(([k, v]) => `${quote(k, ascii)}: ${encode(v, ascii, inside)}`);
    return `{${items.join(", ")}}`;
  } finally {
    inside.delete(object);
  }
}

function number(value: number): string {
  if (Number.isNaN(value)) return "NaN";
  if (!Number.isFinite(value)) return value > 0 ? "Infinity" : "-Infinity";
  if (Number.isInteger(value)) return BigInt(value).toString();
  // Python's float repr: the same shortest digits, in exponent form below 1e-4. Every number
  // from 2^53 up is whole, so none reaches here.
  const [digits, power] = value.toExponential().split("e");
  const exponent = Number(power);
  if (exponent < -4) return `${digits}e-${String(-exponent).padStart(2, "0")}`;
  return String(value);
}

const ESCAPES: Record<string, string> = { '"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f" };

function quote(value: string, ascii: boolean): string {
  const pattern = ascii ? /["\\\u0000-\u001f\u0080-￿]/g : /["\\\u0000-\u001f]/g;
  return `"${value.replace(pattern, (c) => ESCAPES[c] ?? `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`)}"`;
}

function byCodePoint(a: string, b: string): number {
  const x = [...a];
  const y = [...b];
  for (let i = 0; i < Math.min(x.length, y.length); i++) {
    const d = x[i].codePointAt(0)! - y[i].codePointAt(0)!;
    if (d !== 0) return d;
  }
  return x.length - y.length;
}

function text(value: unknown): string {
  try {
    return String(value);
  } catch {
    try {
      return inspect(value, { depth: 0 });
    } catch {
      return `<${typeName(value)}>`;
    }
  }
}

function typeName(value: unknown): string {
  try {
    return (value as object)?.constructor?.name ?? typeof value;
  } catch {
    return typeof value;
  }
}
