/**
 * The memory adapter: records every memory write and read, and labels each entry with where
 * its content came from before it lands.
 *
 *     const memory = new Memory(handler);
 *     const label = await memory.write("facts", "fact-17", text); // save the label with the entry
 *     ...
 *     memory.read("facts", "fact-17", text, label);         // links the read to the write
 *
 * A write is a memory_write event. Its parents are the tool call making it and the earlier
 * events in the trace whose output it carries. The label it returns holds the write's event
 * id, the origins upstream of it (user, tool:..., agent:...), and a digest of the content.
 * Store the label with the entry, so a later read, by any agent in any session, knows where
 * the entry came from without asking the graph.
 *
 * A delete is a memory_write event too, with wake.memory.deleted set and no label to keep.
 *
 * A read is a memory_read event with origin memory:<store>/<entry>. When the entry carries a
 * label whose digest matches what was read, the read's parent is the write. An entry with no
 * label, such as one planted or written outside Wake, or one changed since it was written,
 * gets no parent, so the completeness metric flags it rather than trusting it.
 *
 * Wrappers: WakeStore in wake-sdk/langgraph wraps LangGraph's BaseStore, and writeFile /
 * readFile handle memory files. Recording never blocks or breaks the agent: if it fails, the
 * write or read still happens, unlabeled, and the failure is counted in the recorder's health.
 *
 * With a Quarantine (wake-sdk/quarantine), the adapter also enforces the brake's memory
 * quarantine: a locked agent's writes and deletes reject with MemoryLocked and don't happen,
 * a quarantined entry isn't served (read returns the quarantine's id, the store wrapper drops
 * the entry, readFile rejects with MemoryQuarantined), and overwriting or deleting one
 * rejects with MemoryLocked: it's kept, as evidence. Each refusal is recorded as a denied event. If
 * checking the quarantine fails, the write or delete is refused and the entry isn't served.
 * In a process that names its agents' runs, each run's writes are locked by its own name.
 */

import { randomBytes, randomUUID } from "node:crypto";
import { chmod, readFile, rename, rm, stat, writeFile, realpath } from "node:fs/promises";
import { basename, dirname, join, resolve } from "node:path";

import type { Context, Span } from "@opentelemetry/api";

import * as events from "./events.ts";
import { failed, traceIdOf, unique, type Recording } from "./graph.ts";
import * as links from "./links.ts";
import { log } from "./log.ts";
import { MemoryLocked, MemoryQuarantined, type Label, type Quarantine } from "./quarantine.ts";

export { MemoryLocked, MemoryQuarantined, type Label };

/** Where a label lives: a key in a store's entry, or a file next to a memory file. */
export const LABEL_KEY = "__wake__";
export const LABEL_SUFFIX = ".wake.json";
/** The id a write is refused under, and an entry not served, when checking the quarantine fails. */
export const UNCHECKED = "quarantine-check-failed";
export const GATE_DENIED = "write-gate-denied";

export type WriteSnapshot = {
  event: string;
  origins: string[];
  sha256: string;
  agent: string;
  content: string;
};

/** What write() hands back, to keep with the entry. */
export type WriteLabel = {
  event: string;
  origins: string[];
  sha256: string;
  agent: string;
  previous?: WriteSnapshot[];
};

export type WriteGate = (store: string, entry: string, content: string, origins: string[]) => boolean | Promise<boolean>;

export function digest(content: string): string {
  return events.sha256(content);
}

/** The text of an entry's content, as the label's digest covers it. Never throws. */
export function contentOf(value: unknown): string {
  return typeof value === "string" ? value : events.dumps(value, { ensureAscii: false });
}

/** An event id from a label, which anything that can write the store can forge, if it looks like one. */
function eventId(value: unknown): string | null {
  return typeof value === "string" && links.EVENT_ID.test(value) ? value : null;
}

