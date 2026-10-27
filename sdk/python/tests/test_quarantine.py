"""The brake's memory quarantine, enforced by the memory adapter: locked writes are refused,
quarantined entries aren't served, and both are recorded. The rules come from a stand-in
for wake-proxy's memory endpoint."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from langgraph.store.memory import InMemoryStore
from test_memory import EMAIL, Session, attr, remember_from_email

from wake.langgraph import WakeStore
from wake.memory import LABEL_KEY, Memory, content_of, digest
from wake.quarantine import MemoryLocked, MemoryQuarantined, Quarantine


class Rules:
    """A stand-in for wake-proxy's GET /v1/memory: serves the rules set on it, and notes who
    fetched them."""

    def __init__(self):
        self.rules = {"version": 1, "quarantines": []}
        self.fetched_by = []
        rules = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                rules.fetched_by.append(self.path)
                body = json.dumps(rules.rules).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

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
    assert rules.fetched_by[-1] == "/v1/memory?agent=billing"
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


def test_an_unreachable_proxy_enforces_nothing_and_never_raises():
    q = Quarantine("http://127.0.0.1:9", "billing", start=False, timeout=0.2)
    assert q.fetch() is False
    assert q.locked_by() is None
    assert q.hiding("facts", "x", None, False) is None


def test_the_last_rules_stand_when_a_fetch_fails(rules):
    rules.quarantine(locked_by="q1", lock={"fleet": True})
    q = Quarantine(rules.url, "billing", start=False)
    assert q.fetch()
    rules.server.shutdown()
    rules.server.server_close()
    assert q.fetch() is False
    assert q.locked_by() == "q1"
