"""Persistence. SQLite for local dev and tests, Postgres in production (same code via SQLAlchemy Core)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

metadata = MetaData()

sites = Table(
    "sites",
    metadata,
    Column("account", String(8), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("address", Text, nullable=False),
    Column("passcode", String(64), nullable=False),
    Column("duress_code", String(64), nullable=False),
    Column("keyholders", JSON, nullable=False),
    Column("zones", JSON, nullable=False),
)

incidents = Table(
    "incidents",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("idempotency_key", String(200), nullable=False),
    Column("account", String(8), ForeignKey("sites.account"), nullable=False),
    Column("agent_id", String(100), nullable=False),
    Column("agent_version", String(20), nullable=False),
    Column("signal", JSON, nullable=False),
    Column("state", JSON, nullable=False),
    Column("status", String(20), nullable=False),
    Column("disposition", String(50)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("idempotency_key", name="uq_incident_idempotency"),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("incident_id", String(36), ForeignKey("incidents.id"), nullable=False),
    Column("kind", String(30), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("at", DateTime(timezone=True), nullable=False),
)


def _now() -> datetime:
    return datetime.now(UTC)


class DuplicateSignal(Exception):
    def __init__(self, incident_id: str) -> None:
        super().__init__(incident_id)
        self.incident_id = incident_id


class Store:
    def __init__(self, url: str = "sqlite+pysqlite:///:memory:") -> None:
        kwargs: dict[str, Any] = {}
        if url.startswith("sqlite") and ":memory:" in url:
            from sqlalchemy.pool import StaticPool

            kwargs = {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
        self.engine: Engine = create_engine(url, future=True, **kwargs)
        metadata.create_all(self.engine)

    def ping(self) -> bool:
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True

    # sites ---------------------------------------------------------------
    def upsert_site(self, site: dict[str, Any]) -> None:
        with self.engine.begin() as conn:
            exists = conn.execute(select(sites.c.account).where(sites.c.account == site["account"])).first()
            if exists:
                conn.execute(update(sites).where(sites.c.account == site["account"]).values(**site))
            else:
                conn.execute(insert(sites).values(**site))

    def get_site(self, account: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(sites).where(sites.c.account == account)).mappings().first()
        return dict(row) if row else None

    # incidents -----------------------------------------------------------
    def create_incident(
        self,
        *,
        idempotency_key: str,
        account: str,
        agent_id: str,
        agent_version: str,
        signal: dict[str, Any],
        state: dict[str, Any],
    ) -> str:
        incident_id = str(uuid.uuid4())
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    insert(incidents).values(
                        id=incident_id,
                        idempotency_key=idempotency_key,
                        account=account,
                        agent_id=agent_id,
                        agent_version=agent_version,
                        signal=signal,
                        state=state,
                        status="open",
                        created_at=_now(),
                        updated_at=_now(),
                    )
                )
        except IntegrityError:
            existing = self.find_by_idempotency_key(idempotency_key)
            if existing is None:
                raise
            raise DuplicateSignal(existing["id"]) from None
        return incident_id

    def find_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(incidents).where(incidents.c.idempotency_key == key)).mappings().first()
        return dict(row) if row else None

    def get_incident(self, incident_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(incidents).where(incidents.c.id == incident_id)).mappings().first()
        return dict(row) if row else None

    def save_state(self, incident_id: str, state: dict[str, Any], *, disposition: str | None) -> None:
        values: dict[str, Any] = {"state": state, "updated_at": _now()}
        if disposition:
            values |= {"status": "closed", "disposition": disposition}
        with self.engine.begin() as conn:
            conn.execute(update(incidents).where(incidents.c.id == incident_id).values(**values))

    # audit -----------------------------------------------------------------
    def audit(self, incident_id: str, kind: str, payload: dict[str, Any]) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(audit_log).values(
                    incident_id=incident_id,
                    kind=kind,
                    payload=json.loads(json.dumps(payload, default=str)),
                    at=_now(),
                )
            )

    def audit_trail(self, incident_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(audit_log).where(audit_log.c.incident_id == incident_id).order_by(audit_log.c.id)
            ).mappings()
            return [dict(r) for r in rows]


DEMO_SITES: list[dict[str, Any]] = [
    {
        "account": "1234",
        "name": "Harbour Street Pharmacy",
        "address": "14 Harbour Street, Dublin 2",
        "passcode": "bluebell",
        "duress_code": "sunflower",
        "keyholders": [
            {"name": "Aoife Byrne", "phone": "+353870000001"},
            {"name": "Ciaran Walsh", "phone": "+353870000002"},
        ],
        "zones": {"001": "front door", "003": "stock room motion sensor", "005": "office smoke detector"},
    },
    {
        "account": "5678",
        "name": "Northside Logistics Depot",
        "address": "Unit 7, Ballymun Industrial Estate",
        "passcode": "granite",
        "duress_code": "willow",
        "keyholders": [{"name": "Sam Doyle", "phone": "+353870000003"}],
        "zones": {"001": "roller shutter", "002": "yard beam", "004": "workshop heat detector"},
    },
]


def seed_demo(store: Store) -> None:
    for site in DEMO_SITES:
        store.upsert_site(site)
