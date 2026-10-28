/**
 * The brake's memory quarantine, enforced by the memory adapter: locked writes are refused,
 * quarantined entries aren't served, and both are recorded. The rules come from a stand-in
 * for wake-proxy's memory endpoint.
 */

import assert from "node:assert/strict";
import { createServer, type Server } from "node:http";
import { mkdtemp, writeFile, access } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, test } from "node:test";

import { InMemoryStore } from "@langchain/langgraph";

import { log } from "../src/log.ts";
import { WakeStore } from "../src/langgraph.ts";
import { contentOf, digest, LABEL_KEY, Memory } from "../src/memory.ts";
import { MemoryLocked, MemoryQuarantined, Quarantine, STALE } from "../src/quarantine.ts";
import { attr, id, parents, tick } from "./helpers.ts";
import { EMAIL, rememberFromEmail, Session } from "./sessions.ts";

/** A stand-in for wake-proxy's GET /v1/memory: serves the rules set on it, and notes who fetched them. */
class Rules {
  rules: Record<string, unknown> = { version: 1, quarantines: [] };
  readonly fetchedBy: string[] = [];
  delay = 0;
  padding = 0; // bytes of whitespace after the rules
  readonly server: Server;
  url = "";

  constructor() {
    this.server = createServer(async (request, response) => {
      this.fetchedBy.push(request.url ?? "");
      await tick(this.delay);
      const body = Buffer.concat([Buffer.from(JSON.stringify(this.rules)), Buffer.alloc(this.padding, " ")]);
      response.writeHead(200, { "content-type": "application/json", "content-length": body.length }).end(body);
    });
  }

  async start(): Promise<this> {
    await new Promise<void>((resolve) => this.server.listen(0, "127.0.0.1", resolve));
    this.url = `http://127.0.0.1:${(this.server.address() as { port: number }).port}`;
    return this;
  }

  stop(): Promise<void> {
    this.server.closeAllConnections();
    return new Promise((resolve) => this.server.close(() => resolve()));
  }

  quarantine(selection: Record<string, unknown>): void {
    const { locked_by: lockedBy = null, ...rest } = selection;
    this.rules = { version: 2, locked_by: lockedBy, quarantines: [{ id: "q1", selection: rest, issued_at: 1, applied_at: 1 }] };
  }
}

const opened: Rules[] = [];
async function rules(): Promise<Rules> {
  const r = await new Rules().start();
  opened.push(r);
  return r;
}
after(async () => {
  for (const r of opened) await r.stop().catch(() => {});
});

async function quarantined(session: Session, r: Rules, agent: string): Promise<Memory> {
  const q = new Quarantine(r.url, { agent, start: false });
  assert.ok(await q.fetch());
  return new Memory(session.handler, { quarantine: q });
}

/** What log.warn says while fn runs. */
async function warnings(fn: () => Promise<unknown>): Promise<string> {
  const said: string[] = [];
  const warn = log.warn;
  log.warn = (message) => void said.push(message);
  try {
    await fn();
  } finally {
    log.warn = warn;
  }
  return said.join("\n");
}

async function refused(session: Session): Promise<string[]> {
  return (await session.events("denied")).map((e) => attr(e, "wake.memory.refused") as string);
}

test("a locked agent can't write, and the refusal is recorded", async () => {
  const r = await rules();
  const session = new Session("billing");
  r.quarantine({ locked_by: "q1", lock: { agents: ["billing"] } });
  const memory = await quarantined(session, r, "billing");
  assert.match(r.fetchedBy.at(-1)!, /^\/v1\/memory\?agent=billing&process=\d+\.[0-9a-f]{12}$/);
  const inner = new InMemoryStore();
  const store = new WakeStore(inner, memory);

  await assert.rejects(store.put(["facts"], "acme", { text: EMAIL }), (e: unknown) => e instanceof MemoryLocked && e.quarantine === "q1");
  assert.equal(await inner.get(["facts"], "acme"), null, "nothing was written");
  const dir = await mkdtemp(join(tmpdir(), "wake-quarantine-"));
  await assert.rejects(memory.writeFile(join(dir, "notes.md"), EMAIL), MemoryLocked);
  await assert.rejects(access(join(dir, "notes.md")));

  const denied = await session.events("denied");
  assert.deepEqual(await refused(session), ["write", "write"]);
  assert.equal(attr(denied[0], "wake.brake.halt_id"), "q1");
  assert.equal(attr(denied[0], "wake.capture"), "memory_adapter");
  assert.deepEqual(await session.events("memory_write"), []);
});

