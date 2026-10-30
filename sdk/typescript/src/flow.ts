/**
 * Flow policies in the agent's own process (docs/flow-everywhere.md): the rules wake-proxy
 * enforces on the calls it sees, applied here to the calls it never does, such as a local
 * tool, a handoff or a sub-agent started in the same process.
 *
 * wake-proxy serves the policy in force with the rules a Quarantine fetches, as their flow
 * field. parsePolicy reads it as wake-proxy reads its policy file (proxy/src/policy.rs): one
 * that isn't valid can't be used, and refuses every call under UNREADABLE. A check decides on
 * what entered the calling agent within the policy's window, which Entered keeps for every
 * agent in the process, as wake-proxy's origins.rs does for what passes it: a tool's result,
 * another agent's message, handoff or result, and what a memory read brings in. Never what
 * the user asked: their prompt isn't an origin. Rules naming memory stores hold only the
 * changes the memory adapter makes to them, which never pass the proxy (memory.ts).
 * sdk/testdata/flow_policy.json pins the decisions, which wake-proxy and the Python SDK make
 * alike.
 *
 * Like the halts, this runs in the agent's own process, so it holds only while the agent's
 * code does. wake-proxy, and a network that lets agents out only through it, stay the
 * backstop.
 */

import { log } from "./log.ts";

/** The halt id every call is refused under while the policy can't be used. */
export const UNREADABLE = "policy:unreadable-policy";
/** The window when a policy doesn't set one, in seconds. */
export const DEFAULT_WINDOW_SECONDS = 900;
/** The longest window a policy may set, in seconds. */
export const MAX_WINDOW_SECONDS = 86_400;
/** The most distinct origins kept for one agent within a window. Past it, the agent has every origin until the window passes. */
export const MAX_ORIGINS = 256;
/** The most agents kept at once. Past it, an agent not kept has every origin. */
export const MAX_AGENTS = 10_000;
/** What content read from memory enters as when no label vouches for it. */
export const UNLABELED = "memory:unlabeled";

/** Why an agent has every origin past MAX_ORIGINS. */
export const SATURATED = "more origins entered it than Wake keeps";
/** Why an agent Entered couldn't keep has every origin, past MAX_AGENTS. */
export const OVERFLOWED = "too many agents to remember this one";
/** Why an agent has every origin once content from one that does reached it. */
export const CARRIED = "a message or reply reached it from one with every origin";
/** Why the agent acting has every origin when Wake can't tell which agent it is. */
export const UNKNOWN = "Wake can't tell which agent is acting";

/** One rule, as wake-proxy serves it: the calls it targets, and the origins it keeps from them. */
export interface FlowRule {
  readonly id: string;
  /** The calling agents it applies to. Empty means every caller. */
  readonly callers: readonly string[];
  /** The agents reached it applies to. Empty means every receiver. */
  readonly receivers: readonly string[];
  /** The request paths it applies to, which only wake-proxy sees. */
  readonly path_prefixes: readonly string[];
  /** The tools it applies to, each exact or a prefix ending in *. */
  readonly tools: readonly string[];
  /**
   * The memory stores it applies to, each exact or a prefix ending in *: a write, a delete or a
   * rollback through the memory adapter, and nothing else.
   */
  readonly stores: readonly string[];
  /** Origins that may not be behind a call it targets. */
  readonly forbid_origins: readonly string[];
  /** The only origins that may be behind a call it targets, when given. */
  readonly allow_origins: readonly string[];
}

/**
 * A call as a rule targets it: who makes it and the agents it acts under, and the tool it
 * calls, the agent it reaches or the memory store it changes.
 */
export interface FlowCall {
  /** The agent making the call, or undefined when Wake can't tell which it is: any rule's callers could be it. */
  readonly caller: string | undefined;
  /** The agents the caller acts under, such as the agent that started a sub-agent. */
  readonly lineage?: Iterable<string>;
  /** The tool called, for a tool call. */
  readonly tool?: string;
  /** The agent reached, for a handoff, or a sub-agent or a run being started. */
  readonly receiver?: string;
  /** The memory store changed, for a write, a delete or a rollback through the memory adapter. */
  readonly store?: string;
}

/** That an agent counts as having every origin, and why. */
export class Every {
  readonly why: string;

  constructor(why: string) {
    this.why = why;
  }
}

/** What entered an agent within the window: these origins, newest first, or every origin. */
export type Seen = readonly string[] | Every;

/** A rule refusing a call: the halt it's refused under, and why. */
export interface Denial {
  readonly halt: string;
  /** The origins behind the call the rule keeps from it, in the order seen, or * for every origin. */
  readonly origins: readonly string[];
  /** Why the caller counts as having every origin, when it does. */
  readonly every?: string;
  /** The policy can't be used, so it refuses every call. */
  readonly unreadable?: boolean;
}

