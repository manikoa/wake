/**
 * LangGraph support. LangGraph runs are LangChain runs, so the handler in wake-sdk/langchain
 * records them; WakeStore records LangGraph's long-term memory store.
 *
 *     const memory = new Memory(handler);
 *     const store = new WakeStore(new InMemoryStore(), memory); // or any BaseStore, such as PostgresStore
 *     const graph = builder.compile({ store });
 */

import {
  BaseStore,
  type GetOperation,
  type Item,
  type Operation,
  type OperationResults,
  type PutOperation,
  type SearchOperation,
} from "@langchain/langgraph";

import { LABEL_KEY, type Label, type Memory, type WriteLabel } from "./memory.ts";

type SearchItem = Item & { score?: number };

export { AgentRun, WakeCallbackHandler } from "./langchain.ts";

function isPut(op: Operation): op is PutOperation {
  return "value" in op && "key" in op;
}

function isGet(op: Operation): op is GetOperation {
  return "key" in op && !("value" in op);
}

function isSearch(op: Operation): op is SearchOperation {
  return "namespacePrefix" in op;
}

/**
 * Wraps a LangGraph store so each put or delete is a memory write, each put labeled, and each
 * get or search a memory read. The label is kept in the stored value under __wake__, with the
 * versions the entry held before it, and taken out of what the app gets back; rollback()
 * restores one of those versions. With a quarantine on the Memory, a locked put or delete
 * throws MemoryLocked, a quarantined entry reads as absent and drops out of searches, and
 * overwriting or deleting one throws MemoryLocked: it's kept, as evidence. A batch is checked
 * whole before any of it is recorded: if one put or delete is refused, none is recorded or run.
 */
export class WakeStore extends BaseStore {
  readonly #store: BaseStore;
  readonly #memory: Memory;

  constructor(store: BaseStore, memory: Memory) {
    super();
    this.#store = store;
    this.#memory = memory;
  }

  async batch<Op extends Operation[]>(operations: Op): Promise<OperationResults<Op>> {
    const ops = [...operations];
    const lookups = this.#lookups(ops);
    const found = lookups.length > 0 ? await this.#store.batch(lookups) : [];
    const results = await this.#store.batch(await this.#before(ops, byKey(lookups, found as (Item | null)[])));
    return this.#after(ops, results as unknown[]) as OperationResults<Op>;
  }

  /**
   * Rolls an entry back to a version its label keeps, as Memory.rollbackFile does a memory
   * file: the newest, or the one targetEventId names. A rollback is a write: it rejects with
   * MemoryLocked when a put would, when the quarantine wouldn't serve the version, or when the
   * gate refuses it, and with an Error, restoring nothing, when there's no such version or it
   * doesn't match its digest. Resolves to the new label, which names the rollback and keeps
   * the version it replaced; null if recording failed.
   */
  async rollback(namespace: string[], key: string, targetEventId?: string): Promise<WriteLabel | null> {
    const item = await this.#store.get(namespace, key);
    const [current, currentLabel] = item ? split(item.value) : [undefined, null];
    const store = namespace.join("/");
    const [content, record] = await this.#memory._admitRollback(
      store, key, current, currentLabel, targetEventId, restorable, `${store}/${key}`,
    );
    const label = record();
    await this.#store.put(namespace, key, label === null ? { ...content } : { ...content, [LABEL_KEY]: label });
    return label;
  }

  override start(): void {
    this.#store.start();
  }

  override stop(): void {
    this.#store.stop();
  }

  /**
   * What each put or delete would replace or remove: the quarantine refuses to overwrite or
   * delete an entry it hides, and a put keeps the version it replaces.
   */
  #lookups(ops: Operation[]): GetOperation[] {
    return ops.filter(isPut).map((op) => ({ namespace: op.namespace, key: op.key }));
  }

  /**
   * The ops to run, each put labeled. Every put and delete is checked before any is recorded,
   * so the log never says a write happened in a batch that didn't run.
   */
  async #before(ops: Operation[], found: Map<string, Item | null>): Promise<Operation[]> {
    const checked: [Operation, (() => WriteLabel | null | void) | null][] = [];
    for (const op of ops) {
      if (!isPut(op)) {
        checked.push([op, null]);
        continue;
      }
      const store = op.namespace.join("/");
      const item = found.get(keyOf(op.namespace, op.key));
      const [current, currentLabel] = item ? split(item.value) : [undefined, null];
      if (op.value === null) {
        // Throws MemoryLocked when the quarantine or the gate refuses it.
        checked.push([op, await this.#memory._admitDelete(store, op.key, current, currentLabel)]);
        continue;
      }
      const value: Record<string, unknown> = Object.fromEntries(Object.entries(op.value).filter(([k]) => k !== LABEL_KEY));
      // Throws MemoryLocked when the entry it replaces is quarantined, or the gate refuses it.
      const record = await this.#memory._admitWrite(store, op.key, value, current, currentLabel, { history: true });
      checked.push([{ ...op, value }, record]);
    }
    return checked.map(([op, record]) => {
      const label = record?.();
      if (label && isPut(op) && op.value !== null) op.value[LABEL_KEY] = label;
      return op;
    });
  }

  #after(ops: Operation[], results: unknown[]): unknown[] {
    return ops.map((op, i) => {
      const result = results[i];
      if (isGet(op) && result) return this.#served(result as Item);
      if (isSearch(op) && Array.isArray(result)) {
        return result.map((item: SearchItem) => (item ? this.#served(item) : item)).filter((item) => item !== null);
      }
      return result;
    });
  }

  /**
   * The item as the app gets it, or null if the quarantine stops it being served. Never
   * throws. If reading it fails, it's served without its label, and not at all when a
   * quarantine might hide it.
   */
  #served<T extends Item>(item: T): T | null {
    try {
      const [value, label] = split(item.value);
      if (this.#memory.read(item.namespace.join("/"), item.key, value, label)) return null;
      return { ...item, value };
    } catch (error) {
      this.#memory._failed("a memory read", error);
    }
    if (this.#memory.quarantine !== undefined) return null;
    try {
      return { ...item, value: split(item.value)[0] };
    } catch {
      return null;
    }
  }
}

/** A stored value as the app sees it, without its label and the history in it, and the label. */
function split(value: unknown): [Record<string, unknown>, Label | null] {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return [value as Record<string, unknown>, null];
  const { [LABEL_KEY]: label, ...rest } = value as Record<string, unknown>;
  return [rest, label !== null && typeof label === "object" && !Array.isArray(label) ? (label as Label) : null];
}

/** A version a rollback can put back: a value, as a store holds, with no label of its own. */
function restorable(content: unknown): content is Record<string, unknown> {
  return content !== null && typeof content === "object" && !Array.isArray(content) && !(LABEL_KEY in content);
}

function keyOf(namespace: string[], key: string): string {
  return JSON.stringify([namespace, key]);
}

function byKey(lookups: GetOperation[], found: (Item | null)[]): Map<string, Item | null> {
  return new Map(lookups.map((op, i) => [keyOf(op.namespace, op.key), found[i] ?? null]));
}
