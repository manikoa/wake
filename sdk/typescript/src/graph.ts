/**
 * What every capture integration shares: bounded memory of recent events, the content index
 * that links events by what they carry when no ids do, and each event's parents and
 * origins, so a memory write can be labeled with the origins upstream of it.
 */

import { ROOT_CONTEXT, trace, TraceFlags, type Context } from "@opentelemetry/api";

import * as links from "./links.ts";
import { log } from "./log.ts";
import type { Recorder } from "./recorder.ts";

// Content matching: an output is findable by its first characters, and only outputs long
// enough that a match is unlikely to be chance are indexed.
const SNIPPET = 160;
export const MIN_MATCH = 24;
const OUTPUTS_PER_TRACE = 64;
const KEPT_OUTPUT = 2000;

/**
 * Recording must never break the agent: a hook that fails is logged, counted in the
 * recorder's health, and returns undefined.
 */
export function safely<T>(recorder: Recorder, what: string, fn: () => T): T | undefined {
  try {
    return fn();
  } catch (error) {
    log.warn(`failed to record ${what}`, error);
    failed(recorder, what);
    return undefined;
  }
}

/** Counts an event an integration couldn't record. Never throws. */
export function failed(recorder: Recorder, what: string): void {
  try {
    recorder._failed(what);
  } catch {
    // Counting must never break the agent either.
  }
}

/** A map that forgets its oldest entries, so a long running agent's memory stays bounded. */
export class Recent<K, V> extends Map<K, V> {
  readonly #limit: number;

  constructor(limit = 10_000) {
    super();
    this.#limit = limit;
  }

  override set(key: K, value: V): this {
    super.delete(key);
    super.set(key, value);
    if (this.size > this.#limit) super.delete(this.keys().next().value as K);
    return this;
  }

  /** Removes and returns the entry, as Python's dict.pop does. */
  pop(key: K): V | undefined {
    const value = super.get(key);
    super.delete(key);
    return value;
  }
}

/** The trace context for a span under this link's event, or undefined if it carries no trace. */
export function contextOf(link: links.Link | undefined): Context | undefined {
  if (link === undefined || !link.hasTrace) return undefined;
  return trace.setSpanContext(ROOT_CONTEXT, {
    traceId: link.traceId,
    spanId: link.spanId,
    traceFlags: TraceFlags.SAMPLED,
    isRemote: false,
  });
}

/** The trace id a context's span is in, or "" for none. */
export function traceIdOf(context: Context | undefined): string {
  return context === undefined ? "" : (trace.getSpanContext(context)?.traceId ?? "");
}

/** The ids in order, each once. */
export function unique(ids: Iterable<string>): string[] {
  return [...new Set(ids)];
}

// oxlint-disable-next-line @typescript-eslint/no-explicit-any
type Constructor = abstract new (...args: any[]) => object;

/**
 * Bookkeeping shared by the capture integrations, added to a base class, such as LangChain's
 * callback handler. Subclasses set _recorder, record events with it, and call
 * _rememberOutput and _rememberLineage as they do.
 */
export function Graph<TBase extends Constructor>(Base: TBase) {
  abstract class EventGraphBase extends Base {
    _recorder!: Recorder;
    readonly _outputs = new Recent<string, [string, string][]>(256); // trace id -> recent (event id, output text)
    readonly _lineage = new Recent<string, [string[], string[]]>(); // event id -> (parents, origins), for memory labels

    _rememberLineage(eventId: string, parents: string[], origins: string[]): void {
      this._lineage.set(eventId, [[...parents], [...origins]]);
    }

    /**
     * The origins of these events and everything upstream of them that this integration
     * recorded. Coarse: an event's origins are the union over its ancestors.
     */
    _originsUpstream(eventIds: string[], limit = 2000): string[] {
      const seen = new Set<string>();
      const out: string[] = [];
      const queue = [...eventIds];
      while (queue.length > 0 && seen.size < limit) {
        const eventId = queue.pop()!;
        if (seen.has(eventId)) continue;
        seen.add(eventId);
        const [parents, origins] = this._lineage.get(eventId) ?? [[], []];
        for (const o of origins) if (!out.includes(o)) out.push(o);
        queue.push(...parents);
      }
      return out;
    }

    /**
     * The trace context for an event recorded outside the framework's own hooks, such as a
     * memory read or write: under the tool call making it.
     */
    _eventContext(): Context | undefined {
      return contextOf(links.current());
    }

    _currentAgent(): string {
      return this._recorder.agent;
    }

    _rememberOutput(traceId: string, eventId: string, text: string): void {
      text = text.trim();
      if (text.length < MIN_MATCH || !traceId) return;
      let outputs = this._outputs.get(traceId);
      if (outputs === undefined) {
        outputs = [];
        this._outputs.set(traceId, outputs);
      }
      outputs.push([eventId, text.slice(0, KEPT_OUTPUT)]);
      if (outputs.length > OUTPUTS_PER_TRACE) outputs.shift();
    }

    /** Earlier events in this trace whose output appears in this text, or that contain all of it. */
    _matchingOutputs(traceId: string, text: string): string[] {
      text = text.trim();
      const whole = text.length >= MIN_MATCH;
      const outputs = this._outputs.get(traceId) ?? [];
      return outputs
        .filter(([, output]) => text.includes(output.slice(0, SNIPPET)) || (whole && output.includes(text)))
        .map(([id]) => id);
    }

    /** Earlier events in this trace whose output contains one of these arguments. */
    _outputsContaining(traceId: string, args: string[]): string[] {
      const outputs = this._outputs.get(traceId) ?? [];
      return outputs.filter(([, output]) => args.some((a) => output.includes(a))).map(([id]) => id);
    }
  }
  return EventGraphBase;
}

/** The bookkeeping on its own, for an integration with no framework class to extend. */
export class EventGraph extends Graph(Object) {
  constructor(recorder: Recorder) {
    super();
    this._recorder = recorder;
  }
}

/** What the memory adapter records alongside: any integration with the bookkeeping. */
export type Recording = InstanceType<ReturnType<typeof Graph>>;