/** A parsed policy: its rules, in file order, and its window. */
export class FlowPolicy {
  readonly rules: readonly FlowRule[];
  readonly windowSeconds: number;
  /** Why the policy can't be used, when it can't: every call is then refused. */
  readonly unreadable: string | null;

  constructor(rules: readonly FlowRule[] = [], windowSeconds = DEFAULT_WINDOW_SECONDS, unreadable: string | null = null) {
    this.rules = rules;
    this.windowSeconds = windowSeconds;
    this.unreadable = unreadable;
  }

  /**
   * The first rule that denies the call, given what entered its caller, as wake-proxy decides. A
   * tool call meets the rules naming no tools, receivers or path prefixes first, as wake-proxy's
   * ext_authz applies them to every call, then the rules naming its tool, as its MCP hook does,
   * each in file order. An agent reached meets the rules naming no tools or path prefixes whose
   * receivers are empty or name it: rules with path prefixes only ever meet requests at the
   * proxy. A change to a memory store meets the rules naming that store, in file order, and
   * only those: a rule naming stores meets nothing else. A rule applies when its callers are
   * empty or name the caller or an agent it acts under, or the caller can't be told, and the
   * first that keeps any origin from the call denies it.
   */
  deciding(call: FlowCall, seen: Seen): Denial | undefined {
    if (this.unreadable !== null) return { halt: UNREADABLE, origins: [], unreadable: true };
    const { caller, receiver, store, tool } = call;
    const names = new Set([...(caller !== undefined ? [caller] : []), ...(call.lineage ?? [])]);
    const rules = this.rules.filter((r) => r.stores.length === 0);
    const meets =
      store !== undefined
        ? this.rules.filter((r) => r.stores.some((p) => matches(p, store)))
        : receiver !== undefined
          ? rules.filter((r) => r.tools.length === 0 && r.path_prefixes.length === 0 && (r.receivers.length === 0 || r.receivers.includes(receiver)))
          : [
              ...rules.filter((r) => r.tools.length === 0 && r.receivers.length === 0 && r.path_prefixes.length === 0),
              ...(tool !== undefined ? rules.filter((r) => r.tools.some((p) => matches(p, tool))) : []),
            ];
    for (const rule of meets) {
      if (caller !== undefined && rule.callers.length > 0 && !rule.callers.some((c) => names.has(c))) continue;
      if (seen instanceof Every) return { halt: `policy:${rule.id}`, origins: ["*"], every: seen.why };
      const kept = seen.filter(
        (o) => rule.forbid_origins.some((p) => matches(p, o)) || (rule.allow_origins.length > 0 && !rule.allow_origins.some((p) => matches(p, o))),
      );
      if (kept.length > 0) return { halt: `policy:${rule.id}`, origins: kept };
    }
    return undefined;
  }
}

/** Whether a pattern matches: a prefix when it ends in *, so * alone matches anything, else the whole value. */
export function matches(pattern: string, value: string): boolean {
  return pattern.endsWith("*") ? value.startsWith(pattern.slice(0, -1)) : value === pattern;
}

const TOP_FIELDS = new Set(["version", "window_seconds", "rules"]);
// Each list a rule may give, and whether its entries may be patterns.
const LISTS = [
  ["callers", false],
  ["receivers", false],
  ["path_prefixes", false],
  ["tools", true],
  ["stores", true],
  ["forbid_origins", true],
  ["allow_origins", true],
] as const;
const RULE_FIELDS = new Set(["id", ...LISTS.map(([name]) => name)]);
const RULE_ID = /^[A-Za-z0-9._:-]{1,64}$/;

/**
 * The policy wake-proxy serves, read as wake-proxy reads its file: any field it doesn't know,
 * a value of the wrong type, a rule with no origins to keep, tools with receivers or path
 * prefixes, stores with tools, receivers or path prefixes, a repeated or malformed id, an
 * empty entry or a * anywhere but at the end of a pattern, or a window outside 1 to 86,400
 * seconds makes it unusable. So does what wake-proxy serves when it can't use its own file,
 * {"unreadable": true}. Never throws: a policy that can't be used refuses every call.
 */
export function parsePolicy(served: unknown): FlowPolicy {
  try {
    return read(served);
  } catch (error) {
    return new FlowPolicy([], DEFAULT_WINDOW_SECONDS, error instanceof Error ? error.message : String(error));
  }
}

