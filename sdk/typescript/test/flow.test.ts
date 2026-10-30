/**
 * Flow policies in the agent's own process: the policy read as wake-proxy reads it, the
 * decisions the shared vectors pin, which wake-proxy and the Python SDK make alike, and what
 * entered each agent, kept over the policy's window.
 */

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

import {
  CARRIED,
  entered,
  Entered,
  Every,
  MAX_AGENTS,
  MAX_ORIGINS,
  OVERFLOWED,
  parsePolicy,
  SATURATED,
  UNKNOWN,
  UNREADABLE,
  type FlowCall,
  type Seen,
} from "../src/flow.ts";
import { log } from "../src/log.ts";
import { flowRefusal, type Halts } from "../src/stop.ts";

type Case = {
  name: string;
  policy: string;
  caller: string;
  lineage: string[];
  tool?: string;
  receiver?: string;
  store?: string;
  origins: string[] | "*";
  denied_by: string | null;
  kept: string[];
};

/** Shared with wake-proxy's tests and the Python SDK's, so all three decide alike. */
const VECTORS: { policies: Record<string, unknown>; cases: Case[]; invalid: { name: string; policy: unknown }[] } = JSON.parse(
  await readFile(new URL("../../testdata/flow_policy.json", import.meta.url), "utf8"),
);

test("decides as the shared vectors say", () => {
  assert.ok(VECTORS.cases.length >= 20);
  assert.ok(VECTORS.cases.some((c) => c.store !== undefined), "changes to memory stores among them");
  for (const c of VECTORS.cases) {
    const policy = parsePolicy(VECTORS.policies[c.policy]);
    assert.equal(policy.unreadable, null, c.name);
    assert.equal([c.tool, c.receiver, c.store].filter((x) => x !== undefined).length, 1, `${c.name}: a tool call, an agent reached or a store changed`);
    const seen: Seen = c.origins === "*" ? new Every("every origin") : c.origins;
    const call: FlowCall = { caller: c.caller, lineage: c.lineage, tool: c.tool, receiver: c.receiver, store: c.store };
    const denial = policy.deciding(call, seen);
    assert.equal(denial?.halt ?? null, c.denied_by, c.name);
    assert.deepEqual(denial?.origins ?? [], c.kept, c.name);
  }
});

test("no rule naming stores decides a tool call or an agent reached, nor any other rule a change to a store", () => {
  for (const c of VECTORS.cases) {
    const policy = parsePolicy(VECTORS.policies[c.policy]);
    const naming = new Set(policy.rules.filter((r) => r.stores.length > 0).map((r) => `policy:${r.id}`));
    const others = new Set(policy.rules.filter((r) => r.stores.length === 0).map((r) => `policy:${r.id}`));
    const seen: Seen = c.origins === "*" ? new Every("every origin") : c.origins;
    // The case's own call, and the same caller's calls of every other kind, named for the case's target.
    const target = c.tool ?? c.receiver ?? c.store!;
    const lineage = c.lineage;
    for (const call of [
      { caller: c.caller, lineage, tool: target },
      { caller: c.caller, lineage, receiver: target },
      { caller: c.caller, lineage, tool: "write_memory" },
    ]) {
      const halt = policy.deciding(call, seen)?.halt;
      assert.ok(halt === undefined || !naming.has(halt), `${c.name}: ${JSON.stringify(call)} denied by ${halt}`);
    }
    const halt = policy.deciding({ caller: c.caller, lineage, store: target }, seen)?.halt;
    assert.ok(halt === undefined || !others.has(halt), `${c.name}: a change to ${target} denied by ${halt}`);
  }
});

test("refuses every call under a policy the shared vectors say is invalid", () => {
  assert.ok(VECTORS.invalid.length > 0);
  for (const c of VECTORS.invalid) {
    const policy = parsePolicy(c.policy);
    assert.notEqual(policy.unreadable, null, c.name);
    for (const call of [{ caller: "billing", tool: "pay" }, { caller: "support", receiver: "billing" }, { caller: "research", store: "facts" }]) {
      assert.deepEqual(policy.deciding(call, []), { halt: UNREADABLE, origins: [], unreadable: true }, c.name);
    }
  }
});

