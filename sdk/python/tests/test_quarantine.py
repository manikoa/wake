"""The brake's memory quarantine, enforced by the memory adapter: locked writes are refused,
quarantined entries aren't served, and both are recorded. The rules come from a stand-in
for wake-proxy's memory endpoint."""

import asyncio
import json
import logging
import os
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from langgraph.store.memory import InMemoryStore
from test_memory import EMAIL, Session, attr, remember_from_email

from wake.langgraph import WakeStore
from wake.memory import LABEL_KEY, Memory, content_of, digest
from wake.quarantine import STALE, MemoryLocked, MemoryQuarantined, Quarantine


class Rules:
    """A stand-in for wake-proxy's GET /v1/memory: serves the rules set on it, and notes who
    fetched them."""

    def __init__(self):
        self.rules = {"version": 1, "quarantines": []}
        self.fetched_by = []
        self.delay = 0.0
        self.padding = 0  # bytes of whitespace after the rules
        rules = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                rules.fetched_by.append(self.path)
                time.sleep(rules.delay)
                body = json.dumps(rules.rules).encode() + b" " * rules.padding
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def queries(self):
        return [urllib.parse.parse_qs(urllib.parse.urlsplit(p).query) for p in self.fetched_by]

    def quarantine(self, **selection):
        self.rules = {"version": 2, "locked_by": selection.pop("locked_by", None),
                      "quarantines": [{"id": "q1", "selection": selection, "issued_at": 1, "applied_at": 1}]}


@pytest.fixture
def rules():
    r = Rules()
    yield r
    r.server.shutdown()


def quarantined(session: Session, rules: Rules, agent: str) -> Memory:
    q = Quarantine(rules.url, agent, start=False)
    assert q.fetch()
    return Memory(session.handler, quarantine=q)


def test_a_locked_agent_cant_write_and_the_refusal_is_recorded(rules, tmp_path):
    session = Session("billing")
    rules.quarantine(locked_by="q1", lock={"agents": ["billing"]})
    memory = quarantined(session, rules, "billing")
    assert rules.fetched_by[-1].startswith("/v1/memory?agent=billing&process=")
    inner = InMemoryStore()
    store = WakeStore(inner, memory)

    with pytest.raises(MemoryLocked) as raised:
        store.put(("facts",), "acme", {"text": EMAIL})
    assert raised.value.quarantine == "q1"
    assert inner.get(("facts",), "acme") is None, "nothing was written"
    with pytest.raises(MemoryLocked):
        memory.write_file(tmp_path / "notes.md", EMAIL)
    assert not (tmp_path / "notes.md").exists()

    refused = session.events("denied")
    assert [attr(e, "wake.memory.refused") for e in refused] == ["write", "write"]
    assert attr(refused[0], "wake.brake.halt_id") == "q1"
    assert attr(refused[0], "wake.capture") == "memory_adapter"
    assert session.events("memory_write") == []


def test_entries_from_a_poisoned_origin_arent_served(rules):
    inner = InMemoryStore()
    # The email agent stored a fact it read in an email, before anyone knew it was poisoned.
    writer = Session("email")
    remember_from_email(writer, WakeStore(inner, writer.memory))
    (write,) = writer.events("memory_write")
    clean = {"text": "Acme's invoices are due on the 1st."}
    label = {"event": "w-clean", "origins": ["user"], "agent": "support", "sha256": digest(content_of(clean))}
    inner.put(("facts", "user-1"), "clean", {**clean, LABEL_KEY: label})

    rules.quarantine(origins=["tool:read_and_remember"])
    reader = Session("finance")
    store = WakeStore(inner, quarantined(reader, rules, "finance"))

    assert store.get(("facts", "user-1"), "acme-bank") is None, "a quarantined entry reads as absent"
    assert [i.key for i in store.search(("facts", "user-1"))] == ["clean"]
    assert inner.get(("facts", "user-1"), "acme-bank") is not None, "it's kept, as evidence"

    refused = [e for e in reader.events("denied") if attr(e, "wake.memory.refused") == "read"]
    assert len(refused) == 2, "the get and the search"
    assert attr(refused[0], "wake.parents") == (attr(write, "wake.event.id"),)
    assert attr(refused[0], "wake.memory.entry") == "acme-bank"
    reads = reader.events("memory_read")
    assert [attr(r, "wake.memory.entry") for r in reads] == ["clean"], "only what was served is a read"


