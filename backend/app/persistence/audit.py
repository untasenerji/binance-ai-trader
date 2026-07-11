"""Append-only delivery audit with idempotency and deterministic hash chaining."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.domain.types import TradePlanState
from app.persistence.models import AuditEvent, ProcessedEvent, TradePlanProjection


def _utc_iso(value: datetime, *, allow_naive_utc: bool = False) -> str:
    if value.tzinfo is None:
        if not allow_naive_utc:
            raise ValueError("audit timestamps must be timezone-aware")
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return _utc_iso(value)
    raise TypeError(f"cannot canonicalize {type(value).__name__}")


def _canonical_record(
    *,
    event_id: str,
    source: str,
    event_type: str,
    occurred_at: datetime,
    payload: Mapping[str, object],
    allow_naive_utc: bool = False,
) -> str:
    return json.dumps(
        {
            "event_id": event_id,
            "event_type": event_type,
            "occurred_at": _utc_iso(occurred_at, allow_naive_utc=allow_naive_utc),
            "payload": dict(payload),
            "source": source,
        },
        default=_json_default,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True, slots=True)
class AuditReceipt:
    audit_id: int
    event_id: str
    is_duplicate: bool
    record_hash: str


class AuditRepository:
    """Persists every delivery but applies a state change only once per event ID."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def record_delivery(
        self,
        *,
        event_id: str,
        source: str,
        event_type: str,
        occurred_at: datetime,
        payload: Mapping[str, object],
    ) -> AuditReceipt:
        if not event_id or not source or not event_type:
            raise ValueError("event_id, source, and event_type are required")

        payload_copy = dict(payload)
        with self._session_factory.begin() as session:
            previous_hash = session.scalar(
                select(AuditEvent.record_hash).order_by(AuditEvent.id.desc()).limit(1)
            )
            canonical = _canonical_record(
                event_id=event_id,
                source=source,
                event_type=event_type,
                occurred_at=occurred_at,
                payload=payload_copy,
            )
            record_hash = hashlib.sha256(f"{previous_hash or ''}{canonical}".encode()).hexdigest()
            audit_event = AuditEvent(
                event_id=event_id,
                source=source,
                event_type=event_type,
                occurred_at=occurred_at,
                payload=payload_copy,
                previous_hash=previous_hash,
                record_hash=record_hash,
            )
            session.add(audit_event)
            session.flush()

            is_duplicate = session.get(ProcessedEvent, event_id) is not None
            if not is_duplicate:
                session.add(ProcessedEvent(event_id=event_id, source=source))
                self._project_state(session, event_id=event_id, payload=payload_copy)

            return AuditReceipt(
                audit_id=audit_event.id,
                event_id=event_id,
                is_duplicate=is_duplicate,
                record_hash=record_hash,
            )

    def list_audit_events(self) -> tuple[AuditEvent, ...]:
        with self._session_factory() as session:
            return tuple(session.scalars(select(AuditEvent).order_by(AuditEvent.id.asc())))

    def processed_event_count(self) -> int:
        with self._session_factory() as session:
            return int(session.scalar(select(func.count()).select_from(ProcessedEvent)) or 0)

    @staticmethod
    def _project_state(session: Session, *, event_id: str, payload: Mapping[str, object]) -> None:
        plan_id = payload.get("plan_id")
        target_state = payload.get("to_state")
        if not isinstance(plan_id, str) or not isinstance(target_state, str):
            return

        try:
            parsed_state = TradePlanState(target_state)
        except ValueError:
            return

        projection = session.get(TradePlanProjection, plan_id)
        if projection is None:
            session.add(
                TradePlanProjection(
                    plan_id=plan_id,
                    state=parsed_state.value,
                    last_event_id=event_id,
                )
            )
            return

        projection.state = parsed_state.value
        projection.last_event_id = event_id
        projection.updated_at = datetime.now(UTC)


def verify_hash_chain(events: tuple[AuditEvent, ...]) -> bool:
    previous_hash: str | None = None
    for event in events:
        canonical = _canonical_record(
            event_id=event.event_id,
            source=event.source,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            payload=event.payload,
            allow_naive_utc=True,
        )
        expected_hash = hashlib.sha256(f"{previous_hash or ''}{canonical}".encode()).hexdigest()
        if event.previous_hash != previous_hash or event.record_hash != expected_hash:
            return False
        previous_hash = event.record_hash
    return True