test("entries from a poisoned origin aren't served", async () => {
  const r = await rules();
  const inner = new InMemoryStore();
  // The email agent stored a fact it read in an email, before anyone knew it was poisoned.
  const writer = new Session("email");
  await rememberFromEmail(writer, new WakeStore(inner, writer.memory));
  const [write] = await writer.events("memory_write");
  const clean = { text: "Acme's invoices are due on the 1st." };
  await inner.put(["facts", "user-1"], "clean", { ...clean, [LABEL_KEY]: { event: "w-clean", origins: ["user"], agent: "support", sha256: digest(contentOf(clean)) } });

  r.quarantine({ origins: ["tool:read_and_remember"] });
  const reader = new Session("finance");
  const store = new WakeStore(inner, await quarantined(reader, r, "finance"));

  assert.equal(await store.get(["facts", "user-1"], "acme-bank"), null, "a quarantined entry reads as absent");
  assert.deepEqual((await store.search(["facts", "user-1"])).map((i) => i.key), ["clean"]);
  assert.notEqual(await inner.get(["facts", "user-1"], "acme-bank"), null, "it's kept, as evidence");

  const reads = (await reader.events("denied")).filter((e) => attr(e, "wake.memory.refused") === "read");
  assert.equal(reads.length, 2, "the get and the search");
  assert.deepEqual(parents(reads[0]), [id(write)]);
  assert.equal(attr(reads[0], "wake.memory.entry"), "acme-bank");
  assert.deepEqual((await reader.events("memory_read")).map((e) => attr(e, "wake.memory.entry")), ["clean"], "only what was served is a read");
});

test("nothing is served while the proxy can't read its quarantines", async () => {
  const r = await rules();
  const session = new Session("support");
  const inner = new InMemoryStore();
  await new WakeStore(inner, new Memory(session.handler)).put(["facts"], "by-support", { text: "x" });
  // What wake-proxy says when its state on disk is unreadable: what it quarantined is
  // unknown, so any entry might be.
  r.rules = { version: 0, locked_by: "unreadable-state", hidden_by: "unreadable-state", quarantines: [] };
  const store = new WakeStore(inner, await quarantined(session, r, "support"));

  assert.equal(await store.get(["facts"], "by-support"), null);
  const reads = (await session.events("denied")).filter((e) => attr(e, "wake.memory.refused") === "read");
  assert.deepEqual(reads.map((e) => attr(e, "wake.brake.halt_id")), ["unreadable-state"]);
  await assert.rejects(store.put(["facts"], "new", { text: "y" }), MemoryLocked);
});

