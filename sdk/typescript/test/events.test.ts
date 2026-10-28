/** Payloads are serialized whatever they hold, and what JSON holds comes out as Python's SDK writes it. */

import assert from "node:assert/strict";
import { test } from "node:test";

import { dumps, MAX_PAYLOAD_BYTES, PAYLOAD, PAYLOAD_SHA256, payloadAttributes, sha256 } from "../src/events.ts";

test("what JSON holds comes out as Python's json.dumps(sort_keys=True) writes it", () => {
  const payload = { b: [1, 2.5, null, true], a: { text: "café" } };
  assert.equal(dumps(payload), '{"a": {"text": "caf\\u00e9"}, "b": [1, 2.5, null, true]}');
  assert.equal(dumps(payload, { ensureAscii: false }), '{"a": {"text": "café"}, "b": [1, 2.5, null, true]}');
  assert.equal(dumps("tab\there \u0001   😀"), '"tab\\there \\u0001 \\u2028 \\ud83d\\ude00"');
  assert.equal(dumps([1e-5, 1.5e16, 0.0001, 123.456, 1e100, -2.5e-7]), "[1e-05, 1.5e+16, 0.0001, 123.456, 1e+100, -2.5e-07]");
  assert.equal(dumps([NaN, Infinity, -Infinity]), "[NaN, Infinity, -Infinity]");
  // Keys in code point order, as Python sorts them, not UTF-16's.
  assert.equal(dumps({ "😀": 4, "￿": 5, é: 3, b: 1, a: 2 }, { ensureAscii: false }), '{"a": 2, "b": 1, "é": 3, "￿": 5, "😀": 4}');
});

test("values JSON can't hold become what they say they are", () => {
  const when = new Date(Date.UTC(2026, 9, 27, 9, 30));
  const body = JSON.parse(dumps({ when, map: new Map<unknown, unknown>([[3, "three"], [{ k: 1 }, 2]]), set: new Set([1, 2]), big: 7n, skip: undefined }));
  assert.deepEqual(body, { when: "2026-10-27T09:30:00.000Z", map: { "3": "three", "[object Object]": 2 }, set: [1, 2], big: 7 });
});

test("a value inside itself is its inspect()", () => {
  const loop: Record<string, unknown> = { name: "loop" };
  loop.self = loop;
  const body = JSON.parse(dumps({ payload: loop }));
  assert.equal(body.payload.name, "loop");
  assert.equal(typeof body.payload.self, "string");
});

test("a value whose toString throws is still serialized", () => {
  const odd = {
    toJSON() {
      throw new Error("no");
    },
  };
  const body = JSON.parse(dumps({ 1: odd, a: 1 }));
  assert.equal(typeof body, "string");
  const symbol = JSON.parse(dumps({ s: Symbol("s"), f() {} }));
  assert.deepEqual(symbol, {});
});

test("payload attributes never throw", () => {
  let deep: unknown[] = [];
  for (let i = 0; i < 100_000; i++) deep = [deep];
  const attributes = payloadAttributes({ deep, x: 2 });
  JSON.parse(attributes[PAYLOAD]);
  assert.equal(attributes[PAYLOAD_SHA256].length, 64);
});

test("a payload too large to send whole says so", () => {
  const page = "x".repeat(MAX_PAYLOAD_BYTES + 10);
  const whole = dumps({ output: page });
  const attributes = payloadAttributes({ output: page });
  const body = attributes[PAYLOAD];
  assert.ok(body.length <= MAX_PAYLOAD_BYTES);
  assert.equal(attributes[PAYLOAD_SHA256], sha256(body), "the digest is of what's sent");
  const payload = JSON.parse(body);
  assert.equal(payload.truncated, true);
  assert.equal(payload.bytes, whole.length);
  assert.equal(payload.sha256, sha256(whole), "anyone holding the whole can match it");
  assert.ok(whole.startsWith(payload.prefix) && payload.prefix.length > MAX_PAYLOAD_BYTES / 3);

  // Quotes double when escaped; it still fits.
  const quoted = payloadAttributes({ output: '"'.repeat(MAX_PAYLOAD_BYTES) })[PAYLOAD];
  assert.ok(quoted.length <= MAX_PAYLOAD_BYTES);
  assert.equal(JSON.parse(quoted).truncated, true);
  assert.ok(!("truncated" in JSON.parse(payloadAttributes({ output: "fine" })[PAYLOAD])));
});
