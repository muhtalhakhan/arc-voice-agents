"""Tools an agent may call. Each returns an outcome string the step graph branches on.

Tools hold the business rules and guardrails, so a prompt change can never
talk the agent into, say, dispatching police without grounds.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .contact_id import Category


@dataclass
class ToolResult:
    outcome: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolContext:
    site: dict[str, Any]
    signal: dict[str, Any]
    state: dict[str, Any]
    outbox: list[dict[str, Any]]


Tool = Callable[[ToolContext, dict[str, Any]], ToolResult]

_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _contains_code(utterance: str, code: str) -> bool:
    # constant-time compare per token, so timing does not leak how close a guess was
    return any(hmac.compare_digest(w, code.lower()) for w in _words(utterance))


def lookup_site(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    zone = ctx.signal.get("zone", "")
    return ToolResult(
        "ok",
        {
            "site_name": ctx.site["name"],
            "address": ctx.site["address"],
            "zone_name": ctx.site["zones"].get(zone, f"zone {zone}"),
            "event": str(ctx.signal.get("description", "alarm")).lower(),
        },
    )


def verify_passcode(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    utterance = str(args.get("utterance", ""))
    attempts = ctx.state.setdefault("passcode_attempts", 0) + 1
    ctx.state["passcode_attempts"] = attempts

    if _contains_code(utterance, ctx.site["duress_code"]):
        # The caller is under threat. Never reveal this on the call.
        ctx.state["duress"] = True
        return ToolResult("duress")
    if _contains_code(utterance, ctx.site["passcode"]):
        ctx.state["verified"] = True
        return ToolResult("valid")
    return ToolResult("invalid" if attempts >= 2 else "retry")


def mark_alarm_confirmed(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    ctx.state["alarm_confirmed"] = True
    return ToolResult("ok")


def request_police_dispatch(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Guardrail: police are only requested with grounds an ARC could defend."""
    grounds = [
        ctx.state.get("duress") and "duress code given",
        ctx.state.get("alarm_confirmed") and "keyholder confirmed intrusion",
        ctx.signal.get("category") in {Category.PANIC, Category.DURESS} and "panic/duress signal",
    ]
    grounds = [g for g in grounds if g]
    if not grounds:
        return ToolResult("blocked", {"reason": "no confirmed grounds for police dispatch"})
    ctx.outbox.append({"type": "police_dispatch", "address": ctx.site["address"], "grounds": grounds})
    return ToolResult("ok", {"grounds": grounds})


def request_fire_service(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    ctx.outbox.append({"type": "fire_service", "address": ctx.site["address"]})
    return ToolResult("ok")


def notify_keyholder(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    keyholders = ctx.site.get("keyholders") or []
    if not keyholders:
        return ToolResult("none_available")
    ctx.outbox.append({"type": "sms", "to": keyholders[0]["phone"], "template": args.get("template", "attend")})
    return ToolResult("ok", {"keyholder": keyholders[0]["name"]})


def book_engineer(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    ctx.outbox.append({"type": "engineer_visit", "account": ctx.site["account"], "zone": ctx.signal.get("zone")})
    return ToolResult("ok")


def escalate_to_operator(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    ctx.outbox.append({"type": "operator_queue", "priority": "high", "reason": args.get("reason", "agent escalation")})
    return ToolResult("ok")


REGISTRY: dict[str, Tool] = {
    "lookup_site": lookup_site,
    "verify_passcode": verify_passcode,
    "mark_alarm_confirmed": mark_alarm_confirmed,
    "request_police_dispatch": request_police_dispatch,
    "request_fire_service": request_fire_service,
    "notify_keyholder": notify_keyholder,
    "book_engineer": book_engineer,
    "escalate_to_operator": escalate_to_operator,
}
