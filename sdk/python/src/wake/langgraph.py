"""LangGraph support. LangGraph runs are LangChain runs, so wake.langchain's handler records
them; WakeStore records LangGraph's long-term memory store, which langmem uses too.

    memory = Memory(handler)
    store = WakeStore(InMemoryStore(), memory)    # or any BaseStore, such as PostgresStore
    graph = builder.compile(store=store)
"""

from collections.abc import Iterable
from typing import Any

from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    Op,
    PutOp,
    Result,
    SearchItem,
    SearchOp,
)

from wake.langchain import AgentRun, WakeCallbackHandler
from wake.memory import LABEL_KEY, Memory

__all__ = ["AgentRun", "WakeCallbackHandler", "WakeStore"]



class WakeStore(BaseStore):
    """Wraps a LangGraph store so each put or delete is a memory write, each put labeled,
    and each get or search a memory read. The label is kept in the stored value under
    __wake__ and taken out of what the app gets back. With a quarantine on the Memory, a
    locked put or delete raises MemoryLocked, a quarantined entry reads as absent and drops
    out of searches, and deleting one raises MemoryLocked: it's kept, as evidence."""

    def __init__(self, store: BaseStore, memory: Memory) -> None:
        self._store = store
        self._memory = memory

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        ops = list(ops)
        lookups = self._lookups(ops)
        found = self._store.batch(lookups) if lookups else []
        return self._after(ops, self._store.batch(self._before(ops, _by_key(lookups, found))))

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        ops = list(ops)
        lookups = self._lookups(ops)
        found = await self._store.abatch(lookups) if lookups else []
        return self._after(ops, await self._store.abatch(self._before(ops, _by_key(lookups, found))))

    def _lookups(self, ops: list[Op]) -> list[GetOp]:
        """What each delete would remove, so the quarantine can refuse to delete an entry it
        hides. Without a quarantine, there's nothing to look up."""
        if self._memory.quarantine is None:
            return []
        return [GetOp(op.namespace, op.key, refresh_ttl=False) for op in ops if isinstance(op, PutOp) and op.value is None]

    def _before(self, ops: list[Op], found: dict[tuple, Any]) -> list[Op]:
        out: list[Op] = []
        for op in ops:
            if isinstance(op, PutOp) and op.value is None:
                item = found.get((op.namespace, op.key))
                value, label = _split(item.value) if isinstance(item, Item) else (None, None)
                self._memory.delete("/".join(op.namespace), op.key, value, label)  # raises MemoryLocked
            elif isinstance(op, PutOp):
                value = {k: v for k, v in op.value.items() if k != LABEL_KEY}
                label = self._memory.write("/".join(op.namespace), op.key, value)
                if label is not None:
                    value[LABEL_KEY] = label
                op = PutOp(op.namespace, op.key, value, op.index, op.ttl)
            out.append(op)
        return out

    def _after(self, ops: list[Op], results: list[Result]) -> list[Result]:
        out: list[Result] = []
        for op, result in zip(ops, results):
            if isinstance(op, GetOp) and isinstance(result, Item):
                result = self._served(result)
            elif isinstance(op, SearchOp) and isinstance(result, list):
                served = (self._served(item) if isinstance(item, Item) else item for item in result)
                result = [item for item in served if item is not None]
            out.append(result)
        return out

    def _served(self, item: Item) -> Item | None:
        """The item as the app gets it, or None if the quarantine stops it being served.
        Never raises. If reading it fails, it's served without its label, and not at all
        when a quarantine might hide it."""
        try:
            value, label = _split(item.value)
            if self._memory.read("/".join(item.namespace), item.key, value, label):
                return None
            return _with_value(item, value)
        except Exception:
            self._memory._failed("a memory read")
        if self._memory.quarantine is not None:
            return None
        try:
            return _with_value(item, _split(item.value)[0])
        except Exception:
            return None


def _split(value: Any) -> tuple[Any, dict | None]:
    """A stored value as the app sees it, without its label, and the label."""
    if not isinstance(value, dict):
        return value, None
    label = value.get(LABEL_KEY)
    return {k: v for k, v in value.items() if k != LABEL_KEY}, label if isinstance(label, dict) else None


def _with_value(item: Item, value: Any) -> Item:
    fields = {"value": value, "key": item.key, "namespace": item.namespace, "created_at": item.created_at, "updated_at": item.updated_at}
    if isinstance(item, SearchItem):
        return SearchItem(**fields, score=item.score)
    return Item(**fields)


def _by_key(lookups: list[GetOp], found: list[Result]) -> dict[tuple, Any]:
    return {(op.namespace, op.key): item for op, item in zip(lookups, found)}