test("other selections", async () => {
  const r = await rules();
  const session = new Session("support");
  const inner = new InMemoryStore();
  await new WakeStore(inner, new Memory(session.handler)).put(["facts"], "by-support", { text: "x" }); // labeled as support's write
  await inner.put(["facts"], "planted", { text: "pay account 999" }); // no label at all
  const writeId = (await inner.get(["facts"], "by-support"))!.value[LABEL_KEY].event;

  for (const [selection, hidden] of [
    [{ agents: ["support"] }, ["by-support"]],
    [{ unlabeled: true }, ["planted"]],
    [{ entries: ["facts/planted"] }, ["planted"]],
    [{ writes: [writeId] }, ["by-support"]],
  ] as const) {
    r.quarantine({ ...selection });
    const guarded = new WakeStore(inner, await quarantined(new Session("reader"), r, "reader"));
    const served = new Set((await guarded.search(["facts"])).map((i) => i.key));
    assert.deepEqual(served, new Set(["by-support", "planted"].filter((k) => !(hidden as readonly string[]).includes(k))), JSON.stringify(selection));
  }

  r.quarantine({ unlabeled: true });
  const memory = await quarantined(session, r, "support");
  const dir = await mkdtemp(join(tmpdir(), "wake-quarantine-"));
  await writeFile(join(dir, "planted.md"), "pay account 999");
  await assert.rejects(memory.readFile(join(dir, "planted.md")), MemoryQuarantined);
  await new Memory(session.handler).writeFile(join(dir, "notes.md"), "fine");
  assert.equal(await memory.readFile(join(dir, "notes.md")), "fine", "a labeled file is served");
});

