"""HTTP API.

POST /v1/signals                  alarm receiver webhook (HMAC signed, idempotent)
POST /v1/incidents/{id}/turns     one caller utterance in, one agent reply out
GET  /v1/incidents/{id}           state, transcript and audit trail
POST /v1/tools/{name}             server tool endpoint for hosted voice platforms
GET  /healthz /readyz /metrics
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel

from . import contact_id
from .engine import Conversation
from .evals import signal_dict
from .llm import IntentClassifier, default_classifier
from .observability import Metrics, configure_logging, log, request_id
from .spec import AgentSpec, load_all, route
from .store import DuplicateSignal, Store, seed_demo
from .tools import REGISTRY, ToolContext

logger = logging.getLogger("arcvoice")


@dataclass
class Settings:
    database_url: str = "sqlite+pysqlite:///:memory:"
    agents_dir: str = "agents"
    signal_secret: str = "dev-signal-secret"
    tools_token: str = "dev-tools-token"
    seed_demo: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.environ.get("DATABASE_URL", cls.database_url),
            agents_dir=os.environ.get("AGENTS_DIR", cls.agents_dir),
            signal_secret=os.environ.get("SIGNAL_WEBHOOK_SECRET", cls.signal_secret),
            tools_token=os.environ.get("TOOLS_API_TOKEN", cls.tools_token),
            seed_demo=os.environ.get("SEED_DEMO", "true").lower() == "true",
        )


class SignalIn(BaseModel):
    receiver: str
    raw: str
    received_at: str


class TurnIn(BaseModel):
    utterance: str


class ToolCallIn(BaseModel):
    incident_id: str
    args: dict[str, Any] = {}


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def create_app(
    settings: Settings | None = None,
    store: Store | None = None,
    classifier: IntentClassifier | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    store = store or Store(settings.database_url)
    if settings.seed_demo:
        seed_demo(store)
    specs: dict[str, AgentSpec] = load_all(settings.agents_dir)
    clf = classifier or default_classifier()
    metrics = Metrics()

    app = FastAPI(title="arc-voice-agents", version="0.1.0")

    @app.middleware("http")
    async def _request_context(request: Request, call_next: Any) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id.set(rid)
        start = time.perf_counter()
        try:
            response: Response = await call_next(request)
        finally:
            request_id.reset(token)
        elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
        response.headers["x-request-id"] = rid
        metrics.inc(
            "http_requests_total",
            path=request.url.path.split("/")[2] if request.url.path.startswith("/v1/") else request.url.path,
            status=str(response.status_code),
        )
        log(
            logger,
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            ms=elapsed_ms,
            request_id=rid,
        )
        return response

    def require_tools_token(authorization: Annotated[str | None, Header()] = None) -> None:
        expected = f"Bearer {settings.tools_token}"
        if not authorization or not hmac.compare_digest(authorization, expected):
            raise HTTPException(401, "invalid token")

    def conversation_for(incident: dict[str, Any]) -> Conversation:
        spec = specs[incident["agent_id"]]
        if spec.version != incident["agent_version"]:
            # a call keeps the agent version it started with; log so drift is visible after a deploy
            log(
                logger,
                "agent version changed mid incident",
                logging.WARNING,
                incident_id=incident["id"],
                started=incident["agent_version"],
                current=spec.version,
            )
        site = store.get_site(incident["account"])
        assert site is not None
        return Conversation(spec, site, incident["signal"], clf, state=incident["state"])

    # ------------------------------------------------------------------ health
    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, Any]:
        store.ping()
        return {"status": "ready", "agents": {s.id: s.version for s in specs.values()}}

    @app.get("/metrics")
    def metrics_endpoint() -> Response:
        return Response(metrics.render(), media_type="text/plain; version=0.0.4")

    # ----------------------------------------------------------------- signals
    @app.post("/v1/signals", status_code=202)
    async def receive_signal(
        request: Request,
        response: Response,
        x_signature: Annotated[str | None, Header()] = None,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        body = await request.body()
        if not x_signature or not hmac.compare_digest(x_signature, sign(body, settings.signal_secret)):
            metrics.inc("signals_rejected_total", reason="signature")
            raise HTTPException(401, "bad signature")

        payload = SignalIn.model_validate(json.loads(body))
        try:
            event = contact_id.parse(payload.raw)
        except contact_id.ContactIdError as exc:
            metrics.inc("signals_rejected_total", reason="invalid_frame")
            raise HTTPException(422, f"invalid Contact ID frame: {exc}") from exc

        metrics.inc("signals_received_total", category=event.category.value)
        if not event.is_actionable:
            response.status_code = 200
            return {"status": "logged", "reason": f"{event.qualifier.value} {event.category.value} needs no call"}

        site = store.get_site(event.account)
        if site is None:
            metrics.inc("signals_rejected_total", reason="unknown_account")
            raise HTTPException(422, f"unknown account {event.account}")

        spec = route(specs, event.category)
        if spec is None:
            metrics.inc("signals_unrouted_total", category=event.category.value)
            raise HTTPException(422, f"no agent handles {event.category.value}")

        signal = signal_dict(event)
        convo = Conversation(spec, site, signal, clf)
        turn = convo.start()
        key = (
            idempotency_key
            or hashlib.sha256(f"{payload.receiver}|{event.raw}|{payload.received_at}".encode()).hexdigest()
        )
        try:
            incident_id = store.create_incident(
                idempotency_key=key,
                account=event.account,
                agent_id=spec.id,
                agent_version=spec.version,
                signal=signal,
                state=convo.state,
            )
        except DuplicateSignal as dup:
            metrics.inc("signals_duplicate_total")
            response.status_code = 200
            return {"status": "duplicate", "incident_id": dup.incident_id}

        store.audit(incident_id, "signal", signal)
        store.audit(incident_id, "agent", {"say": turn.say, "step": turn.step})
        metrics.inc("incidents_opened_total", agent=spec.id)
        log(
            logger,
            "incident opened",
            incident_id=incident_id,
            agent=spec.id,
            version=spec.version,
            category=event.category.value,
        )
        return {
            "status": "calling",
            "incident_id": incident_id,
            "agent": spec.id,
            "agent_version": spec.version,
            "say": turn.say,
        }

    # ------------------------------------------------------------------- turns
    @app.post("/v1/incidents/{incident_id}/turns", dependencies=[Depends(require_tools_token)])
    def take_turn(incident_id: str, body: TurnIn) -> dict[str, Any]:
        incident = store.get_incident(incident_id)
        if incident is None:
            raise HTTPException(404, "incident not found")
        if incident["status"] != "open":
            raise HTTPException(409, "incident closed")

        convo = conversation_for(incident)
        turn = convo.respond(body.utterance)
        store.save_state(incident_id, convo.state, disposition=turn.disposition)
        store.audit(incident_id, "agent", {"say": turn.say, "step": turn.step, "actions": turn.actions})
        for action in turn.actions:
            metrics.inc("actions_total", type=action["type"])
        if turn.done:
            metrics.inc("incidents_closed_total", disposition=turn.disposition or "unknown")
            log(logger, "incident closed", incident_id=incident_id, disposition=turn.disposition)
        return {"say": turn.say, "done": turn.done, "disposition": turn.disposition, "actions": turn.actions}

    @app.get("/v1/incidents/{incident_id}", dependencies=[Depends(require_tools_token)])
    def get_incident(incident_id: str) -> dict[str, Any]:
        incident = store.get_incident(incident_id)
        if incident is None:
            raise HTTPException(404, "incident not found")
        return {**incident, "audit": store.audit_trail(incident_id)}

    # ------------------------------------------------------------------- tools
    @app.post("/v1/tools/{name}", dependencies=[Depends(require_tools_token)])
    def call_tool(name: str, body: ToolCallIn) -> dict[str, Any]:
        incident = store.get_incident(body.incident_id)
        if incident is None:
            raise HTTPException(404, "incident not found")
        spec = specs[incident["agent_id"]]
        if name not in spec.tools or name not in REGISTRY:
            raise HTTPException(403, f"tool {name!r} not allowed for agent {spec.id}")
        site = store.get_site(incident["account"])
        assert site is not None
        state = incident["state"]
        ctx = ToolContext(site=site, signal=incident["signal"], state=state["flags"], outbox=state["outbox"])
        result = REGISTRY[name](ctx, body.args)
        state["tool_calls"].append({"tool": name, "outcome": result.outcome, "via": "api"})
        store.save_state(body.incident_id, state, disposition=None)
        store.audit(body.incident_id, "tool", {"tool": name, "outcome": result.outcome})
        return {"outcome": result.outcome, "data": result.data}

    return app


def main() -> FastAPI:  # uvicorn factory: uvicorn arcvoice.api:main --factory
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    return create_app()
