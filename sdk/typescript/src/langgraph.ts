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

import { LABEL_KEY, type Label, type Memory } from "./memory.ts";

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
 * get or search a memory read. The label is kept in the stored value under __wake__ and taken
 * out of what the app gets back. With a quarantine on the Memory, a locked put or delete
 * throws MemoryLocked, a quarantined entry reads as absent and drops out of searches, and
 * deleting one throws MemoryLocked: it's kept, as evidence.
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
    const results = await this.#store.batch(this.#before(ops, byKey(lookups, found as (Item | null)[])));
    return this.#after(ops, results as unknown[]) as OperationResults<Op>;
  }

  override start(): void {
    this.#store.start();
  }

  override stop(): void {
    this.#store.stop();
  }

  /**
   * What each delete would remove, so the quarantine can refuse to delete an entry it hides.
   * Without a quarantine, there's nothing to look up.
   */
  #lookups(ops: Operation[]): GetOperation[] {
    if (this.#memory.quarantine === undefined) return [];
    return ops.filter((op): op is PutOperation => isPut(op) && op.value === null).map((op) => ({ namespace: op.namespace, key: op.key }));
  }

  #before(ops: Operation[], found: Map<string, Item | null>): Operation[] {
    return ops.map((op) => {
      if (!isPut(op)) return op;
      const store = op.namespace.join("/");
      if (op.value === null) {
        const item = found.get(keyOf(op.namespace, op.key));
        const [value, label] = item ? split(item.value) : [undefined, null];
        this.#memory.delete(store, op.key, value, label); // throws MemoryLocked
        return op;
      }
      const value: Record<string, unknown> = Object.fromEntries(Object.entries(op.value).filter(([k]) => k !== LABEL_KEY));
      const label = this.#memory.write(store, op.key, value);
      if (label !== null) value[LABEL_KEY] = label;
      return { ...op, value };
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

/** A stored value as the app sees it, without its label, and the label. */
function split(value: unknown): [Record<string, unknown>, Label | null] {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return [value as Record<string, unknown>, null];
  const { [LABEL_KEY]: label, ...rest } = value as Record<string, unknown>;
  return [rest, label !== null && typeof label === "object" && !Array.isArray(label) ? (label as Label) : null];
}

function keyOf(namespace: string[], key: string): string {
  return JSON.stringify([namespace, key]);
}

function byKey(lookups: GetOperation[], found: (Item | null)[]): Map<string, Item | null> {
  return new Map(lookups.map((op, i) => [keyOf(op.namespace, op.key), found[i] ?? null]));
}
