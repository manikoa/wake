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
 * id, the origins upstream of it (user, tool:..., agent:...), and a digest of the content,
 * which the event carries too, as wake.memory.sha256. Store the label with the entry, so a
 * later read, by any agent in any session, knows where the entry came from without asking the
 * graph.
 *
 * A delete is a memory_write event too, with wake.memory.deleted set and no label to keep.
 *
 * A read is a memory_read event with origin memory:<store>/<entry>, and the digest of what was
 * read. When the entry carries a label whose digest matches what was read, the read's parent
 * is the write. An entry with no label, such as one planted or written outside Wake, or one
 * changed since it was written, gets no parent, so the completeness metric flags it rather
 * than trusting it.
 *
 * Wrappers: WakeStore in wake-sdk/langgraph wraps LangGraph's BaseStore, and writeFile /
 * readFile handle memory files. Both keep the versions an entry held in its label's history,
 * and roll back to one (WakeStore.rollback, rollbackFile). Recording never blocks or breaks
 * the agent: if it fails, the write or read still happens, unlabeled, and the failure is
 * counted in the recorder's health.
 *
 * A gate, given to Memory, sees each write, delete and rollback before it happens, and refuses
 * it by answering false. A gate that throws or rejects, or that can't be asked because the
 * write's origins can't be worked out, refuses it too. A refused write rejects with
 * MemoryLocked under GATE_DENIED.
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
/** The most versions a label's history keeps. */
export const MAX_HISTORY = 16;
/** What a read's payload keeps of the label: not its history, which can be far larger than the entry. */
const LABEL_FIELDS = ["event", "origins", "sha256", "agent"] as const;

/** A version of an entry its label keeps: what it held, a memory file's text or a store's value, and its label. */
export type WriteSnapshot<T = unknown> = {
  event: string;
  origins: string[];
  sha256: string;
  agent: string;
  content: T;
};

/** What write() hands back, to keep with the entry. */
export type WriteLabel<T = unknown> = {
  event: string;
  origins: string[];
  sha256: string;
  agent: string;
  previous?: WriteSnapshot<T>[];
};

/** Asked before each write, delete and rollback; content is null for a delete. */
export type WriteGate = (store: string, entry: string, content: string | null, origins: string[]) => boolean | Promise<boolean>;

/** A write's parents, and the origins upstream of them. */
type Lineage = { parents: string[]; origins: string[] };

export function digest(content: string): string {
  return events.sha256(content);
}

/**
 * The text of an entry's content, as the label's digest covers it. Never throws. A value is
 * its JSON, keys sorted, with each whole number in it an integer, as the Python SDK writes it.
 */
export function contentOf(value: unknown): string {
  return typeof value === "string" ? value : events.dumps(value, { ensureAscii: false });
}

