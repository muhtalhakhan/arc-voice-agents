import copy
from pathlib import Path

import pytest
from pydantic import ValidationError

from arcvoice.engine import check_tools
from arcvoice.spec import AgentSpec, load_all, load_yaml

AGENTS = Path(__file__).parent.parent / "agents"


def base() -> dict:
    return load_yaml((AGENTS / "trouble_triage.yaml").read_text())


def test_shipped_agents_are_valid():
    specs = load_all(AGENTS)
    assert set(specs) == {"intruder-verification", "fire-verification", "trouble-triage"}
    for spec in specs.values():
        check_tools(spec)


def test_each_category_has_one_owner():
    specs = load_all(AGENTS)
    handled = [c for s in specs.values() for c in s.handles]
    assert len(handled) == len(set(handled))


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda d: d["steps"]["offer_engineer"]["on"].pop("deny"), "no transition"),
        (lambda d: d["steps"]["book"].update(next="nowhere"), "unknown step"),
        (lambda d: d["steps"]["book"].update(tool="request_police_dispatch"), "not declared"),
        (lambda d: d["steps"]["close_booked"].update(disposition="vibes"), "unknown disposition"),
        (lambda d: d.update(version="1.0"), "semver"),
        (
            lambda d: d["steps"].update(island={"kind": "end", "say": "hi", "disposition": "advised_no_action"}),
            "unreachable",
        ),
    ],
)
def test_broken_specs_are_rejected(mutate, error):
    data = copy.deepcopy(base())
    mutate(data)
    with pytest.raises(ValidationError, match=error):
        AgentSpec.model_validate(data)


def test_dead_end_loop_is_rejected():
    data = base()
    data["steps"]["book"] = {"kind": "action", "tool": "book_engineer", "next": "book_again"}
    data["steps"]["book_again"] = {"kind": "action", "tool": "book_engineer", "next": "book"}
    del data["steps"]["close_booked"]
    with pytest.raises(ValidationError, match="never reach an end"):
        AgentSpec.model_validate(data)


def test_yaml_on_and_yes_stay_strings():
    assert load_yaml("on: {yes: no}\nflag: true") == {"on": {"yes": "no"}, "flag": True}
