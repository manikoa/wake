"""LangGraph support. LangGraph runs are LangChain runs, so wake.langchain's handler records
them; WakeStore records LangGraph's long-term memory store, which langmem uses too.

    memory = Memory(handler)
    store = WakeStore(InMemoryStore(), memory)    # or any BaseStore, such as PostgresStore
    graph = builder.compile(store=store)
"""

import logging
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

log = logging.getLogger("wake")


class WakeStore(BaseStore):
    """Wraps a LangGraph store so each put is a labeled memory write and each get or search
    a memory read. The label is kept in the stored value under __wake__ and taken out of
    what the app gets back. With a quarantine on the Memory, a locked put raises
    MemoryLocked, and a quarantined entry reads as absent and drops out of searches."""

    def __init__(self, store: BaseStore, memory: Memory) -> None:
        self._store = store
        self._memory = memory

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        ops = list(ops)
        return self._after(ops, self._store.batch(self._before(ops)))

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        ops = list(ops)
        return self._after(ops, await self._store.abatch(self._before(ops)))

    def _before(self, ops: list[Op]) -> list[Op]:
        out: list[Op] = []
        for op in ops:
            if isinstance(op, PutOp) and op.value is not None:
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
            try:
                if isinstance(op, GetOp) and isinstance(result, Item):
                    result = self._read(result)
                elif isinstance(op, SearchOp) and isinstance(result, list):
                    served = (self._read(item) if isinstance(item, Item) else item for item in result)
                    result = [item for item in served if item is not None]
            except Exception:
                log.warning("wake: failed to record a memory read", exc_info=True)
            out.append(result)
        return out

    def _read(self, item: Item) -> Item | None:
        """The item as the app gets it, or None if the quarantine stops it being served."""
        label = item.value.get(LABEL_KEY) if isinstance(item.value, dict) else None
        value = {k: v for k, v in item.value.items() if k != LABEL_KEY} if isinstance(item.value, dict) else item.value
        if self._memory.read("/".join(item.namespace), item.key, value, label if isinstance(label, dict) else None):
            return None
        fields = {"value": value, "key": item.key, "namespace": item.namespace, "created_at": item.created_at, "updated_at": item.updated_at}
        if isinstance(item, SearchItem):
            return SearchItem(**fields, score=item.score)
        return Item(**fields)
