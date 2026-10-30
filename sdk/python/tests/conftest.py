"""What every test shares: a process nothing has entered yet, and the brake's rules served with
a flow policy."""

import pytest

from wake import flow
from wake.quarantine import Quarantine


@pytest.fixture(autouse=True)
def nothing_entered_yet(monkeypatch):
    """What entered each agent is kept for the whole process (wake.flow), so each test starts
    from a process nothing has entered, rather than with the agents of the tests before it."""
    monkeypatch.setattr(flow, "_entered", flow.Origins())


@pytest.fixture
def served():
    """The brake's rules for an agent, fetched from a stand-in for wake-proxy's memory endpoint
    (test_quarantine.Rules) that serves them with this flow policy, or with none, and any more
    fields given, such as locked_by."""
    from test_quarantine import Rules

    stand_in = Rules()

    def rules(policy, agent: str, **more) -> Quarantine:
        stand_in.rules = {"version": 1, "quarantines": [], "halts": [], **more}
        if policy is not None:
            stand_in.rules["flow"] = policy
        quarantine = Quarantine(stand_in.url, agent, start=False)
        assert quarantine.fetch()
        return quarantine

    yield rules
    stand_in.server.shutdown()
