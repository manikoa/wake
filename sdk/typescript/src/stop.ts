/**
 * The in-process stop every integration shares: whether a halt in the brake's rules covers
 * whoever is about to act, and what they're told when one does.
 *
 * Each integration knows who is acting by its own means, and asks here, so every one refuses
 * the same things the same way: a call by an agent, or by an instance of it, that a halt
 * covers; a run it would start of an agent a halt covers, such as a Claude subagent type, an
 * OpenAI Agents handoff's target or a LangChain named run; every call while the rules are
 * stale (Quarantine); and while an integration can't yet tell which instance is acting, any
 * call a halt on one it could be would cover. A check that fails refuses too: the brake fails
 * closed.
 *
 * At the same points, once no halt covers a call, each integration asks flowRefusal whether
 * the flow policy served with the halts keeps it from what entered its caller (flow.ts), and
 * refuses what the policy keeps the same way.
 */

import { entered, Every, UNKNOWN, UNREADABLE, type Denial, type FlowCall, type FlowPolicy } from "./flow.ts";
import { log } from "./log.ts";

/** The id a call is refused under when its halts couldn't be checked. */
export const UNCHECKED = "halt-check-failed";

/**
 * What a refusal under the flow policy adds to the denied event's payload, as wake-proxy's
 * does: the origins its rule kept from the call, or * for every origin, and then why the caller
 * counted as having every origin.
 */
export interface Kept {
  readonly origins: readonly string[];
  readonly every_origin?: string;
}

/**
 * A call a halt or the flow policy stops: the halt, as the denied event names it, what the
 * agent is told, and, under the flow policy, what's behind it.
 */
export type Refusal = readonly [halt: string, reason: string, kept?: Kept];

/** What says whether a halt covers some names: the brake's rules, a Quarantine. */
export interface Halts {
  halted(names: Iterable<string>): string | null;
  /** The flow policy served with the halts (flow.ts), or undefined when there's none. Rules that can't say, such as a test's, have none. */
  flow?(): FlowPolicy | undefined;
  /** Called by an integration that refuses halted instances' calls with these rules (stopsWith). */
  _stopsInstances?(): void;
}

/** A run the brake's halt stopped before it started. */
export class AgentHalted extends Error {
  readonly halt: string;

  constructor([halt, reason]: Refusal) {
    super(reason);
    this.name = "AgentHalted";
    this.halt = halt;
  }
}

/**
 * The refusal of something who is about to do, if a halt covers any of names: its agent, the
 * instance it is and those that started it, as far as they're known. While who can't be
 * placed, unplaced names every instance it could be, and a halt on any of them refuses it too.
 * Undefined without rules. Never throws: when the check itself fails, it refuses.
 */
export function refusal(
  halts: Halts | undefined,
  who: string,
  names: Iterable<string>,
  { unplaced = [], outcome = "this tool call wasn't run" }: { unplaced?: Iterable<string>; outcome?: string } = {},
): Refusal | undefined {
  if (halts === undefined) return undefined;
  try {
    const named = new Set(names);
    let halt = halts.halted(named);
    if (halt !== null) return [halt, `Wake halted ${who} (${halt}), so ${outcome}.`];
    const others = [...unplaced].filter((name) => !named.has(name));
    if (others.length > 0) {
      halt = halts.halted([...named, ...others]);
      if (halt !== null) {
        return [halt, `Wake can't yet tell which copy of ${who} is calling, and a halt (${halt}) may cover it, so ${outcome}.`];
      }
    }
    return undefined;
  } catch (error) {
    log.warn(`couldn't check the halts covering ${who}; refusing`, error);
    return unchecked(outcome);
  }
}

/** The refusal when Wake couldn't work out whether a halt covers a call. */
export function unchecked(outcome = "this tool call wasn't run"): Refusal {
  return [UNCHECKED, `Wake couldn't check its halts, so ${outcome}.`];
}

/**
 * The refusal of a call the flow policy served with the halts keeps from what entered its
 * caller in this process, by the caller's name; who is the caller as the agent is told, as for
 * a halt, such as the instance it is. call names the agents the caller acts under too, as for
 * the halts: an instance, <agent>#<id>, counts as its agent as well, so a rule naming an agent
 * holds the sub-agents its runs start. A caller Wake can't tell, undefined, counts as having
 * every origin, and any rule's callers could be it. Undefined without a policy, as when
 * wake-proxy serves none. Never throws: a check that fails refuses, as a policy that can't be
 * read does.
 */
export function flowRefusal(halts: Halts | undefined, who: string, call: FlowCall, outcome = "this tool call wasn't run"): Refusal | undefined {
  if (halts?.flow === undefined) return undefined;
  let denial: Denial | undefined;
  try {
    const policy = halts.flow();
    if (policy === undefined) return undefined;
    const lineage = [...(call.lineage ?? [])];
    if (policy.unreadable === null) entered.setWindow(policy.windowSeconds);
    const seen = policy.unreadable !== null ? [] : call.caller === undefined ? new Every(UNKNOWN) : entered.seen(call.caller);
    denial = policy.deciding({ ...call, lineage: [...lineage, ...lineage.map(agentOf)] }, seen);
  } catch (error) {
    log.warn(`couldn't check the flow policy for ${who}; refusing`, error);
    denial = { halt: UNREADABLE, origins: [], unreadable: true };
  }
  if (denial === undefined) return undefined;
  if (denial.unreadable) return [denial.halt, `Wake's flow policy can't be read, so ${outcome}.`, { origins: [] }];
  if (denial.every !== undefined) {
    const why = denial.every;
    return [denial.halt, `Wake's flow policy (${denial.halt}) holds ${who} back, because ${why}, so ${outcome}.`, { origins: denial.origins, every_origin: why }];
  }
  return [denial.halt, `Wake's flow policy (${denial.halt}) holds ${who} back after ${denial.origins.join(", ")}, so ${outcome}.`, { origins: denial.origins }];
}

/** The agent an instance, <agent>#<id>, is a run of; any other name is its own. */
function agentOf(name: string): string {
  const at = name.lastIndexOf("#");
  return at > 0 ? name.slice(0, at) : name;
}

/** What a denied event's payload adds for a refusal: under the flow policy, what's behind it. */
export function keptOf(refused: Refusal | undefined): Kept | Record<string, never> {
  return refused?.[2] ?? {};
}

/**
 * Notes that an integration refuses halted instances' calls with these rules, so each fetch
 * says so from then on (Quarantine). Rules that can't say it, such as a test's, are left as
 * they are.
 */
export function stopsWith(halts: Halts | undefined): void {
  halts?._stopsInstances?.();
}
