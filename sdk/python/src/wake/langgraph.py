"""LangGraph support. LangGraph runs are LangChain runs, so wake.langchain's handler records
them; WakeStore records LangGraph's long-term memory store, which langmem uses too.

    memory = Memory(handler)
    store = WakeStore(InMemoryStore(), memory)    # or any BaseStore, such as PostgresStore
    graph = builder.compile(store=store)
"""

from collections.abc import Callable, Iterable
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
    __wake__, with the versions the entry held before it, and taken out of what the app
    gets back; rollback() restores one of those versions. With a quarantine on the Memory, a
    locked put or delete raises MemoryLocked, a quarantined entry reads as absent and drops
    out of searches, and overwriting or deleting one raises MemoryLocked: it's kept, as
    evidence. A batch is checked whole before any of it is recorded: if one put or delete
    is refused, none is recorded or run."""

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

    def rollback(self, namespace: tuple[str, ...], key: str, target_event_id: str | None = None) -> dict[str, Any] | None:
        """Rolls an entry back to a version its label keeps, as Memory.rollback_file does a
        memory file: the newest, or the one target_event_id names. A rollback is a write: it
        raises MemoryLocked when a put would, when the quarantine wouldn't serve the version,
        or when the gate refuses it, and ValueError, restoring nothing, when there's no such
        version or it doesn't match its digest. Returns the new label, which names the
        rollback and keeps the version it replaced; None if recording failed."""
        item = self._store.get(namespace, key, refresh_ttl=False)
        value, label = self._rolled_back(namespace, key, item, target_event_id)
        self._store.put(namespace, key, value)
        return label

    async def arollback(
        self, namespace: tuple[str, ...], key: str, target_event_id: str | None = None
    ) -> dict[str, Any] | None:
        """rollback(), for an async store."""
        item = await self._store.aget(namespace, key, refresh_ttl=False)
        value, label = self._rolled_back(namespace, key, item, target_event_id)
        await self._store.aput(namespace, key, value)
        return label

    def _rolled_back(
        self, namespace: tuple[str, ...], key: str, item: Item | None, target: str | None
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """The value to put back, labeled, once the rollback is checked and recorded."""
        current, current_label = _split(item.value) if isinstance(item, Item) else (None, None)
        store = "/".join(namespace)
        content, record = self._memory._admit_rollback(
            store, key, current, current_label, target, lambda c: isinstance(c, dict) and LABEL_KEY not in c, f"{store}/{key}"
        )
        label = record()
        value = dict(content)
        if label is not None:
            value[LABEL_KEY] = label
        return value, label

    def _lookups(self, ops: list[Op]) -> list[GetOp]:
        """What each put or delete would replace or remove: the quarantine refuses to
        overwrite or delete an entry it hides, and a put keeps the version it replaces."""
        return [GetOp(op.namespace, op.key, refresh_ttl=False) for op in ops if isinstance(op, PutOp)]

    def _before(self, ops: list[Op], found: dict[tuple, Any]) -> list[Op]:
        """The ops to run, each put labeled. Every put and delete is checked before any is
        recorded, so the log never says a write happened in a batch that didn't run."""
        checked: list[tuple[Op, Callable[[], Any] | None]] = []
        for op in ops:
            if not isinstance(op, PutOp):
                checked.append((op, None))
                continue
            item = found.get((op.namespace, op.key))
            current, current_label = _split(item.value) if isinstance(item, Item) else (None, None)
            store = "/".join(op.namespace)
            if op.value is None:
                # Raises MemoryLocked when the quarantine or the gate refuses it.
                checked.append((op, self._memory._admit_delete(store, op.key, current, current_label)))
                continue
            value = {k: v for k, v in op.value.items() if k != LABEL_KEY}
            # Raises MemoryLocked when the entry it replaces is quarantined, or the gate refuses it.
            record = self._memory._admit_write(store, op.key, value, current, current_label, history=True)
            checked.append((PutOp(op.namespace, op.key, value, op.index, op.ttl), record))
        out: list[Op] = []
        for op, record in checked:
            label = record() if record is not None else None
            if label is not None:
                op.value[LABEL_KEY] = label
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
    """A stored value as the app sees it, without its label and the history in it, and the
    label."""
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