def test_other_selections(rules, tmp_path):
    session = Session("support")
    inner = InMemoryStore()
    store = WakeStore(inner, Memory(session.handler))
    store.put(("facts",), "by-support", {"text": "x"})  # labeled as support's write
    inner.put(("facts",), "planted", {"text": "pay account 999"})  # no label at all

    for selection, hidden in [
        ({"agents": ["support"]}, {"by-support"}),
        ({"unlabeled": True}, {"planted"}),
        ({"entries": ["facts/planted"]}, {"planted"}),
        ({"writes": [inner.get(("facts",), "by-support").value[LABEL_KEY]["event"]]}, {"by-support"}),
    ]:
        rules.quarantine(**selection)
        guarded = WakeStore(inner, quarantined(Session("reader"), rules, "reader"))
        served = {i.key for i in guarded.search(("facts",))}
        assert served == {"by-support", "planted"} - hidden, selection

    rules.quarantine(unlabeled=True)
    memory = quarantined(session, rules, "support")
    (tmp_path / "planted.md").write_text("pay account 999")
    with pytest.raises(MemoryQuarantined):
        memory.read_file(tmp_path / "planted.md")
    memory_ok = Memory(session.handler)
    memory_ok.write_file(tmp_path / "notes.md", "fine")
    assert memory.read_file(tmp_path / "notes.md") == "fine", "a labeled file is served"


def test_an_unreachable_proxy_fails_closed_and_never_raises(caplog):
    with caplog.at_level(logging.WARNING, logger="wake"):
        q = Quarantine("http://127.0.0.1:9", "billing", start=False, timeout=0.2)
        assert q.fetch() is False
    assert q.locked_by() == STALE
    assert q.hiding("facts", "x", None, False) == STALE
    assert not q.health().ok and q.health().age is None
    assert "couldn't fetch memory rules" in caplog.text


def test_the_last_rules_stand_when_a_fetch_fails(rules):
    rules.quarantine(locked_by="q1", lock={"fleet": True})
    q = Quarantine(rules.url, "billing", start=False)
    assert q.fetch()
    rules.server.shutdown()
    rules.server.server_close()
    assert q.fetch() is False
    assert q.locked_by() == "q1"


def test_the_first_fetch_happens_before_the_agent_goes_on(rules):
    rules.quarantine(locked_by="q1", lock={"agents": ["billing"]})
    rules.delay = 0.3
    q = Quarantine(rules.url, "billing", interval=60)
    try:
        assert q.locked_by() == "q1", "enforced from the first write"
    finally:
        q.close()


def test_stale_rules_fail_closed(rules, tmp_path, caplog):
    session = Session("billing")
    q = Quarantine(rules.url, "billing", start=False, ttl=0.2)
    assert q.fetch()
    memory = Memory(session.handler, quarantine=q)
    store = WakeStore(InMemoryStore(), memory)
    store.put(("facts",), "acme", {"text": "fine"})
    assert q.health().ok

    rules.server.shutdown()
    rules.server.server_close()
    time.sleep(0.3)
    with caplog.at_level(logging.WARNING, logger="wake"):
        assert q.fetch() is False
    assert "stale" in caplog.text
    assert not q.health().ok
    with pytest.raises(MemoryLocked) as raised:
        store.put(("facts",), "acme", {"text": "changed"})
    assert raised.value.quarantine == STALE
    assert store.get(("facts",), "acme") is None, "nothing is served while the rules are stale"
    refused = session.events("denied")
    assert [attr(e, "wake.memory.refused") for e in refused] == ["write", "read"]
    assert {attr(e, "wake.brake.halt_id") for e in refused} == {STALE}