function read(served: unknown): FlowPolicy {
  if (!isObject(served)) throw new Error("a policy must be a JSON object");
  if (served.unreadable === true) throw new Error("wake-proxy can't use its policy file");
  for (const key of Object.keys(served)) {
    if (!TOP_FIELDS.has(key)) throw new Error(`a policy has no field ${JSON.stringify(key)}`);
  }
  if (Object.hasOwn(served, "version") && typeof served.version !== "string") throw new Error("a policy's version must be a string");
  const window = Object.hasOwn(served, "window_seconds") ? served.window_seconds : DEFAULT_WINDOW_SECONDS;
  if (typeof window !== "number" || !Number.isInteger(window) || window < 1 || window > MAX_WINDOW_SECONDS) {
    throw new Error(`window_seconds must be a whole number from 1 to ${MAX_WINDOW_SECONDS}, not ${JSON.stringify(window)}`);
  }
  const rules = Object.hasOwn(served, "rules") ? served.rules : [];
  if (!Array.isArray(rules)) throw new Error("a policy's rules must be a list");
  const ids = new Set<string>();
  const parsed = rules.map((value: unknown) => {
    const rule = ruleOf(value);
    if (ids.has(rule.id)) throw new Error(`two rules are ${JSON.stringify(rule.id)}`);
    ids.add(rule.id);
    return rule;
  });
  return new FlowPolicy(parsed, window);
}

