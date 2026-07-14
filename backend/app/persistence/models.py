"""SQLAlchemy models for append-only audit and replayable local state."""

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class AppendOnlyViolation(RuntimeError):
    """Raised if code attempts to mutate or remove an immutable audit record."""


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), index=True)
    source: Mapped[str] = mapped_column(String(32))
    event_type: Mapped[str] = mapped_column(String(96))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    chain_sequence: Mapped[int] = mapped_column(Integer, unique=True)
    semantic_fingerprint: Mapped[str] = mapped_column(String(64))
    delivery_status: Mapped[str] = mapped_column(String(32))
    previous_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    record_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ProcessedEvent(Base):
    __tablename__ = "processed_events"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source: Mapped[str] = mapped_column(String(32))
    semantic_fingerprint: Mapped[str] = mapped_column(String(64))
    legacy_semantic_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuditChainHead(Base):
    __tablename__ = "audit_chain_heads"

    chain_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_count: Mapped[int] = mapped_column(Integer)
    last_sequence: Mapped[int] = mapped_column(Integer)
    last_record_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TradePlanProjection(Base):
    __tablename__ = "trade_plan_projections"

    plan_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    state: Mapped[str] = mapped_column(String(48))
    plan_version: Mapped[int] = mapped_column(Integer)
    source_sequence: Mapped[int] = mapped_column(Integer)
    last_event_id: Mapped[str] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DurableOrderIntent(Base):
    """Append-only attempt history for local simulated order submissions."""

    __tablename__ = "durable_order_intents"
    __table_args__ = (
        UniqueConstraint("economic_key", "attempt_number", name="uq_durable_intent_attempt"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    economic_key: Mapped[str] = mapped_column(String(256), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    client_order_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    plan_id: Mapped[str] = mapped_column(String(128))
    symbol: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(16))
    role: Mapped[str] = mapped_column(String(32))
    stage_index: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[str] = mapped_column(String(64))
    price: Mapped[str] = mapped_column(String(64))
    filled_quantity: Mapped[str] = mapped_column(String(64), default="0")
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class DurableIntentFill(Base):
    """Immutable fill facts required to rebuild VWAP and fees after a restart."""

    __tablename__ = "durable_intent_fills"
    __table_args__ = (
        UniqueConstraint("client_order_id", "trade_id", name="uq_durable_intent_fill_trade"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(
        ForeignKey("durable_order_intents.client_order_id"), index=True
    )
    trade_id: Mapped[str] = mapped_column(String(128))
    semantic_fingerprint: Mapped[str] = mapped_column(String(64))
    last_quantity: Mapped[str] = mapped_column(String(64))
    cumulative_quantity: Mapped[str] = mapped_column(String(64))
    fill_price: Mapped[str] = mapped_column(String(64))
    fee: Mapped[str] = mapped_column(String(64))
    fee_asset: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DurableIntentAbsenceObservation(Base):
    """Immutable reconciliation observations for a 503 UNKNOWN absence decision."""

    __tablename__ = "durable_intent_absence_observations"
    __table_args__ = (
        UniqueConstraint(
            "client_order_id",
            "source",
            "observed_at_ms",
            name="uq_durable_intent_absence_observation",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(
        ForeignKey("durable_order_intents.client_order_id"), index=True
    )
    economic_key: Mapped[str] = mapped_column(String(256))
    source: Mapped[str] = mapped_column(String(48))
    observed_at_ms: Mapped[int] = mapped_column(Integer)
    stream_watermark_ms: Mapped[int] = mapped_column(Integer)
    found: Mapped[bool] = mapped_column()
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


@event.listens_for(AuditEvent, "before_update")
def _reject_audit_update(*_: object) -> None:
    raise AppendOnlyViolation("audit_events are append-only")


@event.listens_for(AuditEvent, "before_delete")
def _reject_audit_delete(*_: object) -> None:
    raise AppendOnlyViolation("audit_events are append-only")


@event.listens_for(DurableIntentFill, "before_update")
@event.listens_for(DurableIntentFill, "before_delete")
@event.listens_for(DurableIntentAbsenceObservation, "before_update")
@event.listens_for(DurableIntentAbsenceObservation, "before_delete")
def _reject_durable_evidence_mutation(*_: object) -> None:
    raise AppendOnlyViolation("durable reconciliation evidence is append-only")