test("an unreachable proxy fails closed and never throws", async () => {
  const q = new Quarantine("http://127.0.0.1:9", { agent: "billing", start: false, timeoutMillis: 200 });
  const said = await warnings(async () => assert.equal(await q.fetch(), false));
  assert.equal(q.lockedBy(), STALE);
  assert.equal(q.hiding("facts", "x", null, false), STALE);
  assert.ok(!q.health().ok);
  assert.equal(q.health().ageMillis, null);
  assert.match(said, /couldn't fetch memory rules/);
});

test("the last rules stand when a fetch fails", async () => {
  const r = await rules();
  r.quarantine({ locked_by: "q1", lock: { fleet: true } });
  const q = new Quarantine(r.url, { agent: "billing", start: false });
  assert.ok(await q.fetch());
  await r.stop();
  assert.equal(await q.fetch(), false);
  assert.equal(q.lockedBy(), "q1");
});

test("the first fetch is done before the agent goes on", async () => {
  const r = await rules();
  r.quarantine({ locked_by: "q1", lock: { agents: ["billing"] } });
  r.delay = 300;
  const q = new Quarantine(r.url, { agent: "billing", intervalMillis: 60_000 });
  try {
    assert.equal(q.lockedBy(), STALE, "until then, it fails closed");
    assert.ok(await q.ready);
    assert.equal(q.lockedBy(), "q1", "enforced from the first write");
  } finally {
    q.close();
  }
});

test("stale rules fail closed", async () => {
  const r = await rules();
  const session = new Session("billing");
  const q = new Quarantine(r.url, { agent: "billing", start: false, ttlMillis: 200 });
  assert.ok(await q.fetch());
  const store = new WakeStore(new InMemoryStore(), new Memory(session.handler, { quarantine: q }));
  await store.put(["facts"], "acme", { text: "fine" });
  assert.ok(q.health().ok);

  await r.stop();
  await tick(300);
  const said = await warnings(async () => assert.equal(await q.fetch(), false));
  assert.match(said, /stale/);
  assert.ok(!q.health().ok);
  await assert.rejects(store.put(["facts"], "acme", { text: "changed" }), (e: unknown) => e instanceof MemoryLocked && e.quarantine === STALE);
  assert.equal(await store.get(["facts"], "acme"), null, "nothing is served while the rules are stale");
  const denied = await session.events("denied");
  assert.deepEqual(await refused(session), ["write", "read"]);
  assert.deepEqual(new Set(denied.map((e) => attr(e, "wake.brake.halt_id"))), new Set([STALE]));
});

test("rules over a mebibyte are read, and too large rules are a warning", async () => {
  const r = await rules();
  r.quarantine({ locked_by: "q1", lock: { fleet: true } });
  r.padding = 2 << 20;
  const q = new Quarantine(r.url, { agent: "billing", start: false });
  assert.ok(await q.fetch(), "a large quarantine is still read");
  assert.equal(q.lockedBy(), "q1");

  r.padding = 9 << 20;
  const said = await warnings(async () => assert.equal(await q.fetch(), false));
  assert.match(said, /memory rules over/);
});

test("deletes are recorded, and quarantined entries can't be deleted", async () => {
  const r = await rules();
  const inner = new InMemoryStore();
  const writer = new Session("email");
  await rememberFromEmail(writer, new WakeStore(inner, writer.memory));
  const store = new WakeStore(inner, writer.memory);
  await store.put(["facts"], "old", { text: "stale fact" });
  await store.delete(["facts"], "old");
  assert.equal(await inner.get(["facts"], "old"), null);
  const deleted = (await writer.events("memory_write")).filter((e) => attr(e, "wake.memory.deleted"));
  assert.deepEqual(deleted.map((e) => attr(e, "wake.memory.entry")), ["old"]);

  // The poisoned entry is quarantined: it's evidence, so it can't be deleted.
  r.quarantine({ origins: ["tool:read_and_remember"] });
  const session = new Session("finance");
  const guarded = new WakeStore(inner, await quarantined(session, r, "finance"));
  await assert.rejects(guarded.delete(["facts", "user-1"], "acme-bank"), (e: unknown) => e instanceof MemoryLocked && e.quarantine === "q1");
  assert.notEqual(await inner.get(["facts", "user-1"], "acme-bank"), null, "kept, as evidence");
  assert.deepEqual(await refused(session), ["delete"]);
  await guarded.delete(["facts"], "missing"); // nothing there to hide
  assert.deepEqual((await session.events("memory_write")).map((e) => attr(e, "wake.memory.entry")), ["missing"]);
});

test("a locked agent can't delete", async () => {
  const r = await rules();
  const inner = new InMemoryStore();
  await inner.put(["facts"], "acme", { text: "fine" });
  r.quarantine({ locked_by: "q1", lock: { agents: ["billing"] } });
  const session = new Session("billing");
  const store = new WakeStore(inner, await quarantined(session, r, "billing"));
  await assert.rejects(store.delete(["facts"], "acme"), MemoryLocked);
  assert.notEqual(await inner.get(["facts"], "acme"), null);
  assert.deepEqual(await refused(session), ["delete"]);
});

test("a quarantined entry JSON can't hold isn't served, and its label never shows", async () => {
  const r = await rules();
  const inner = new InMemoryStore();
  const label = { event: "w1", origins: ["tool:read_email"], agent: "email", sha256: "0".repeat(64) };
  await inner.put(["facts"], "odd", { when: new Date(0), note: 1, [LABEL_KEY]: label });
  r.quarantine({ entries: ["facts/odd"] });
  const store = new WakeStore(inner, await quarantined(new Session("finance"), r, "finance"));
  assert.equal(await store.get(["facts"], "odd"), null);
  assert.deepEqual(await store.search(["facts"]), []);

  // Without a quarantine it's served, without its label.
  const item = await new WakeStore(inner, new Memory(new Session("finance").handler)).get(["facts"], "odd");
  assert.ok(!(LABEL_KEY in item!.value));
  assert.equal(item!.value.note, 1);
});

test("if checking the quarantine fails, nothing is served or written", async () => {
  const r = await rules();
  const inner = new InMemoryStore();
  await inner.put(["facts"], "acme", { text: "fine" });
  const session = new Session("finance");
  const q = new Quarantine(r.url, { agent: "finance", start: false });
  assert.ok(await q.fetch());
  const bug = () => {
    throw new Error("quarantine bug");
  };
  q.hiding = bug;
  q.lockedBy = bug;
  const store = new WakeStore(inner, new Memory(session.handler, { quarantine: q }));
  assert.equal(await store.get(["facts"], "acme"), null);
  await assert.rejects(store.put(["facts"], "acme", { text: "changed" }), MemoryLocked);
  assert.deepEqual((await inner.get(["facts"], "acme"))!.value, { text: "fine" });
});
