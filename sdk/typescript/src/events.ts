/** Attribute names and helpers for Wake events. See docs/events.md. */

import { createHash } from "node:crypto";
import { inspect } from "node:util";

export const EVENT_ID = "wake.event.id";
export const EVENT_KIND = "wake.event.kind";
export const AGENT_ID = "wake.agent.id";
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

/**
 * JSON for any value, with keys sorted. Never throws.
 *
 * What JSON can hold comes out as Python's json.dumps(value, sort_keys=True) writes it, so
 * a memory label's digest is the same whichever SDK wrote the entry: ", " and ": " between
 * items, keys in code point order, and non-ASCII escaped unless ensureAscii is false. Past
 * that, a value with toJSON() is what it returns, a Map an object, a Set an array, a value
 * inside itself its inspect(), and anything else its String(). JavaScript keeps no
 * difference between 1 and 1.0, so a whole float Python wrote as 1.0 comes out as 1.
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

/** The payload and its digest as span attributes. Never throws. */
export function payloadAttributes(payload: unknown): Record<string, string> {
  const body = dumps(payload);
  return { [PAYLOAD]: body, [PAYLOAD_SHA256]: sha256(body) };
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
  if (Number.isInteger(value) && Math.abs(value) < 1e16) return Object.is(value, -0) ? "-0.0" : value.toFixed(0);
  // Python's float repr: the same shortest digits, in exponent form below 1e-4 and from 1e16.
  const [digits, power] = value.toExponential().split("e");
  const exponent = Number(power);
  if (exponent < -4 || exponent >= 16) {
    return `${digits}e${exponent < 0 ? "-" : "+"}${String(Math.abs(exponent)).padStart(2, "0")}`;
  }
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