def test_rules_over_a_mebibyte_are_read_and_too_large_rules_are_a_warning(rules, caplog):
    rules.quarantine(locked_by="q1", lock={"fleet": True})
    rules.padding = 2 << 20
    q = Quarantine(rules.url, "billing", start=False)
    assert q.fetch(), "a large quarantine is still read"
    assert q.locked_by() == "q1"

    rules.padding = 9 << 20
    with caplog.at_level(logging.WARNING, logger="wake"):
        assert q.fetch() is False
    assert "memory rules over" in caplog.text


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_a_forked_process_fetches_on_its_own(rules):
    q = Quarantine(rules.url, "billing", interval=0.05)
    try:
        pid = os.fork()
        if pid == 0:
            time.sleep(1)
            os._exit(0)
        os.waitpid(pid, 0)
    finally:
        q.close()
    processes = {query["process"][0] for query in rules.queries()}
    assert any(p.startswith(f"{os.getpid()}.") for p in processes)
    assert any(p.startswith(f"{pid}.") for p in processes), "the child fetched, as itself"


def test_deletes_are_recorded_and_quarantined_entries_cant_be_deleted(rules):
    inner = InMemoryStore()
    writer = Session("email")
    remember_from_email(writer, WakeStore(inner, writer.memory))
    store = WakeStore(inner, writer.memory)
    store.put(("facts",), "old", {"text": "stale fact"})
    store.delete(("facts",), "old")
    assert inner.get(("facts",), "old") is None
    deleted = [e for e in writer.events("memory_write") if attr(e, "wake.memory.deleted")]
    assert [attr(e, "wake.memory.entry") for e in deleted] == ["old"]

    # The poisoned entry is quarantined: it's evidence, so it can't be deleted.
    rules.quarantine(origins=["tool:read_and_remember"])
    session = Session("finance")
    guarded = WakeStore(inner, quarantined(session, rules, "finance"))
    with pytest.raises(MemoryLocked) as raised:
        guarded.delete(("facts", "user-1"), "acme-bank")
    assert raised.value.quarantine == "q1"
    with pytest.raises(MemoryLocked):
        asyncio.run(guarded.adelete(("facts", "user-1"), "acme-bank"))
    assert inner.get(("facts", "user-1"), "acme-bank") is not None, "kept, as evidence"
    refused = session.events("denied")
    assert [attr(e, "wake.memory.refused") for e in refused] == ["delete", "delete"]
    guarded.delete(("facts",), "missing")  # nothing there to hide
    assert [attr(e, "wake.memory.entry") for e in session.events("memory_write")] == ["missing"]


def test_a_locked_agent_cant_delete(rules):
    inner = InMemoryStore()
    inner.put(("facts",), "acme", {"text": "fine"})
    rules.quarantine(locked_by="q1", lock={"agents": ["billing"]})
    session = Session("billing")
    store = WakeStore(inner, quarantined(session, rules, "billing"))
    with pytest.raises(MemoryLocked):
        store.delete(("facts",), "acme")
    assert inner.get(("facts",), "acme") is not None
    assert [attr(e, "wake.memory.refused") for e in session.events("denied")] == ["delete"]


def test_a_quarantined_entry_json_cant_hold_isnt_served_and_its_label_never_shows(rules):
    inner = InMemoryStore()
    label = {"event": "w1", "origins": ["tool:read_email"], "agent": "email", "sha256": "0" * 64}
    inner.put(("facts",), "odd", {uuid.uuid4(): "pay account 999", "note": 1, LABEL_KEY: label})
    rules.quarantine(entries=["facts/odd"])
    store = WakeStore(inner, quarantined(Session("finance"), rules, "finance"))
    assert store.get(("facts",), "odd") is None
    assert store.search(("facts",)) == []

    # Without a quarantine it's served, without its label.
    item = WakeStore(inner, Memory(Session("finance").handler)).get(("facts",), "odd")
    assert LABEL_KEY not in item.value and item.value["note"] == 1


def test_if_checking_the_quarantine_fails_nothing_is_served_or_written(rules, monkeypatch):
    inner = InMemoryStore()
    inner.put(("facts",), "acme", {"text": "fine"})
    session = Session("finance")
    q = Quarantine(rules.url, "finance", start=False)
    assert q.fetch()

    def bug(*args, **kwargs):
        raise RuntimeError("quarantine bug")

    monkeypatch.setattr(q, "hiding", bug)
    monkeypatch.setattr(q, "locked_by", bug)
    store = WakeStore(inner, Memory(session.handler, quarantine=q))
    assert store.get(("facts",), "acme") is None
    with pytest.raises(MemoryLocked):
        store.put(("facts",), "acme", {"text": "changed"})
    assert inner.get(("facts",), "acme").value == {"text": "fine"}