/** An event id from a label, which anything that can write the store can forge, if it looks like one. */
function eventId(value: unknown): string | null {
  return typeof value === "string" && links.EVENT_ID.test(value) ? value : null;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isLabel(value: unknown): value is Label {
  return isObject(value);
}

function isText(value: unknown): value is string {
  return typeof value === "string";
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

/** A label as a read's payload records it. */
function withoutHistory(label: Label | null): Record<string, unknown> | null {
  return label === null ? null : Object.fromEntries(LABEL_FIELDS.filter((k) => k in label).map((k) => [k, label[k]]));
}

/**
 * The history a new version of an entry keeps in its label: the version it replaces, when that
 * version's label vouches for it, then that label's own history, newest first.
 */
function historyOf(label: Label | null | undefined, current: unknown): WriteSnapshot[] {
  if (!isLabel(label)) return [];
  const versions = Array.isArray(label.previous) ? (label.previous.filter(isObject) as WriteSnapshot[]) : [];
  const event = eventId(label.event);
  // Content changed since its label was written isn't kept: no label vouches for it.
  if (current !== undefined && current !== null && event && label.sha256 === digest(contentOf(current))) {
    versions.unshift({
      event,
      origins: strings(label.origins),
      sha256: label.sha256 as string,
      agent: typeof label.agent === "string" ? label.agent : "",
      content: current,
    });
  }
  return versions.slice(0, MAX_HISTORY);
}

/**
 * The version a rollback restores, from a label's history: the newest, or the one target names.
 * Throws when there's none, or it names no write or doesn't match its own digest: anything
 * that can write the store can write its history.
 */
function versionOf<T>(
  label: Label | null | undefined,
  target: string | undefined,
  restorable: (content: unknown) => content is T,
  where: string,
): WriteSnapshot<T> {
  const versions = isLabel(label) && Array.isArray(label.previous) ? label.previous.filter(isObject) : [];
  if (versions.length === 0) throw new Error(`no rollback history in ${where}`);
  const version = target === undefined ? versions[0] : versions.find((v) => v.event === target);
  if (version === undefined) throw new Error(`rollback target ${target} not found in ${where}`);
  const event = eventId(version.event);
  const { content, sha256 } = version;
  if (!event || typeof sha256 !== "string" || !restorable(content) || digest(contentOf(content)) !== sha256) {
    throw new Error(`rollback version in ${where} doesn't check out: it must name a write and match its digest`);
  }
  return { event, origins: strings(version.origins), sha256, agent: typeof version.agent === "string" ? version.agent : "", content };
}

function describe(error: unknown): string {
  try {
    return error instanceof Error ? `${error.name}: ${error.message}` : String(error);
  } catch {
    return "an error that can't be described";
  }
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

  /**
   * gate, if given, is asked before each write, with the content's text and the origins
   * upstream of it; before each delete, with content null; and before each rollback, with the
   * content and origins of the version it restores. A falsy answer refuses it, and so does a
   * gate that throws or rejects.
   */
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
   * kept, as evidence, not overwritten. And when the gate refuses it.
   */
  async write(store: string, entry: string, content: unknown, current?: unknown, label?: Label | null): Promise<WriteLabel | null> {
    return (await this._admitWrite(store, entry, content, current, label))();
  }

  /**
   * Checks a write as write() does, and resolves to what records it, so a batch can be checked
   * whole before any of it is recorded. With history, the label it records keeps the version
   * the write replaces.
   */
  async _admitWrite(
    store: string,
    entry: string,
    content: unknown,
    current?: unknown,
    label?: Label | null,
    { history = false }: { history?: boolean } = {},
  ): Promise<() => WriteLabel | null> {
    await this.#check("write", store, entry, current, label);
    const text = contentOf(content);
    const lineage = await this.#gated("write", store, entry, text, () => this.#parentsAndOrigins(text));
    const previous = history ? historyOf(label, current) : [];
    return () => {
      let written: WriteLabel;
      try {
        written = this.#write(store, entry, text, lineage);
      } catch (error) {
        this._failed("a memory write", error);
        return null;
      }
      return previous.length > 0 ? { ...written, previous } : written;
    };
  }

  /**
   * Records the delete of an entry, given what it holds now (undefined if nothing) and the
   * label stored with it. Rejects with MemoryLocked, before anything is deleted, when the
   * brake's quarantine locks this agent's writes, or hides the entry: a quarantined entry is
   * kept, as evidence. And when the gate refuses it.
   */
  async delete(store: string, entry: string, current?: unknown, label?: Label | null): Promise<void> {
    (await this._admitDelete(store, entry, current, label))();
  }

  /** Checks a delete as delete() does, and resolves to what records it. */
  async _admitDelete(store: string, entry: string, current?: unknown, label?: Label | null): Promise<() => void> {
    await this.#check("delete", store, entry, current, label);
    const lineage = await this.#gated("delete", store, entry, null, () => this.#deleteLineage());
    return () => {
      try {
        this.#delete(store, entry, lineage);
      } catch (error) {
        this._failed("a memory delete", error);
      }
    };
  }

  /**
   * Checks a rollback of an entry to a version its label's history keeps, and resolves to the
   * version's content and what records the rollback, which gives the label to keep with it. A
   * rollback is a write, so it's refused as one, and also when the quarantine wouldn't serve
   * the version: restoring it would give it a label the quarantine no longer matches. Rejects
   * with MemoryLocked when refused, and with an Error when there's no such version or it
   * doesn't match its digest.
   */
  async _admitRollback<T>(
    store: string,
    entry: string,
    current: unknown,
    label: Label | null | undefined,
    target: string | undefined,
    restorable: (content: unknown) => content is T,
    where: string,
  ): Promise<[T, () => WriteLabel | null]> {
    await this.#check("write", store, entry, current, label);
    const version = versionOf(label, target, restorable, where);
    const text = contentOf(version.content);
    const hidden = this.#hidden(store, entry, text, version);
    if (hidden) {
      this.#refused("write", store, entry, hidden, [...this.#linked(), version.event]);
      throw new MemoryLocked(hidden, store, entry);
    }
    await this.#gated("write", store, entry, text, () => ({ parents: [version.event], origins: version.origins }));
    // The version it replaces is kept too, so a rollback can be rolled back.
    const previous = historyOf(label, current);
    const record = (): WriteLabel | null => {
      let restored: WriteLabel;
      try {
        restored = this.#rollback(store, entry, text, version.event, version.origins);
      } catch (error) {
        this._failed("a memory rollback", error);
        return null;
      }
      return previous.length > 0 ? { ...restored, previous } : restored;
    };
    return [version.content, record];
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
   * Asks the gate, if there is one, and resolves to the parents and origins it judged by.
   * Rejects with MemoryLocked, and records the refusal, when it refuses, when it throws or
   * rejects, and when the origins it needs can't be worked out: it fails closed.
   */
  async #gated(action: string, store: string, entry: string, content: string | null, lineage: () => Lineage): Promise<Lineage | undefined> {
    if (!this.#gate) return undefined;
    let judged: Lineage;
    try {
      judged = lineage();
    } catch (error) {
      this._failed(`a memory ${action}'s origins`, error);
      this.#refused(action, store, entry, GATE_DENIED, this.#linked(), "origins unavailable");
      throw new MemoryLocked(GATE_DENIED, store, entry);
    }
    let allowed = false;
    let reason: string | undefined;
    try {
      allowed = Boolean(await this.#gate(store, entry, content, [...judged.origins]));
    } catch (error) {
      log.warn(`the memory gate failed; refusing the ${action}`, error);
      reason = describe(error);
    }
    if (!allowed) {
      this.#refused(action, store, entry, GATE_DENIED, judged.parents, reason);
      throw new MemoryLocked(GATE_DENIED, store, entry);
    }
    return judged;
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
   * that doesn't vouch for it, only the other way round, which reads as changed. The label
   * keeps the versions the file held, at most MAX_HISTORY, for rollbackFile. Rejects with
   * MemoryLocked when the quarantine locks this agent's writes or hides the file as it is now,
   * or the gate refuses the write.
   */
  async writeFile(path: string, text: string): Promise<void> {
    const file = await resolved(path);
    const current = await this.#current(path);
    const currentLabel = current !== undefined ? await this.#labelOf(path) : null;
    const label = (await this._admitWrite("file", file, text, current, currentLabel, { history: true }))();
    if (label !== null) {
      try {
        await writeAtomically(path + LABEL_SUFFIX, JSON.stringify(label));
      } catch (error) {
        this._failed("a memory file's label", error);
      }
    }
    await writeAtomically(file, text);
  }

  /**
   * Rolls a memory file back to a version its label keeps: the newest, or the one
   * targetEventId names. A rollback is a write: it rejects with MemoryLocked when writeFile
   * would, when the quarantine wouldn't serve the version, or when the gate refuses its
   * content, and with an Error, restoring nothing, when there's no such version or it doesn't
   * match its digest. It's recorded as a memory_write naming the write it restores. Resolves
   * to the new label, which names the rollback and keeps the version it replaced, so it can
   * be rolled back too; null if recording failed.
   */
  async rollbackFile(path: string, targetEventId?: string): Promise<WriteLabel | null> {
    const file = await resolved(path);
    const current = await readFile(path, "utf8").catch((error: NodeJS.ErrnoException) => {
      if (error?.code === "ENOENT") return undefined;
      throw error;
    });
    const [content, record] = await this._admitRollback(
      "file", file, current, await this.#labelOf(path), targetEventId, isText, path + LABEL_SUFFIX,
    );
    const label = record();
    if (label !== null) {
      try {
        await writeAtomically(path + LABEL_SUFFIX, JSON.stringify(label));
      } catch (error) {
        this._failed("a memory file's label", error);
      }
    }
    await writeAtomically(file, content);
    return label;
  }

  /** Reads a memory file and records the read, with its label if it has one. */
  async readFile(path: string): Promise<string> {
    const text = await readFile(path, "utf8");
    const entry = await resolved(path);
    const hidden = this.read("file", entry, text, await this.#labelOf(path));
    if (hidden) throw new MemoryQuarantined(hidden, "file", entry);
    return text;
  }

  /**
   * What a memory file holds now, or undefined if nothing. One that can't be read is refused
   * under a quarantine, which can't check what it holds.
   */
  async #current(path: string): Promise<string | undefined> {
    try {
      return await readFile(path, "utf8");
    } catch (error) {
      if ((error as NodeJS.ErrnoException)?.code === "ENOENT" || this.#quarantine === undefined) return undefined;
      throw error;
    }
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

  /**
   * Records what the quarantine or the gate refused, as a denied event, with why the gate
   * couldn't judge it, if it couldn't.
   */
  #refused(action: string, store: string, entry: string, quarantine: string, parents: string[], reason?: string): void {
    const payload: { store: string; [key: string]: unknown } = { store, entry, refused: action, quarantine };
    if (reason !== undefined) payload.error = reason;
    try {
      this.#emit(randomUUID(), events.DENIED, this.#handler._eventContext(), parents, [], {
        "wake.memory.store": store,
        "wake.memory.entry": entry,
        "wake.memory.refused": action,
        "wake.brake.halt_id": quarantine,
      }, payload);
    } catch (error) {
      this._failed(`a refused memory ${action}`, error);
    }
  }

  _failed(what: string, error?: unknown): void {
    log.warn(`failed to record ${what}`, error);
    failed(this.#handler._recorder, what);
  }

  #parentsAndOrigins(content: string): Lineage {
    const h = this.#handler;
    const traceId = traceIdOf(h._eventContext());
    let parents = [...this.#linked()];
    if (traceId) parents.push(...h._matchingOutputs(traceId, content));
    parents = unique(parents);
    return { parents, origins: h._originsUpstream(parents) };
  }

  #deleteLineage(): Lineage {
    const parents = this.#linked();
    return { parents, origins: this.#handler._originsUpstream(parents) };
  }

  #write(store: string, entry: string, content: string, lineage?: Lineage): WriteLabel {
    const h = this.#handler;
    const context = h._eventContext();
    const { parents, origins } = lineage ?? this.#parentsAndOrigins(content);
    const id = randomUUID();
    const sha256 = digest(content);
    const label = { event: id, origins, sha256, agent: h._currentAgent() };
    this.#emit(id, events.MEMORY_WRITE, context, parents, [], {
      "wake.memory.store": store,
      "wake.memory.entry": entry,
      "wake.memory.origins": origins,
      "wake.memory.sha256": sha256,
    }, { store, entry, content });
    return label;
  }

  /**
   * Records a rollback, and returns the label to keep with what it restores. Its parent is the
   * write it restores, whose origins it carries.
   */
  #rollback(store: string, entry: string, content: string, version: string, origins: string[]): WriteLabel {
    const h = this.#handler;
    const id = randomUUID();
    const sha256 = digest(content);
    this.#emit(id, events.MEMORY_WRITE, h._eventContext(), [version], [], {
      "wake.memory.store": store,
      "wake.memory.entry": entry,
      "wake.memory.rollback_to": version,
      "wake.memory.origins": origins,
      "wake.memory.sha256": sha256,
    }, { store, entry, content, rollback_to: version });
    return { event: id, origins, sha256, agent: h._currentAgent() };
  }

  #delete(store: string, entry: string, lineage?: Lineage): void {
    const h = this.#handler;
    const { parents, origins } = lineage ?? this.#deleteLineage();
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
    const sha256 = digest(content);
    const attributes: Record<string, string | boolean | string[]> = {
      "wake.memory.store": store,
      "wake.memory.entry": entry,
      "wake.memory.sha256": sha256,
    };
    if (label === null) {
      attributes["wake.memory.labeled"] = false;
    } else {
      attributes["wake.memory.labeled"] = true;
      const write = eventId(label.event);
      if (label.sha256 === sha256 && write) {
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
      label: withoutHistory(label),
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