function isLabel(value: unknown): value is Label {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** Replaces the file whole, keeping its mode: a reader finds the old file or the new one. */
async function writeAtomically(path: string, text: string): Promise<void> {
  const temporary = join(dirname(path), `.${basename(path)}.${randomBytes(4).toString("hex")}.tmp`);
  try {
    await writeFile(temporary, text);
    const mode = await stat(path).then((s) => s.mode, () => null);
    if (mode !== null) await chmod(temporary, mode & 0o7777);
    await rename(temporary, path);
  } catch (error) {
    await rm(temporary, { force: true });
    throw error;
  }
}

/** The path with symbolic links resolved, as far as it exists. */
async function resolved(path: string): Promise<string> {
  const absolute = resolve(path);
  try {
    return await realpath(absolute);
  } catch {
    try {
      return join(await realpath(dirname(absolute)), basename(absolute));
    } catch {
      return absolute;
    }
  }
}

/**
 * Records memory writes and reads alongside the events an SDK integration records, such as a
 * WakeCallbackHandler, a WakeTracingProcessor or a WakeClaudeAgent.
 */
export class Memory {
  readonly #handler: Recording;
  readonly #quarantine?: Quarantine;
  readonly #gate?: WriteGate;

  constructor(handler: Recording, options: { quarantine?: Quarantine; gate?: WriteGate } = {}) {
    this.#handler = handler;
    this.#quarantine = options.quarantine;
    this.#gate = options.gate;
  }

  get quarantine(): Quarantine | undefined {
    return this.#quarantine;
  }

  /**
   * Records a write and resolves to the label to store with the entry, or null if recording
   * failed. current and label are what the entry holds now (undefined if nothing) and the
   * label stored with it. Rejects with MemoryLocked, before anything is written, when the
   * brake's quarantine locks this agent's writes, or hides the entry: a quarantined entry is
   * kept, as evidence, not overwritten.
   */
  async write(store: string, entry: string, content: unknown, current?: unknown, label?: Label | null): Promise<WriteLabel | null> {
    await this.#check("write", store, entry, current, label);
    const text = contentOf(content);
    if (this.#gate) {
      const { parents, origins } = this.#parentsAndOrigins(text);
      const allowed = await this.#gate(store, entry, text, origins);
      if (!allowed) {
        this.#refused("write", store, entry, GATE_DENIED, parents);
        throw new MemoryLocked(GATE_DENIED, store, entry);
      }
    }
    try {
      return this.#write(store, entry, text);
    } catch (error) {
      this._failed("a memory write", error);
      return null;
    }
  }

  /**
   * Records the delete of an entry, given what it holds now (undefined if nothing) and the
   * label stored with it. Rejects with MemoryLocked, before anything is deleted, when the
   * brake's quarantine locks this agent's writes, or hides the entry: a quarantined entry is
   * kept, as evidence.
   */
  async delete(store: string, entry: string, current?: unknown, label?: Label | null): Promise<void> {
    await this.#check("delete", store, entry, current, label);
    try {
      this.#delete(store, entry);
    } catch (error) {
      this._failed("a memory delete", error);
    }
  }

  /**
   * Throws MemoryLocked, and records the refusal, when the quarantine locks this agent's
   * writes or hides the entry as it is now.
   */
  async #check(action: string, store: string, entry: string, current: unknown, label: Label | null | undefined): Promise<void> {
    let refused = await this.#locked();
    if (!refused && current !== undefined && current !== null) refused = this.#hidden(store, entry, contentOf(current), label);
    if (refused) {
      const write = isLabel(label) ? eventId(label.event) : null;
      this.#refused(action, store, entry, refused, [...this.#linked(), ...(write ? [write] : [])]);
      throw new MemoryLocked(refused, store, entry);
    }
  }

  /**
   * Records a read of an entry, with the label it was stored with, if any. Returns the id of
   * the quarantine that stops the entry being served, or null to serve it.
   */
  read(store: string, entry: string, content: unknown, label: Label | null | undefined): string | null {
    const text = contentOf(content);
    const hidden = this.#hidden(store, entry, text, label);
    if (hidden) {
      const write = isLabel(label) ? eventId(label.event) : null;
      this.#refused("read", store, entry, hidden, write ? [write] : []);
      return hidden;
    }
    try {
      this.#read(store, entry, text, isLabel(label) ? label : null, content);
    } catch (error) {
      this._failed("a memory read", error);
    }
    return null;
  }

  /**
   * Writes a memory file and its label, next to it as <file>.wake.json. The label lands
   * first and each file is replaced whole, so the new content is never found with a label
   * that doesn't vouch for it, only the other way round, which reads as changed. Rejects with
   * MemoryLocked when the quarantine locks this agent's writes or hides the file as it is now.
   */
  async writeFile(path: string, text: string): Promise<void> {
    const file = await resolved(path);
    const current = await readFile(path, "utf8").catch(() => undefined);
    const currentLabel = current !== undefined ? await this.#labelOf(path) : null;
    let previous: WriteSnapshot[] = [];
    if (current !== undefined && currentLabel !== null && eventId(currentLabel.event)) {
      const prior = Array.isArray((currentLabel as WriteLabel).previous)
        ? ((currentLabel as WriteLabel).previous ?? [])
        : [];
      previous = [
        {
          event: String(currentLabel.event),
          origins: Array.isArray(currentLabel.origins)
            ? currentLabel.origins.filter((o): o is string => typeof o === "string")
            : [],
          sha256: typeof currentLabel.sha256 === "string" ? currentLabel.sha256 : digest(current),
          agent: typeof currentLabel.agent === "string" ? currentLabel.agent : "",
          content: current,
        },
        ...prior,
      ].slice(0, 16);
    }
    const label = await this.write("file", file, text, current, currentLabel);
    if (label !== null) {
      const fullLabel: WriteLabel = previous.length > 0 ? { ...label, previous } : label;
      try {
        await writeAtomically(path + LABEL_SUFFIX, JSON.stringify(fullLabel));
      } catch (error) {
        this._failed("a memory file's label", error);
      }
    }
    await writeAtomically(file, text);
  }

  /**
   * Rolls a memory file and its .wake.json label back to a prior versioned snapshot (or the
   * immediately preceding snapshot when targetEventId is omitted), recording a memory_write
   * event.
   */
  async rollbackFile(path: string, targetEventId?: string): Promise<WriteLabel> {
    const file = await resolved(path);
    const currentLabel = await this.#labelOf(path);
    const history = currentLabel && Array.isArray((currentLabel as WriteLabel).previous)
      ? ((currentLabel as WriteLabel).previous ?? [])
      : [];
    if (history.length === 0) {
      throw new Error(`no rollback history in ${path}${LABEL_SUFFIX}`);
    }
    const idx = targetEventId ? history.findIndex((h) => h.event === targetEventId) : 0;
    if (idx < 0) {
      throw new Error(`rollback target ${targetEventId} not found in ${path}${LABEL_SUFFIX}`);
    }
    const snapshot = history[idx];
    const remaining = history.slice(idx + 1);
    const restoredLabel: WriteLabel = {
      event: snapshot.event,
      origins: snapshot.origins,
      sha256: snapshot.sha256,
      agent: snapshot.agent,
      ...(remaining.length > 0 ? { previous: remaining } : {}),
    };
    try {
      this.#emit(randomUUID(), events.MEMORY_WRITE, this.#handler._eventContext(), [snapshot.event], [], {
        "wake.memory.store": "file",
        "wake.memory.entry": file,
        "wake.memory.rollback_to": snapshot.event,
        "wake.memory.origins": snapshot.origins,
      }, { store: "file", entry: file, content: snapshot.content, rollback_to: snapshot.event });
    } catch (error) {
      this._failed("a memory rollback", error);
    }
    await writeAtomically(path + LABEL_SUFFIX, JSON.stringify(restoredLabel));
    await writeAtomically(file, snapshot.content);
    return restoredLabel;
  }

  /** Reads a memory file and records the read, with its label if it has one. */
  async readFile(path: string): Promise<string> {
    const text = await readFile(path, "utf8");
    const entry = await resolved(path);
    const hidden = this.read("file", entry, text, await this.#labelOf(path));
    if (hidden) throw new MemoryQuarantined(hidden, "file", entry);
    return text;
  }

  /** The label next to a memory file, if it has one that reads. */
  async #labelOf(path: string): Promise<Label | null> {
    try {
      const label: unknown = JSON.parse(await readFile(path + LABEL_SUFFIX, "utf8"));
      return isLabel(label) ? label : null;
    } catch (error) {
      if ((error as NodeJS.ErrnoException)?.code !== "ENOENT") log.warn("failed to read a memory file's label", error);
      return null;
    }
  }

  async #locked(): Promise<string | null> {
    if (!this.#quarantine) return null;
    try {
      const agent = this.#runAgent();
      // A named run's rules are fetched from its first write, which waits for them.
      if (agent !== undefined) await this.#quarantine.track(agent);
      return this.#quarantine.lockedBy(agent);
    } catch (error) {
      log.warn("failed to check the memory quarantine", error);
      return UNCHECKED;
    }
  }

  /**
   * The named run writing, such as handler.agent("email"), when it isn't the agent the
   * recorder records for: one process can host several agents, and the brake locks each by
   * its own name.
   */
  #runAgent(): string | undefined {
    const agent = this.#handler._currentAgent();
    return agent === this.#handler._recorder.agent ? undefined : agent;
  }

  #hidden(store: string, entry: string, text: string, label: Label | null | undefined): string | null {
    if (!this.#quarantine) return null;
    try {
      const vouched = isLabel(label) && label.sha256 === digest(text);
      return this.#quarantine.hiding(store, entry, isLabel(label) ? label : null, vouched);
    } catch (error) {
      log.warn("failed to check the memory quarantine", error);
      return UNCHECKED;
    }
  }

  #linked(): string[] {
    return links.current()?.parents ?? [];
  }

  /** Records what the quarantine refused, as a denied event. */
  #refused(action: string, store: string, entry: string, quarantine: string, parents: string[]): void {
    try {
      this.#emit(randomUUID(), events.DENIED, this.#handler._eventContext(), parents, [], {
        "wake.memory.store": store,
        "wake.memory.entry": entry,
        "wake.memory.refused": action,
        "wake.brake.halt_id": quarantine,
      }, { store, entry, refused: action, quarantine });
    } catch (error) {
      this._failed(`a refused memory ${action}`, error);
    }
  }

  _failed(what: string, error?: unknown): void {
    log.warn(`failed to record ${what}`, error);
    failed(this.#handler._recorder, what);
  }

  #parentsAndOrigins(content: string): { parents: string[]; origins: string[]; context: Context | undefined } {
    const h = this.#handler;
    const context = h._eventContext();
    const traceId = traceIdOf(context);
    let parents = [...this.#linked()];
    if (traceId) parents.push(...h._matchingOutputs(traceId, content));
    parents = unique(parents);
    const origins = h._originsUpstream(parents);
    return { parents, origins, context };
  }

  #write(store: string, entry: string, content: string): WriteLabel {
    const h = this.#handler;
    const { parents, origins, context } = this.#parentsAndOrigins(content);
    const id = randomUUID();
    const label = { event: id, origins, sha256: digest(content), agent: h._currentAgent() };
    this.#emit(id, events.MEMORY_WRITE, context, parents, [], {
      "wake.memory.store": store,
      "wake.memory.entry": entry,
      "wake.memory.origins": origins,
    }, { store, entry, content });
    return label;
  }

  #delete(store: string, entry: string): void {
    const h = this.#handler;
    const parents = this.#linked();
    const origins = h._originsUpstream(parents);
    this.#emit(randomUUID(), events.MEMORY_WRITE, h._eventContext(), parents, [], {
      "wake.memory.store": store,
      "wake.memory.entry": entry,
      "wake.memory.deleted": true,
      "wake.memory.origins": origins,
    }, { store, entry, deleted: true });
  }

  #read(store: string, entry: string, content: string, label: Label | null, value: unknown): void {
    const h = this.#handler;
    const parents: string[] = [];
    const attributes: Record<string, string | boolean | string[]> = { "wake.memory.store": store, "wake.memory.entry": entry };
    if (label === null) {
      attributes["wake.memory.labeled"] = false;
    } else {
      attributes["wake.memory.labeled"] = true;
      const write = eventId(label.event);
      if (label.sha256 === digest(content) && write) {
        parents.push(write);
        if (Array.isArray(label.origins)) {
          attributes["wake.memory.origins"] = label.origins.filter((o): o is string => typeof o === "string").slice(0, 64);
        }
      } else {
        // The label doesn't vouch for this content: it changed since it was written.
        attributes["wake.memory.changed"] = true;
      }
    }
    const id = randomUUID();
    const span = this.#emit(id, events.MEMORY_READ, h._eventContext(), parents, [`memory:${store}/${entry}`], attributes, {
      store,
      entry,
      content,
      label: label ?? null,
    });
    // What was read can feed later calls in the trace, like a tool's output: as the label's
    // digest covers it, and as JSON.stringify writes it into a prompt.
    h._rememberOutput(span.spanContext().traceId, id, content);
    if (typeof value !== "string") {
      try {
        const compact = JSON.stringify(value);
        if (typeof compact === "string" && compact !== content) h._rememberOutput(span.spanContext().traceId, id, compact);
      } catch {
        // Not JSON as JavaScript writes it; matched as above only.
      }
    }
  }

  #emit(
    id: string,
    kind: string,
    context: Context | undefined,
    parents: string[],
    origins: string[],
    attributes: Record<string, string | boolean | string[]>,
    payload: { store: string; [key: string]: unknown },
  ): Span {
    const h = this.#handler;
    h._rememberLineage(id, parents, origins);
    const span = h._recorder.tracer.startSpan(
      `${kind} ${payload.store}`,
      {
        attributes: {
          [events.EVENT_ID]: id,
          [events.EVENT_KIND]: kind,
          [events.AGENT_ID]: h._currentAgent(),
          [events.CAPTURE]: "memory_adapter",
          [events.PARENTS]: parents,
          [events.ORIGINS]: origins,
          ...attributes,
          ...events.payloadAttributes(payload),
        },
      },
      context,
    );
    span.end();
    return span;
  }
}
