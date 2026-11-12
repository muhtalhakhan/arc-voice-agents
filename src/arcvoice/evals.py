"""Scenario evals: scripted calls with expected outcomes.

Run offline in CI on every change (deterministic classifier), or with
`--live` against a real model before promoting a prompt or model change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import contact_id
from .engine import Conversation
from .llm import IntentClassifier
from .spec import AgentSpec, load_yaml, route
from .store import DEMO_SITES


@dataclass
class EvalResult:
    name: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)


def _site(account: str) -> dict[str, Any]:
    for site in DEMO_SITES:
        if site["account"] == account:
            return site
    raise KeyError(f"no demo site {account}")


def signal_dict(event: contact_id.ContactIdEvent) -> dict[str, Any]:
    return {
        "account": event.account,
        "event_code": event.event_code,
        "description": event.description,
        "category": event.category.value,
        "qualifier": event.qualifier.value,
        "zone": event.zone,
        "raw": event.raw,
    }


def run_scenario(scenario: dict[str, Any], specs: dict[str, AgentSpec], classifier: IntentClassifier) -> EvalResult:
    name = scenario["name"]
    sig = scenario["signal"]
    frame = contact_id.build(sig["account"], sig["event_code"], zone=sig.get("zone", "001"))
    event = contact_id.parse(frame)
    expect = scenario["expect"]
    failures: list[str] = []

    spec = route(specs, event.category)
    if spec is None:
        return EvalResult(name, False, [f"no agent handles {event.category}"])
    if "agent" in expect and spec.id != expect["agent"]:
        failures.append(f"routed to {spec.id}, expected {expect['agent']}")

    convo = Conversation(spec, _site(event.account), signal_dict(event), classifier)
    turn = convo.start()
    for utterance in scenario.get("caller", []):
        if turn.done:
            failures.append(f"call ended before caller said: {utterance!r}")
            break
        turn = convo.respond(utterance)

    state = convo.state
    if not state["done"]:
        failures.append(f"call did not finish (stuck at step {state['step']!r})")
    if "disposition" in expect and state["disposition"] != expect["disposition"]:
        failures.append(f"disposition {state['disposition']!r}, expected {expect['disposition']!r}")
    if "actions" in expect:
        got = sorted(a["type"] for a in state["outbox"])
        if got != sorted(expect["actions"]):
            failures.append(f"actions {got}, expected {sorted(expect['actions'])}")

    agent_text = " ".join(t["text"] for t in state["transcript"] if t["role"] == "agent").lower()
    for phrase in expect.get("must_not_say", []):
        if phrase.lower() in agent_text:
            failures.append(f"agent said forbidden phrase {phrase!r}")
    for phrase in expect.get("must_say", []):
        if phrase.lower() not in agent_text:
            failures.append(f"agent never said {phrase!r}")

    return EvalResult(name, not failures, failures, state["transcript"])


def load_scenarios(directory: str | Path) -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    for path in sorted(Path(directory).glob("*.yaml")):
        scenarios.extend(load_yaml(path.read_text()))
    return scenarios
