/**
 * The in-process stop every integration shares: whether a halt in the brake's rules covers
 * whoever is about to act, and what they're told when one does.
 *
 * Each integration knows who is acting by its own means, and asks here, so every one refuses
 * the same things the same way: a call by an agent, or by an instance of it, that a halt
 * covers; every call while the rules are stale (Quarantine); and while an integration can't
 * yet tell which instance is acting, any call a halt on one it could be would cover. A check
 * that fails refuses too: the brake fails closed.
 */

import { log } from "./log.ts";

/** The id a call is refused under when its halts couldn't be checked. */
export const UNCHECKED = "halt-check-failed";

/** A call a halt stops: the halt, as the denied event names it, and what the agent is told. */
export type Refusal = readonly [halt: string, reason: string];

/** What says whether a halt covers some names: the brake's rules, a Quarantine. */
export interface Halts {
  halted(names: Iterable<string>): string | null;
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
