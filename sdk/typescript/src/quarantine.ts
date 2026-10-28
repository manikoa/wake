/**
 * The brake's memory quarantine, as the memory adapter enforces it.
 *
 *     const quarantine = new Quarantine("http://wake-proxy:9003", { agent: "billing" });
 *     await quarantine.ready; // the first fetch, a second at most
 *     const memory = new Memory(handler, { quarantine });
 *
 * The brake puts quarantines on wake-proxy, and this fetches the agent's rules from it: once
 * when it starts, with a short timeout, then in the background every couple of seconds, so
 * it never holds up the agent after that. With them, the memory adapter refuses the agent's
 * writes when they're locked (MemoryLocked) and stops serving entries the quarantine covers:
 * by the agent that wrote them, their origins, the write that made them, their key, or
 * because no label vouches for them. Quarantined entries stay in the store, as evidence;
 * they're only not served, and not deleted.
 *
 * The brake fails closed. When wake-proxy can't read the quarantines it holds, it says so
 * (hidden_by), and no entry is served. When a fetch fails the last rules stand, but only for
 * a while: once they're older than ttlMillis, or if no fetch has succeeded yet, every write
 * is refused and no entry is served, under the id STALE, until a fetch succeeds again.
 * health() says whether the rules are fresh, and going stale is logged as a warning.
 *
 * The proxy notes each fetch, which is how the brake confirms an adapter has a quarantine.
 * Each fetch names the agent and this process (a process parameter the proxy is free to
 * ignore). All of this runs in the agent's own process, so it holds only as long as the
 * agent's code does; the proxy deny doesn't depend on it.
 */

import { randomBytes } from "node:crypto";
import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";

import { context as otelContext } from "@opentelemetry/api";
import { suppressTracing } from "@opentelemetry/core";

import { log } from "./log.ts";

/** The id writes and reads are refused under while the rules are stale. */
export const STALE = "stale-rules";
/** The largest rules accepted. A larger answer is a failed fetch. */
export const MAX_RULES_BYTES = 8 << 20;

/** A memory write the brake's quarantine refused. */
export class MemoryLocked extends Error {
  readonly quarantine: string;

  constructor(quarantine: string, store: string, entry: string) {
    super(`memory writes are locked by quarantine ${quarantine}: ${store}/${entry} not written`);
    this.name = "MemoryLocked";
    this.quarantine = quarantine;
  }
}

/** A memory entry the brake's quarantine stopped serving. */
export class MemoryQuarantined extends Error {
  readonly quarantine: string;

  constructor(quarantine: string, store: string, entry: string) {
    super(`${store}/${entry} is quarantined by ${quarantine}`);
    this.name = "MemoryQuarantined";
    this.quarantine = quarantine;
  }
}

/** Whether the rules are fresh: ageMillis since the last fetch that succeeded, null if none has. */
export interface QuarantineHealth {
  readonly ok: boolean;
  readonly ageMillis: number | null;
  readonly lastError: string | null;
}

export interface QuarantineOptions {
  agent: string;
  intervalMillis?: number;
  timeoutMillis?: number;
  ttlMillis?: number;
  firstTimeoutMillis?: number;
  /** Fetch now and then every intervalMillis. Off, rules come only from fetch(). */
  start?: boolean;
}

// What the rules hold, as wake-proxy serves them.
export interface Rules {
  locked_by?: string | null;
  hidden_by?: string | null;
  quarantines?: { id: string; selection?: Selection | null }[];
  [key: string]: unknown;
}

interface Selection {
  agents?: string[];
  origins?: string[];
  writes?: string[];
  entries?: string[];
  unlabeled?: boolean;
  [key: string]: unknown;
}

export interface Label {
  event?: unknown;
  origins?: unknown;
  sha256?: unknown;
  agent?: unknown;
  [key: string]: unknown;
}

function processId(): string {
  return `${process.pid}.${randomBytes(6).toString("hex")}`;
}

