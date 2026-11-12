"""Runs an AgentSpec as a turn-based conversation.

The engine is deliberately deterministic: the LLM classifies replies, but
which step comes next, which tools run and how the call ends are decided by
the spec. That makes every path testable and every change reviewable.

State is a plain dict so it can be stored between turns (a voice platform
calls us once per caller utterance) and inspected after the call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .llm import IntentClassifier
from .spec import UNCLEAR, AgentSpec, Step
from .tools import REGISTRY, ToolContext

REPROMPT = "Sorry, I didn't catch that. "


@dataclass
class Turn:
    say: str
    done: bool = False
    disposition: str | None = None
    step: str | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)


class _SafeVars(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def check_tools(spec: AgentSpec) -> None:
    unknown = [t for t in spec.tools if t not in REGISTRY]
    if unknown:
        raise ValueError(f"{spec.id}: tools not in registry: {unknown}")


class Conversation:
    def __init__(
        self,
        spec: AgentSpec,
        site: dict[str, Any],
        signal: dict[str, Any],
        classifier: IntentClassifier,
        state: dict[str, Any] | None = None,
    ) -> None:
        check_tools(spec)
        self.spec = spec
        self.site = site
        self.signal = signal
        self.classifier = classifier
        self.state: dict[str, Any] = state or {
            "step": None,
            "retries": 0,
            "vars": {},
            "flags": {},
            "transcript": [],
            "tool_calls": [],
            "outbox": [],
            "done": False,
            "disposition": None,
        }

    # public ----------------------------------------------------------------
    def start(self) -> Turn:
        return self._advance(self.spec.start)

    def respond(self, utterance: str) -> Turn:
        if self.state["done"]:
            raise RuntimeError("conversation already finished")
        name = self.state["step"]
        step = self.spec.steps[name]
        # captured replies (passcodes) are redacted from the stored transcript
        logged = "[redacted]" if step.kind == "capture" else utterance
        self.state["transcript"].append({"role": "caller", "text": logged})

        if step.kind == "ask":
            intent = self.classifier.classify(utterance, step.expect, question=self._render(step.say or ""))
            self.state["transcript"][-1]["intent"] = intent
            if intent != UNCLEAR and intent in step.on:
                return self._advance(step.on[intent])
            return self._retry(step)

        # capture
        assert step.tool is not None
        outcome = self._call(step.tool, {**step.args, "utterance": utterance})
        if outcome == "retry":
            return self._retry(step)
        return self._advance(step.on.get(outcome, self.spec.fallback))

    # internals -------------------------------------------------------------
    def _retry(self, step: Step) -> Turn:
        self.state["retries"] += 1
        if self.state["retries"] > step.max_retries:
            return self._advance(step.on_exhausted or self.spec.fallback)
        return self._say(REPROMPT + self._render(step.say or ""), step=self.state["step"])

    def _advance(self, name: str) -> Turn:
        actions_before = len(self.state["outbox"])
        for _ in range(50):  # guards against a cycle of action steps
            step = self.spec.steps[name]
            if step.kind == "action":
                assert step.tool is not None
                outcome = self._call(step.tool, dict(step.args))
                name = step.on.get(outcome) or step.next or self.spec.fallback
                continue

            self.state["step"] = name
            self.state["retries"] = 0
            if step.kind == "end":
                self.state["done"] = True
                self.state["disposition"] = step.disposition
                turn = self._say(self._render(step.say or ""), step=name)
                turn.done, turn.disposition = True, step.disposition
            else:
                turn = self._say(self._render(step.say or ""), step=name)
            turn.actions = self.state["outbox"][actions_before:]
            return turn
        raise RuntimeError(f"{self.spec.id}: action steps loop without reaching a prompt")

    def _call(self, tool_name: str, args: dict[str, Any]) -> str:
        ctx = ToolContext(site=self.site, signal=self.signal, state=self.state["flags"], outbox=self.state["outbox"])
        result = REGISTRY[tool_name](ctx, args)
        self.state["vars"].update(result.data)
        # never store what the caller said to the passcode tool
        logged_args = {k: v for k, v in args.items() if k != "utterance"}
        self.state["tool_calls"].append({"tool": tool_name, "args": logged_args, "outcome": result.outcome})
        return result.outcome

    def _render(self, template: str) -> str:
        return template.format_map(_SafeVars(self.state["vars"])).strip()

    def _say(self, text: str, *, step: str | None) -> Turn:
        self.state["transcript"].append({"role": "agent", "text": text})
        return Turn(say=text, step=step)
