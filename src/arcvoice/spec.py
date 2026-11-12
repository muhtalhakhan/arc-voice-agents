"""Agents as code: a voice agent is a versioned YAML spec, validated before it ships.

Every agent shares one shape (persona, prompt, tools, a step graph and the
dispositions it may end in), so agents stay consistent and a change to one
is reviewable as a diff and testable in CI.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .contact_id import Category

DISPOSITIONS = {
    "false_alarm_confirmed",
    "police_dispatched",
    "fire_service_dispatched",
    "keyholder_attending",
    "escalated_to_human",
    "engineer_booked",
    "advised_no_action",
}

UNCLEAR = "unclear"


class Step(BaseModel):
    """One node in the conversation graph.

    kind="ask":     speak, classify the reply into one of `expect`, branch with `on`.
    kind="capture": speak, pass the raw reply to `tool` as `capture`, branch on the tool outcome.
    kind="action":  run `tool` without speaking, branch on its outcome (or go to `next`).
    kind="end":     speak a closing line and finish with `disposition`.
    """

    kind: Literal["ask", "capture", "action", "end"]
    say: str | None = None
    expect: list[str] = Field(default_factory=list)
    capture: str | None = None
    tool: str | None = None
    args: dict[str, str] = Field(default_factory=dict)
    on: dict[str, str] = Field(default_factory=dict)
    next: str | None = None
    disposition: str | None = None
    max_retries: int = 2
    on_exhausted: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> Step:
        if self.kind == "ask":
            if not self.say or not self.expect:
                raise ValueError("ask steps need `say` and `expect`")
            missing = set(self.expect) - set(self.on)
            if missing:
                raise ValueError(f"ask step has no transition for intents: {sorted(missing)}")
        if self.kind == "capture" and not (self.say and self.capture and self.tool):
            raise ValueError("capture steps need `say`, `capture` and `tool`")
        if self.kind == "action" and not self.tool:
            raise ValueError("action steps need `tool`")
        if self.kind == "action" and not (self.on or self.next):
            raise ValueError("action steps need `on` or `next`")
        if self.kind == "end":
            if self.disposition not in DISPOSITIONS:
                raise ValueError(f"end step has unknown disposition {self.disposition!r}")
        elif self.disposition:
            raise ValueError("only end steps may set a disposition")
        return self


class Voice(BaseModel):
    language: str = "en-GB"
    persona: str


class AgentSpec(BaseModel):
    id: str
    version: str
    description: str
    handles: list[Category]
    voice: Voice
    system_prompt: str
    tools: list[str]
    start: str
    fallback: str
    steps: dict[str, Step]

    @field_validator("version")
    @classmethod
    def _semver(cls, v: str) -> str:
        parts = v.split(".")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise ValueError("version must be semver, e.g. 1.4.0")
        return v

    @model_validator(mode="after")
    def _graph(self) -> AgentSpec:
        names = set(self.steps)
        for ref in (self.start, self.fallback):
            if ref not in names:
                raise ValueError(f"unknown step {ref!r}")

        for name, step in self.steps.items():
            targets = [*step.on.values(), step.next, step.on_exhausted]
            for t in filter(None, targets):
                if t not in names:
                    raise ValueError(f"step {name!r} points to unknown step {t!r}")
            if step.tool and step.tool not in self.tools:
                raise ValueError(f"step {name!r} uses tool {step.tool!r} not declared in `tools`")

        reachable = self._reachable_from(self.start) | self._reachable_from(self.fallback)
        orphans = names - reachable
        if orphans:
            raise ValueError(f"unreachable steps: {sorted(orphans)}")

        for name in names:
            if not any(self.steps[s].kind == "end" for s in self._reachable_from(name)):
                raise ValueError(f"step {name!r} can never reach an end step")
        return self

    def _reachable_from(self, start: str) -> set[str]:
        seen: set[str] = set()
        stack = [start]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            step = self.steps[cur]
            stack.extend(t for t in [*step.on.values(), step.next, step.on_exhausted] if t)
            if step.kind in {"ask", "capture"}:
                stack.append(self.fallback)
        return seen


class _Yaml12Loader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2 booleans.

    PyYAML follows YAML 1.1, where `on`, `off`, `yes` and `no` are booleans.
    In a spec full of `on:` transitions and callers saying "yes", that is a bug
    waiting to happen, so only `true` and `false` are booleans here.
    """


_Yaml12Loader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:bool"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Yaml12Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def load_yaml(text: str) -> Any:
    return yaml.load(text, Loader=_Yaml12Loader)  # noqa: S506 - SafeLoader subclass


def load_spec(path: str | Path) -> AgentSpec:
    data = load_yaml(Path(path).read_text())
    return AgentSpec.model_validate(data)


def load_all(directory: str | Path) -> dict[str, AgentSpec]:
    specs: dict[str, AgentSpec] = {}
    for path in sorted(Path(directory).glob("*.yaml")):
        spec = load_spec(path)
        if spec.id in specs:
            raise ValueError(f"duplicate agent id {spec.id!r}")
        specs[spec.id] = spec
    return specs


def route(specs: dict[str, AgentSpec], category: Category) -> AgentSpec | None:
    """Pick the agent responsible for a signal category."""
    for spec in specs.values():
        if category in spec.handles:
            return spec
    return None
