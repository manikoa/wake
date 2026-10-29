"""A sub-agent's AWS session is named as IAM takes it, the same way here as in the brake."""

import json
import re
from pathlib import Path

import pytest

from wake.aws import session_name

VECTORS = json.loads((Path(__file__).resolve().parents[2] / "testdata" / "aws_session_names.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(("identity", "name"), VECTORS)
def test_a_session_is_named_as_the_brake_names_it(identity, name):
    assert session_name(identity) == name
    assert re.fullmatch(r"[\w+=,.@-]{2,64}", name, re.ASCII), "IAM takes it"


def test_two_identities_never_share_a_name():
    assert session_name("support/researcher#toolu_1") != session_name("support-researcher-toolu_1")