function ruleOf(value: unknown): FlowRule {
  if (!isObject(value)) throw new Error("each rule must be a JSON object");
  for (const key of Object.keys(value)) {
    if (!RULE_FIELDS.has(key)) throw new Error(`a rule has no field ${JSON.stringify(key)}`);
  }
  const id = value.id;
  if (typeof id !== "string" || !RULE_ID.test(id)) throw new Error(`rule id ${JSON.stringify(id)} must be 1 to 64 of A-Z a-z 0-9 . _ : -`);
  const list = (name: string): string[] => {
    if (!Object.hasOwn(value, name)) return [];
    const entries = value[name];
    if (!Array.isArray(entries) || !entries.every((e) => typeof e === "string")) throw new Error(`rule ${id}: ${name} must be a list of strings`);
    return [...entries];
  };
  const rule: FlowRule = {
    id,
    callers: list("callers"),
    receivers: list("receivers"),
    path_prefixes: list("path_prefixes"),
    tools: list("tools"),
    stores: list("stores"),
    forbid_origins: list("forbid_origins"),
    allow_origins: list("allow_origins"),
  };
  if (rule.forbid_origins.length === 0 && rule.allow_origins.length === 0) {
    throw new Error(`rule ${id} keeps nothing: give it forbid_origins or allow_origins`);
  }
  if (rule.tools.length > 0 && (rule.receivers.length > 0 || rule.path_prefixes.length > 0)) {
    throw new Error(`rule ${id} names tools with receivers or path_prefixes, which no call has together`);
  }
  if (rule.stores.length > 0 && (rule.tools.length > 0 || rule.receivers.length > 0 || rule.path_prefixes.length > 0)) {
    throw new Error(`rule ${id} names stores with tools, receivers or path_prefixes: a memory write has none of them`);
  }
  for (const [name, patterns] of LISTS) {
    for (const entry of rule[name]) {
      const wildcard = entry.indexOf("*");
      if (entry === "" || (!patterns && wildcard >= 0) || (wildcard >= 0 && wildcard !== entry.length - 1)) {
        throw new Error(`rule ${id}: ${name} entry ${JSON.stringify(entry)} must be exact${patterns ? ", or a prefix ending in *" : ""}`);
      }
    }
  }
  return rule;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** What entered one agent. */
class Agent {
  /** Each origin, and when it last entered. */
  readonly origins = new Map<string, number>();
  /** When the agent last came to have every origin, and why. */
  saturated: readonly [at: number, why: string] | undefined;

  newest(): number {
    let newest = this.saturated?.[0] ?? -Infinity;
    for (const at of this.origins.values()) newest = Math.max(newest, at);
    return newest;
  }

  /** Notes that origin entered at, forgetting origins older than oldest. Past MAX_ORIGINS, the agent has every origin from then. */
  note(origin: string, at: number, oldest: number): void {
    const seen = this.origins.get(origin);
    if (seen !== undefined) {
      this.origins.set(origin, Math.max(seen, at));
      return;
    }
    for (const [o, t] of this.origins) if (t < oldest) this.origins.delete(o);
    if (this.origins.size >= MAX_ORIGINS) {
      this.saturate(at, SATURATED);
      return;
    }
    this.origins.set(origin, at);
  }

  /** Notes that the agent has every origin from at, for why, unless it does from later. */
  saturate(at: number, why: string): void {
    if (this.saturated === undefined || this.saturated[0] <= at) this.saturated = [at, why];
  }
}

/**
 * What entered each agent in a process, by its name, and when, as wake-proxy keeps what
 * entered each agent through it (proxy/src/origins.rs). Each origin keeps the last time it
 * entered, and only those within the window count. Where it can't know, an agent has every
 * origin: past MAX_ORIGINS within a window, and past MAX_AGENTS for an agent it couldn't keep.
 * Its methods never throw: noting what entered an agent must never break it.
 */
export class Entered {
  readonly #clock: () => number;
  readonly #agents = new Map<string, Agent>();
  #window: number; // milliseconds
  #overflowedAt: number | undefined; // when an agent last couldn't be kept, past MAX_AGENTS

  /** clock gives the time in milliseconds: by default, performance.now(), which never goes back. */
  constructor({ windowSeconds = DEFAULT_WINDOW_SECONDS, clock = () => performance.now() }: { windowSeconds?: number; clock?: () => number } = {}) {
    this.#clock = clock;
    this.#window = millis(windowSeconds);
  }

  /** Keeps origins for this many seconds from now on, as the policy in force says. */
  setWindow(seconds: number): void {
    this.#window = millis(seconds);
  }

  /** Notes that origin entered agent now. */
  note(agent: string, origin: string): void {
    try {
      const at = this.#clock();
      this.#agent(agent, at)?.note(origin, at, at - this.#window);
    } catch (error) {
      log.warn(`couldn't note what entered ${agent}`, error);
    }
  }

  /**
   * Notes that content from one agent reached another, such as a message, a handoff or a
   * sub-agent's result: what had entered from within the window entered to too, each origin
   * keeping when it entered, so passing content on never makes it newer. When from has every
   * origin, so does to, from when from did. to's own name isn't carried back to it as an
   * origin.
   */
  carry(from: string, to: string): void {
    try {
      const at = this.#clock();
      const since = at - this.#window;
      const entered = this.#entered(from, since);
      if (entered instanceof Map) {
        entered.delete(`agent:${to}`);
        if (entered.size === 0) return;
        const agent = this.#agent(to, at);
        for (const [origin, seen] of entered) agent?.note(origin, seen, since);
      } else {
        this.#agent(to, at)?.saturate(entered.since, CARRIED);
      }
    } catch (error) {
      log.warn(`couldn't note what reached ${to} from ${from}`, error);
    }
  }

  /**
   * Notes that a message from one agent reached another: a message, a handoff, a sub-agent's
   * start or its result. It enters to as agent:<from>, bringing what had entered from (carry).
   * Nothing enters an agent from itself.
   */
  message(from: string, to: string): void {
    if (from === to) return;
    this.note(to, `agent:${from}`);
    this.carry(from, to);
  }

  /** What entered agent within the window: each origin once, newest first, or every origin. */
  seen(agent: string): Seen {
    const entered = this.#entered(agent, this.#clock() - this.#window);
    if (!(entered instanceof Map)) return new Every(entered.why);
    return [...entered].sort(([a, x], [b, y]) => y - x || byCodePoint(a, b)).map(([origin]) => origin);
  }

  /** The agent kept under name, made room for if need be. Undefined when it can't be kept: MAX_AGENTS others had something enter them within the window. */
  #agent(name: string, at: number): Agent | undefined {
    let agent = this.#agents.get(name);
    if (agent !== undefined) return agent;
    if (this.#agents.size >= MAX_AGENTS) {
      // Agents nothing has entered for a window can be forgotten.
      const oldest = at - this.#window;
      for (const [n, a] of this.#agents) if (a.newest() < oldest) this.#agents.delete(n);
      if (this.#agents.size >= MAX_AGENTS) {
        this.#overflowedAt = at;
        return undefined;
      }
    }
    agent = new Agent();
    this.#agents.set(name, agent);
    return agent;
  }

  /** What entered name since since, each origin with when it last entered, or why it's every origin and since when. */
  #entered(name: string, since: number): Map<string, number> | { why: string; since: number } {
    const agent = this.#agents.get(name);
    if (agent === undefined) {
      if (this.#overflowedAt !== undefined && this.#overflowedAt >= since) return { why: OVERFLOWED, since: this.#overflowedAt };
      return new Map();
    }
    if (agent.saturated !== undefined && agent.saturated[0] >= since) return { why: agent.saturated[1], since: agent.saturated[0] };
    return new Map([...agent.origins].filter(([, at]) => at >= since));
  }
}

function millis(seconds: number): number {
  return Number.isFinite(seconds) ? Math.min(Math.max(seconds, 1), MAX_WINDOW_SECONDS) * 1000 : DEFAULT_WINDOW_SECONDS * 1000;
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

/** What entered each agent in this process, which every integration notes in and checks against. */
export const entered = new Entered();