function valid(rules: unknown): rules is Rules {
  if (rules === null || typeof rules !== "object" || Array.isArray(rules)) return false;
  const { locked_by: locked, hidden_by: hidden, quarantines = [] } = rules as Rules;
  if ([locked, hidden].some((v) => v !== null && v !== undefined && typeof v !== "string") || !Array.isArray(quarantines)) {
    return false;
  }
  return quarantines.every(
    (q) =>
      q !== null &&
      typeof q === "object" &&
      typeof q.id === "string" &&
      q.id !== "" &&
      (q.selection === null || q.selection === undefined || (typeof q.selection === "object" && !Array.isArray(q.selection))),
  );
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

/** Fetches one agent's memory rules from wake-proxy, and fails closed when it can't. */
export class Quarantine {
  /** Settles once the first fetch has succeeded or failed: a second at most, by default. */
  readonly ready: Promise<boolean>;
  readonly #url: string;
  readonly #agent: string;
  readonly #interval: number;
  readonly #timeout: number;
  readonly #ttl: number;
  readonly #process = processId();
  #rules: Rules | null = null;
  #fetchedAt: number | null = null; // performance.now() of the last fetch that succeeded
  #error: string | null = null;
  #warnedStale = false;
  #closed = false;
  #timer: NodeJS.Timeout | null = null;

  constructor(url: string, options: QuarantineOptions) {
    this.#url = `${url.replace(/\/+$/, "")}/v1/memory`;
    this.#agent = options.agent;
    this.#interval = options.intervalMillis ?? 2000;
    this.#timeout = options.timeoutMillis ?? 2000;
    this.#ttl = options.ttlMillis ?? 30_000;
    if (options.start ?? true) {
      this.ready = this.fetch(options.firstTimeoutMillis ?? 1000);
      void this.ready.then(() => this.#schedule());
    } else {
      this.ready = Promise.resolve(false);
    }
  }

  #schedule(): void {
    if (this.#closed) return;
    this.#timer = setTimeout(() => void this.fetch().then(() => this.#schedule()), this.#interval);
    this.#timer.unref();
  }

  /** Fetches the rules now. Never rejects: on failure the last rules stand until they're stale. */
  async fetch(timeoutMillis?: number): Promise<boolean> {
    try {
      const query = new URLSearchParams({ agent: this.#agent, process: this.#process });
      const body = await get(`${this.#url}?${query}`, timeoutMillis ?? this.#timeout);
      const rules: unknown = JSON.parse(body);
      if (!valid(rules)) throw new TypeError("unexpected memory rules");
      this.#rules = rules;
      this.#fetchedAt = performance.now();
      this.#error = null;
    } catch (e) {
      const error = `couldn't fetch memory rules: ${e instanceof Error ? e.message : String(e)}`;
      const repeated = error === this.#error;
      this.#error = error;
      // Each new error is a warning; the same one again, every couple of seconds, isn't.
      if (repeated) log.debug(error);
      else log.warn(error);
      this.#noteStaleness();
      return false;
    }
    this.#noteStaleness();
    return true;
  }

  #noteStaleness(): void {
    const stale = this.#isStale();
    if (stale === this.#warnedStale) return;
    this.#warnedStale = stale;
    if (stale) log.warn("memory rules are stale; refusing memory writes and reads until they're fetched");
    else log.warn("memory rules fetched again");
  }

  close(): void {
    this.#closed = true;
    if (this.#timer !== null) clearTimeout(this.#timer);
  }

  health(): QuarantineHealth {
    const age = this.#fetchedAt === null ? null : performance.now() - this.#fetchedAt;
    return { ok: age !== null && age <= this.#ttl, ageMillis: age, lastError: this.#error };
  }

  rules(): Rules | null {
    return this.#rules;
  }

  #isStale(): boolean {
    return this.#fetchedAt === null || performance.now() - this.#fetchedAt > this.#ttl;
  }

  /** The quarantine refusing this agent's memory writes, if any: STALE when the rules are. */
  lockedBy(): string | null {
    const locked = this.#rules?.locked_by;
    if (typeof locked === "string" && locked) return locked;
    return this.#isStale() ? STALE : null;
  }

  /**
   * The quarantine that stops this entry being served, if any: STALE when the rules are, and
   * the proxy's hidden_by when it serves no entry at all, as when it can't read the
   * quarantines it holds. vouched says whether the label's digest matches the content read.
   */
  hiding(store: string, entry: string, label: Label | null | undefined, vouched: boolean): string | null {
    const rules = this.#rules;
    const hidden = rules?.hidden_by;
    if (typeof hidden === "string" && hidden) return hidden;
    const key = `${store}/${entry}`;
    const trusted = vouched ? label : null;
    for (const q of rules?.quarantines ?? []) {
      const selection = q.selection ?? {};
      if (!trusted && selection.unlabeled) return q.id;
      if (strings(selection.entries).includes(key)) return q.id;
      if (!trusted) continue;
      const origins = strings(trusted.origins);
      if (
        strings(selection.agents).includes(trusted.agent as string) ||
        strings(selection.writes).includes(trusted.event as string) ||
        strings(selection.origins).some((o) => origins.includes(o))
      ) {
        return q.id;
      }
    }
    return this.#isStale() ? STALE : null;
  }
}

/** GETs a URL's body, at most MAX_RULES_BYTES of it, and rejects on anything but a 2xx. */
function get(url: string, timeoutMillis: number): Promise<string> {
  return new Promise((resolve, reject) => {
    const send = url.startsWith("https:") ? httpsRequest : httpRequest;
    otelContext.with(suppressTracing(otelContext.active()), () => {
      const request = send(url, { method: "GET", timeout: timeoutMillis }, (response) => {
        const status = response.statusCode ?? 0;
        if (status < 200 || status >= 300) {
          response.resume();
          reject(new Error(`HTTP ${status}`));
          return;
        }
        const chunks: Buffer[] = [];
        let size = 0;
        response.on("data", (chunk: Buffer) => {
          size += chunk.length;
          if (size > MAX_RULES_BYTES) {
            request.destroy(new Error(`memory rules over ${MAX_RULES_BYTES} bytes`));
            return;
          }
          chunks.push(chunk);
        });
        response.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
        response.on("error", reject);
      });
      // A deadline for the whole fetch, not just for a quiet socket.
      const deadline = setTimeout(() => request.destroy(new Error(`timed out after ${timeoutMillis}ms`)), timeoutMillis);
      deadline.unref();
      request.on("close", () => clearTimeout(deadline));
      request.on("timeout", () => request.destroy(new Error(`timed out after ${timeoutMillis}ms`)));
      request.on("error", reject);
      request.end();
    });
  });
}
