from pathlib import Path

import pytest

from arcvoice import contact_id
from arcvoice.engine import Conversation
from arcvoice.evals import load_scenarios, run_scenario, signal_dict
from arcvoice.llm import KeywordClassifier
from arcvoice.spec import load_all
from arcvoice.store import DEMO_SITES

ROOT = Path(__file__).parent.parent
SPECS = load_all(ROOT / "agents")
SITE = DEMO_SITES[0]


def convo(code: str = "130", zone: str = "003", agent: str = "intruder-verification") -> Conversation:
    event = contact_id.parse(contact_id.build(SITE["account"], code, zone=zone))
    return Conversation(SPECS[agent], SITE, signal_dict(event), KeywordClassifier())


def test_greeting_names_site_but_not_address():
    turn = convo().start()
    assert "Harbour Street Pharmacy" in turn.say
    assert SITE["address"] not in turn.say


def test_passcode_is_redacted_from_transcript_and_tool_log():
    c = convo()
    c.start()
    c.respond("yes")
    c.respond("bluebell")
    text = str(c.state["transcript"]) + str(c.state["tool_calls"])
    assert "bluebell" not in text
    assert "[redacted]" in text


def test_police_guardrail_blocks_dispatch_without_grounds():
    c = convo()
    c.start()
    from arcvoice.tools import REGISTRY, ToolContext

    ctx = ToolContext(site=SITE, signal=c.signal, state={}, outbox=[])
    assert REGISTRY["request_police_dispatch"](ctx, {}).outcome == "blocked"
    assert ctx.outbox == []


def test_panic_signal_is_grounds_for_police():
    from arcvoice.tools import REGISTRY, ToolContext

    event = contact_id.parse(contact_id.build(SITE["account"], "120"))
    ctx = ToolContext(site=SITE, signal=signal_dict(event), state={}, outbox=[])
    assert REGISTRY["request_police_dispatch"](ctx, {}).outcome == "ok"


def test_cannot_respond_after_call_ends():
    c = convo()
    c.start()
    for line in ["yes", "bluebell", "false alarm, my fault"]:
        turn = c.respond(line)
    assert turn.done and turn.disposition == "false_alarm_confirmed"
    with pytest.raises(RuntimeError):
        c.respond("hello?")


def test_state_round_trips_between_turns():
    """The API stores state between HTTP calls; a rebuilt conversation must carry on."""
    c1 = convo()
    c1.start()
    c1.respond("yes")
    c2 = Conversation(c1.spec, SITE, c1.signal, KeywordClassifier(), state=c1.state)
    turn = c2.respond("bluebell")
    assert turn.step == "ask_cause"


@pytest.mark.parametrize("scenario", load_scenarios(ROOT / "evals" / "scenarios"), ids=lambda s: s["name"])
def test_eval_scenarios(scenario):
    result = run_scenario(scenario, SPECS, KeywordClassifier())
    assert result.passed, result.failures