test("reads the policy as wake-proxy serves it", () => {
  const served = {
    version: "0".repeat(64),
    window_seconds: 600,
    rules: [{ id: "no-pay-after-web", callers: ["billing"], tools: ["pay"], forbid_origins: ["tool:fetch_page"] }],
  };
  const policy = parsePolicy(served);
  assert.equal(policy.unreadable, null);
  assert.equal(policy.windowSeconds, 600);
  assert.deepEqual(policy.rules, [
    { id: "no-pay-after-web", callers: ["billing"], receivers: [], path_prefixes: [], tools: ["pay"], stores: [], forbid_origins: ["tool:fetch_page"], allow_origins: [] },
  ], "a list left out is empty, as wake-proxy serves it");
  const stores = parsePolicy({ rules: [{ id: "no-web-into-facts", stores: ["facts", "customers/*"], forbid_origins: ["tool:fetch_page"] }] });
  assert.equal(stores.unreadable, null);
  assert.deepEqual(stores.rules[0].stores, ["facts", "customers/*"], "a rule may name memory stores");
  assert.equal(parsePolicy({}).windowSeconds, 900, "the window wake-proxy takes when none is set");
  assert.deepEqual(parsePolicy({}).rules, []);
  assert.match(parsePolicy({ unreadable: true }).unreadable!, /can't use its policy file/, "what wake-proxy serves when it can't use its file");
  // Types are checked as strictly as wake-proxy checks them.
  for (const bad of [
    null,
    [],
    "{}",
    { version: 1, rules: [] },
    { window_seconds: null },
    { window_seconds: "900" },
    { rules: null },
    { rules: [{ id: "x", forbid_origins: null }] },
    { rules: [{ id: "x", forbid_origins: ["*", 5] }] },
    { rules: [{ id: 5, forbid_origins: ["*"] }] },
    { rules: [{ forbid_origins: ["*"] }] },
    { rules: [{ id: "x", forbid_origins: ["tool:**"] }] },
    { rules: [{ id: "x", receivers: ["bill*"], forbid_origins: ["*"] }] },
    { rules: [{ id: "x", path_prefixes: ["/a*"], forbid_origins: ["*"] }] },
    { rules: [{ id: "x", tools: [""], forbid_origins: ["*"] }] },
    { rules: [{ id: "é", forbid_origins: ["*"] }] },
    { rules: [{ id: "x", stores: "facts", forbid_origins: ["*"] }] },
    { rules: [{ id: "x", stores: ["facts", 5], forbid_origins: ["*"] }] },
    { rules: [{ id: "x", stores: ["*s"], forbid_origins: ["*"] }] },
    { rules: [{ id: "x", stores: ["facts"] }] },
  ]) {
    assert.notEqual(parsePolicy(bad).unreadable, null, JSON.stringify(bad));
  }
  assert.match(parsePolicy({ rules: [{ id: "x", stores: ["facts"], tools: ["pay"], forbid_origins: ["*"] }] }).unreadable!, /names stores with tools, receivers or path_prefixes/);
});

test("a pattern is a prefix when it ends in *, and * is every origin", () => {
  const policy = parsePolicy({ rules: [{ id: "r", tools: ["wire_*", "pay"], forbid_origins: ["*"] }] });
  for (const [tool, denied] of [["wire_eur", true], ["wire_", true], ["wire", false], ["pay", true], ["payout", false]] as const) {
    assert.equal(policy.deciding({ caller: "billing", tool }, ["anything:at-all"]) !== undefined, denied, tool);
  }
  const stores = parsePolicy({ rules: [{ id: "r", stores: ["customers/*", "facts"], forbid_origins: ["*"] }] });
  for (const [store, denied] of [["customers/42", true], ["customers/", true], ["customers", false], ["facts", true], ["facts/1", false], ["*", false]] as const) {
    assert.equal(stores.deciding({ caller: "billing", store }, ["anything:at-all"]) !== undefined, denied, store);
  }
  assert.equal(parsePolicy({ rules: [{ id: "r", stores: ["*"], forbid_origins: ["*"] }] }).deciding({ caller: "a", store: "any/store" }, ["x"])?.halt, "policy:r", "* alone is every store");
});

test("a caller Wake can't tell has every origin, and any rule's callers could be it", () => {
  const halts = serving({
    rules: [
      { id: "scratch-from-billing", callers: ["flow-billing"], stores: ["scratch"], allow_origins: ["tool:lookup_order"] },
      { id: "ledger-from-billing", callers: ["flow-billing"], stores: ["ledger"], allow_origins: ["tool:lookup_order"] },
    ],
  });
  assert.deepEqual(flowRefusal(halts, "the agent acting", { caller: undefined, store: "ledger" }, "this memory write wasn't made"), [
    "policy:ledger-from-billing",
    "Wake's flow policy (policy:ledger-from-billing) holds the agent acting back, because Wake can't tell which agent is acting, so this memory write wasn't made.",
    { origins: ["*"], every_origin: UNKNOWN },
  ]);
  assert.equal(UNKNOWN, "Wake can't tell which agent is acting");
  assert.equal(flowRefusal(halts, "the agent acting", { caller: undefined, store: "notes" }), undefined, "unless no rule names the store");
  assert.equal(flowRefusal(halts, "flow-support", { caller: "flow-support", store: "ledger" }), undefined, "a caller Wake can tell is held only by the rules for it");
});

/** A tracker on a clock the test moves, in seconds. */
function tracker(windowSeconds = 900) {
  const time = { seconds: 0 };
  return { time, e: new Entered({ windowSeconds, clock: () => time.seconds * 1000 }) };
}

test("each origin counts for a window from when it last entered, newest first", () => {
  const { time, e } = tracker();
  e.note("billing", "tool:fetch_page");
  time.seconds = 10;
  e.note("billing", "agent:research");
  time.seconds = 20;
  assert.deepEqual(e.seen("billing"), ["agent:research", "tool:fetch_page"]);
  // Entering again later keeps it on.
  time.seconds = 800;
  e.note("billing", "tool:fetch_page");
  time.seconds = 905;
  assert.deepEqual(e.seen("billing"), ["tool:fetch_page", "agent:research"]);
  time.seconds = 910;
  assert.deepEqual(e.seen("billing"), ["tool:fetch_page", "agent:research"], "a whole window, to the end");
  time.seconds = 910.001;
  assert.deepEqual(e.seen("billing"), ["tool:fetch_page"], "and not past it");
  time.seconds = 1_800.001;
  assert.deepEqual(e.seen("billing"), []);
  assert.deepEqual(e.seen("support"), [], "nothing entered another agent");
  // Origins that entered at once come in order by name.
  e.note("support", "tool:z");
  e.note("support", "agent:b");
  e.note("support", "tool:b");
  assert.deepEqual(e.seen("support"), ["agent:b", "tool:b", "tool:z"]);
});

test("a policy's window is the one that counts", () => {
  const { time, e } = tracker();
  e.note("billing", "tool:fetch_page");
  time.seconds = 100;
  e.setWindow(60);
  assert.deepEqual(e.seen("billing"), []);
  e.setWindow(900);
  assert.deepEqual(e.seen("billing"), ["tool:fetch_page"]);
});

test("past its bound of origins an agent has every origin, for a window", () => {
  const { time, e } = tracker();
  for (let i = 0; i <= MAX_ORIGINS; i++) e.note("a", `tool:t${i}`);
  time.seconds = 1;
  assert.deepEqual(e.seen("a"), new Every(SATURATED));
  assert.equal(SATURATED, "more origins entered it than Wake keeps");
  time.seconds = 900.001;
  assert.deepEqual(e.seen("a"), [], "once the window has passed with nothing new, it's known again");
  // Origins older than the window don't count toward the bound.
  time.seconds = 1_000;
  for (let i = 0; i < MAX_ORIGINS; i++) e.note("a", `tool:later${i}`);
  const seen = e.seen("a");
  assert.ok(Array.isArray(seen) && seen.length === MAX_ORIGINS);
  // An origin it has already is no more.
  e.note("a", "tool:later0");
  assert.ok(Array.isArray(e.seen("a")));
});

test("past its bound of agents, one it can't keep has every origin", () => {
  const { time, e } = tracker();
  for (let i = 0; i < MAX_AGENTS; i++) e.note(`agent-${i}`, "tool:t");
  time.seconds = 1;
  e.note("one-too-many", "tool:t");
  time.seconds = 2;
  assert.deepEqual(e.seen("one-too-many"), new Every(OVERFLOWED));
  assert.equal(OVERFLOWED, "too many agents to remember this one");
  assert.deepEqual(e.seen("never-seen"), new Every(OVERFLOWED), "any agent not kept could be it");
  assert.deepEqual(e.seen("agent-0"), ["tool:t"], "the ones kept are still known");
  // Agents nothing entered for a window make room.
  time.seconds = 910;
  e.note("newcomer", "tool:t");
  time.seconds = 911;
  assert.deepEqual(e.seen("newcomer"), ["tool:t"]);
  assert.deepEqual(e.seen("never-seen"), [], "and a window after the last that couldn't be kept, the rest are known again");
});

test("content carries what entered the agent it comes from, keeping each origin's time", () => {
  const { time, e } = tracker();
  e.note("research", "tool:fetch_page");
  time.seconds = 5;
  e.note("research", "agent:billing");
  // Research's reply to billing brings the page, as of when research fetched it, and not
  // billing's own message back to it.
  time.seconds = 100;
  e.message("research", "billing");
  time.seconds = 101;
  assert.deepEqual(e.seen("billing"), ["agent:research", "tool:fetch_page"]);
  // Passing it on never makes it newer: it leaves each of them when it leaves research.
  time.seconds = 200;
  e.carry("billing", "support");
  assert.deepEqual(e.seen("support"), ["agent:research", "tool:fetch_page"]);
  time.seconds = 800;
  e.carry("support", "billing");
  time.seconds = 850;
  e.carry("billing", "research");
  time.seconds = 900.001;
  for (const agent of ["research", "billing", "support"]) {
    assert.ok(!(e.seen(agent) as string[]).includes("tool:fetch_page"), agent);
  }
  // Nothing enters an agent from itself.
  e.message("billing", "billing");
  assert.ok(!(e.seen("billing") as string[]).includes("agent:billing"));
});

test("content from an agent with every origin gives every origin, until that agent's ends", () => {
  const { time, e } = tracker();
  for (let i = 0; i <= MAX_ORIGINS; i++) e.note("a", `tool:t${i}`);
  time.seconds = 500;
  e.carry("a", "b");
  time.seconds = 600;
  e.message("b", "c");
  time.seconds = 601;
  assert.deepEqual(e.seen("c"), new Every(CARRIED));
  assert.equal(CARRIED, "a message or reply reached it from one with every origin");
  time.seconds = 700;
  e.carry("c", "a");
  time.seconds = 900.001;
  assert.deepEqual(e.seen("a"), [], "nor kept alive by passing it around");
  assert.deepEqual(e.seen("c"), ["agent:b"], "only what entered it since is known");
});

/** Rules that serve this policy, as a Quarantine does. */
function serving(policy: unknown): Halts {
  const parsed = parsePolicy(policy);
  return { halted: () => null, flow: () => parsed };
}

/** What a log.warn says while fn runs. */
function warnings(fn: () => void): string {
  const said: string[] = [];
  const warn = log.warn;
  log.warn = (message) => void said.push(message);
  try {
    fn();
  } finally {
    log.warn = warn;
  }
  return said.join("\n");
}

test("a refusal says which rule, and what it kept, as a halt's says which halt", () => {
  const halts = serving({ window_seconds: 600, rules: [{ id: "no-pay-after-web", callers: ["flow-billing"], tools: ["pay", "wire_*"], forbid_origins: ["tool:fetch_page", "agent:*"] }] });
  const billing = { caller: "flow-billing", tool: "pay" };
  assert.equal(flowRefusal(halts, "flow-billing", billing), undefined, "nothing entered it yet");
  entered.note("flow-billing", "tool:search");
  entered.note("flow-billing", "tool:fetch_page");
  assert.deepEqual(flowRefusal(halts, "flow-billing", billing), [
    "policy:no-pay-after-web",
    "Wake's flow policy (policy:no-pay-after-web) holds flow-billing back after tool:fetch_page, so this tool call wasn't run.",
    { origins: ["tool:fetch_page"] },
  ]);
  entered.message("flow-research", "flow-billing");
  assert.equal(
    flowRefusal(halts, "flow-billing#run-1", { caller: "flow-billing", tool: "wire_eur" }, "the payment wasn't made")?.[1],
    "Wake's flow policy (policy:no-pay-after-web) holds flow-billing#run-1 back after agent:flow-research, tool:fetch_page, so the payment wasn't made.",
    "who's told, as for a halt, and what the rule kept, newest first",
  );
  assert.equal(flowRefusal(halts, "flow-billing", { caller: "flow-billing", tool: "lookup_order" }), undefined, "a tool the rule doesn't name");
  assert.equal(flowRefusal(halts, "flow-support", { caller: "flow-support", tool: "pay" }), undefined, "a caller the rule doesn't name");
});

test("a sub-agent is held to the rules naming its chain, an instance by its agent", () => {
  const halts = serving({ rules: [{ id: "lead-quiet-after-web", callers: ["flow-lead"], forbid_origins: ["tool:fetch_page"] }] });
  entered.note("flow-lead/writer", "tool:fetch_page");
  const writer = { caller: "flow-lead/writer", tool: "Bash" };
  assert.equal(flowRefusal(halts, "flow-lead/writer", writer), undefined);
  assert.equal(flowRefusal(halts, "flow-lead/writer", { ...writer, lineage: ["flow-lead"] })?.[0], "policy:lead-quiet-after-web");
  assert.equal(flowRefusal(halts, "flow-lead/writer", { ...writer, lineage: ["flow-root", "flow-lead#toolu_1"] })?.[0], "policy:lead-quiet-after-web");
});

test("every origin, a policy that can't be read and a check that fails refuse too", () => {
  const halts = serving({ rules: [{ id: "no-pay-after-web", tools: ["pay"], forbid_origins: ["tool:fetch_page"] }] });
  for (let i = 0; i <= MAX_ORIGINS; i++) entered.note("flow-busy", `tool:t${i}`);
  assert.deepEqual(flowRefusal(halts, "flow-busy", { caller: "flow-busy", tool: "pay" }), [
    "policy:no-pay-after-web",
    "Wake's flow policy (policy:no-pay-after-web) holds flow-busy back, because more origins entered it than Wake keeps, so this tool call wasn't run.",
    { origins: ["*"], every_origin: SATURATED },
  ]);
  const handoff = { caller: "flow-anyone", receiver: "billing" };
  const outcome = "the handoff to billing wasn't made";
  const unreadable = [UNREADABLE, "Wake's flow policy can't be read, so the handoff to billing wasn't made.", { origins: [] }];
  assert.deepEqual(flowRefusal(serving({ unreadable: true }), "flow-anyone", handoff, outcome), unreadable);
  const broken: Halts = {
    halted: () => null,
    flow: () => {
      throw new Error("the rules can't be read");
    },
  };
  const said = warnings(() => assert.deepEqual(flowRefusal(broken, "flow-anyone", handoff, outcome), unreadable));
  assert.match(said, /couldn't check the flow policy for flow-anyone; refusing/);
});

test("without a policy nothing is refused", () => {
  entered.note("flow-free", "tool:fetch_page");
  const call = { caller: "flow-free", tool: "pay" };
  assert.equal(flowRefusal(undefined, "flow-free", call), undefined);
  assert.equal(flowRefusal({ halted: () => null }, "flow-free", call), undefined, "rules that don't say");
  assert.equal(flowRefusal({ halted: () => null, flow: () => undefined }, "flow-free", call), undefined, "a proxy that enforces none");
  assert.equal(flowRefusal(serving({ rules: [] }), "flow-free", call), undefined, "an empty policy");
});
