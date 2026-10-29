/** A sub-agent's AWS session is named as IAM takes it, the same way here as in the brake. */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

import { sessionName } from "../src/aws.ts";

const VECTORS: [string, string][] = JSON.parse(await readFile(new URL("../../testdata/aws_session_names.json", import.meta.url), "utf8"));

test("a session is named as the brake names it", () => {
  for (const [identity, name] of VECTORS) {
    assert.equal(sessionName(identity), name, identity);
    assert.match(name, /^[A-Za-z0-9_+=,.@-]{2,64}$/, "IAM takes it");
  }
});

test("two identities never share a name", () => {
  assert.notEqual(sessionName("support/researcher#toolu_1"), sessionName("support-researcher-toolu_1"));
});
