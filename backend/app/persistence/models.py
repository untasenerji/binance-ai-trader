"""SQLAlchemy models for append-only audit and replayable local state."""

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
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
    account_id: Mapped[str] = mapped_column(String(128), default="v1-primary", index=True)
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
    submitted_at_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unknown_at_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    account_id: Mapped[str] = mapped_column(String(128), default="v1-primary", index=True)
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
    account_id: Mapped[str] = mapped_column(String(128), default="v1-primary", index=True)
    client_order_id: Mapped[str] = mapped_column(
        ForeignKey("durable_order_intents.client_order_id"), index=True
    )
    economic_key: Mapped[str] = mapped_column(String(256))
    source: Mapped[str] = mapped_column(String(48))
    # A legacy quarantine must preserve a missing original reference as NULL; a
    # synthetic replacement would falsely imply a query that never happened.
    query_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    query_client_order_id: Mapped[str] = mapped_column(String(128))
    query_economic_key: Mapped[str] = mapped_column(String(256))
    query_started_at_ms: Mapped[int] = mapped_column(Integer)
    attempt_number: Mapped[int] = mapped_column(Integer)
    client_order_namespace: Mapped[str] = mapped_column(String(32))
    provenance_fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    observed_at_ms: Mapped[int] = mapped_column(Integer)
    stream_watermark_ms: Mapped[int] = mapped_column(Integer)
    found: Mapped[bool] = mapped_column()
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DurableActualRiskPolicy(Base):
    """Immutable plan policy required before simulator entry facts can be accepted."""

    __tablename__ = "durable_actual_risk_policies"

    plan_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), default="v1-primary", index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(16))
    worst_stop_exit_price: Mapped[str] = mapped_column(String(64))
    exit_fee_rate: Mapped[str] = mapped_column(String(64))
    funding_buffer_rate: Mapped[str] = mapped_column(String(64))
    funding_interval_count: Mapped[int] = mapped_column(Integer)
    risk_budget: Mapped[str] = mapped_column(String(64))
    max_symbol_exposure_usdt: Mapped[str] = mapped_column(String(64))
    max_total_exposure_usdt: Mapped[str] = mapped_column(String(64))
    existing_symbol_exposure_usdt: Mapped[str] = mapped_column(String(64))
    existing_total_exposure_usdt: Mapped[str] = mapped_column(String(64))
    effective_leverage: Mapped[int] = mapped_column(Integer)
    required_reserve_usdt: Mapped[str] = mapped_column(String(64))
    effective_equity_usdt: Mapped[str] = mapped_column(String(64))
    protective_stop_reference: Mapped[str] = mapped_column(String(128))
    reduce_only_exit_reference: Mapped[str] = mapped_column(String(128))
    account_envelope_scope: Mapped[str] = mapped_column(String(128))
    account_envelope_version: Mapped[int] = mapped_column(Integer)
    account_envelope_fingerprint: Mapped[str] = mapped_column(String(64))
    policy_fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DurableActualRiskPolicyVersion(Base):
    """Append-only policy revisions; the base policy remains immutable version one."""

    __tablename__ = "durable_actual_risk_policy_versions"
    __table_args__ = (UniqueConstraint("plan_id", "version", name="uq_actual_risk_policy_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(128), default="v1-primary", index=True)
    plan_id: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[int] = mapped_column(Integer)
    symbol: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(16))
    worst_stop_exit_price: Mapped[str] = mapped_column(String(64))
    exit_fee_rate: Mapped[str] = mapped_column(String(64))
    funding_buffer_rate: Mapped[str] = mapped_column(String(64))
    funding_interval_count: Mapped[int] = mapped_column(Integer)
    risk_budget: Mapped[str] = mapped_column(String(64))
    max_symbol_exposure_usdt: Mapped[str] = mapped_column(String(64))
    max_total_exposure_usdt: Mapped[str] = mapped_column(String(64))
    existing_symbol_exposure_usdt: Mapped[str] = mapped_column(String(64))
    existing_total_exposure_usdt: Mapped[str] = mapped_column(String(64))
    effective_leverage: Mapped[int] = mapped_column(Integer)
    required_reserve_usdt: Mapped[str] = mapped_column(String(64))
    effective_equity_usdt: Mapped[str] = mapped_column(String(64))
    protective_stop_reference: Mapped[str] = mapped_column(String(128))
    reduce_only_exit_reference: Mapped[str] = mapped_column(String(128))
    account_envelope_scope: Mapped[str] = mapped_column(String(128))
    account_envelope_version: Mapped[int] = mapped_column(Integer)
    account_envelope_fingerprint: Mapped[str] = mapped_column(String(64))
    policy_fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DurableAccountPortfolioEnvelope(Base):
    """Append-only account-level cap, equity, and exposure authority."""

    __tablename__ = "durable_account_portfolio_envelopes"
    __table_args__ = (
        UniqueConstraint(
            "account_scope",
            "version",
            name="uq_account_portfolio_envelope_version",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(128), default="v1-primary", index=True)
    account_scope: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[int] = mapped_column(Integer)
    verified_account_equity_usdt: Mapped[str] = mapped_column(String(64))
    bot_equity_cap_usdt: Mapped[str] = mapped_column(String(64))
    required_reserve_usdt: Mapped[str] = mapped_column(String(64))
    max_total_exposure_usdt: Mapped[str] = mapped_column(String(64))
    max_symbol_exposure_usdt: Mapped[str] = mapped_column(String(64))
    max_required_margin_usdt: Mapped[str] = mapped_column(String(64))
    daily_remaining_risk_usdt: Mapped[str] = mapped_column(String(64))
    weekly_remaining_risk_usdt: Mapped[str] = mapped_column(String(64))
    open_position_count: Mapped[int] = mapped_column(Integer)
    pending_order_count: Mapped[int] = mapped_column(Integer)
    exposure_slices: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    reconciliation_required: Mapped[bool] = mapped_column(Boolean, default=False)
    envelope_fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MigrationQuarantineRecord(Base):
    """Fail-closed evidence retained when a forward migration cannot infer facts."""

    __tablename__ = "migration_quarantine_records"
    __table_args__ = (
        UniqueConstraint(
            "migration_revision",
            "source_table",
            "source_identity",
            "reason",
            name="uq_migration_quarantine_record",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    migration_revision: Mapped[str] = mapped_column(String(64))
    source_table: Mapped[str] = mapped_column(String(128))
    source_identity: Mapped[str] = mapped_column(String(256))
    reason: Mapped[str] = mapped_column(String(128))
    evidence: Mapped[dict[str, object]] = mapped_column(JSON)
    reconciliation_required: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DurableEntryAuthorizationGrant(Base):
    """Append-only, short-lived recovery authorization evidence."""

    __tablename__ = "durable_entry_authorization_grants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(128), default="v1-primary", index=True)
    failure_epoch: Mapped[int] = mapped_column(Integer, default=0)
    recovery_epoch: Mapped[int] = mapped_column(Integer, default=0)
    envelope_version: Mapped[int] = mapped_column(Integer, default=0)
    grant_generation: Mapped[int] = mapped_column(Integer, default=0)
    grant_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    recovery_probe_event_id: Mapped[str] = mapped_column(String(128))
    recovery_status: Mapped[str] = mapped_column(String(32))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    audit_event_count: Mapped[int] = mapped_column(Integer)
    audit_last_sequence: Mapped[int] = mapped_column(Integer)
    audit_last_record_hash: Mapped[str] = mapped_column(String(64))
    replay_valid: Mapped[bool] = mapped_column(Boolean)
    projection_matches_replay: Mapped[bool] = mapped_column(Boolean)
    unresolved_intent_count: Mapped[int] = mapped_column(Integer)
    reconciliation_clean: Mapped[bool] = mapped_column(Boolean)
    reconciliation_fingerprint: Mapped[str] = mapped_column(String(64))
    exchange_snapshot: Mapped[dict[str, object]] = mapped_column(JSON)
    exchange_snapshot_fingerprint: Mapped[str] = mapped_column(String(64))
    local_state_snapshot: Mapped[dict[str, object]] = mapped_column(JSON)
    local_state_fingerprint: Mapped[str] = mapped_column(String(64))
    capability_fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DurableEntryAuthorizationRevocation(Base):
    """Append-only invalidation of a previously issued entry capability."""

    __tablename__ = "durable_entry_authorization_revocations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(128), default="v1-primary", index=True)
    revocation_id: Mapped[str] = mapped_column(String(128), unique=True)
    grant_id: Mapped[str] = mapped_column(
        ForeignKey("durable_entry_authorization_grants.grant_id"), unique=True, index=True
    )
    reason: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DurableSimulatedProtection(Base):
    """Latest rehearsal-only intent coverage for a simulated position."""

    __tablename__ = "durable_simulated_protections"

    plan_id: Mapped[str] = mapped_column(
        ForeignKey("durable_actual_risk_policies.plan_id"), primary_key=True
    )
    account_id: Mapped[str] = mapped_column(String(128), default="v1-primary", index=True)
    stop_intent_reference: Mapped[str] = mapped_column(String(128))
    reduce_only_exit_intent_reference: Mapped[str] = mapped_column(String(128))
    protected_position_quantity: Mapped[str] = mapped_column(String(64), default="0")
    stop_intent_ready: Mapped[bool] = mapped_column(default=False)
    reduce_only_exit_intent_ready: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class DurableActualRiskState(Base):
    """Durable rehearsal position/risk state re-evaluated from immutable fills."""

    __tablename__ = "durable_actual_risk_states"

    plan_id: Mapped[str] = mapped_column(
        ForeignKey("durable_actual_risk_policies.plan_id"), primary_key=True
    )
    account_id: Mapped[str] = mapped_column(String(128), default="v1-primary", index=True)
    position_quantity: Mapped[str] = mapped_column(String(64), default="0")
    average_entry_price: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actual_notional_usdt: Mapped[str] = mapped_column(String(64), default="0")
    actual_required_margin_usdt: Mapped[str] = mapped_column(String(64), default="0")
    actual_stop_risk: Mapped[str] = mapped_column(String(64), default="0")
    pending_entries_blocked: Mapped[bool] = mapped_column(default=False)
    hard_halted: Mapped[bool] = mapped_column(default=False)
    reason: Mapped[str | None] = mapped_column(String(96), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class DurableRiskReductionRequirement(Base):
    """Typed fail-closed requirement created when a filled position must be reduced."""

    __tablename__ = "durable_risk_reduction_requirements"

    plan_id: Mapped[str] = mapped_column(
        ForeignKey("durable_actual_risk_policies.plan_id"), primary_key=True
    )
    account_id: Mapped[str] = mapped_column(String(128), default="v1-primary", index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(96))
    required_reduction_quantity: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class DurablePortfolioEnvelopeHead(Base):
    """Versioned account-wide risk authority; history is never overwritten."""

    __tablename__ = "durable_portfolio_envelope_heads"
    __table_args__ = (
        UniqueConstraint("account_id", "version", name="uq_portfolio_envelope_head_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    account_scope: Mapped[str] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(Integer)
    verified_account_equity_usdt: Mapped[str] = mapped_column(String(64))
    bot_equity_cap_usdt: Mapped[str] = mapped_column(String(64))
    required_reserve_usdt: Mapped[str] = mapped_column(String(64))
    max_total_exposure_usdt: Mapped[str] = mapped_column(String(64))
    symbol_exposure_caps_usdt: Mapped[dict[str, object]] = mapped_column(JSON)
    max_required_margin_usdt: Mapped[str] = mapped_column(String(64))
    daily_remaining_risk_usdt: Mapped[str] = mapped_column(String(64))
    weekly_remaining_risk_usdt: Mapped[str] = mapped_column(String(64))
    open_position_count: Mapped[int] = mapped_column(Integer)
    pending_order_count: Mapped[int] = mapped_column(Integer)
    reconciliation_required: Mapped[bool] = mapped_column(Boolean)
    exposure_slices: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    envelope_fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    # Separates the operator-supplied decision timestamp from the durable row
    # creation timestamp so an explicit envelope fingerprint can be replayed.
    fingerprint_effective_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    superseded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DurablePortfolioEnvelopeSupersession(Base):
    """Append-only link proving which later head supersedes an earlier version."""

    __tablename__ = "durable_portfolio_envelope_supersessions"
    __table_args__ = (
        UniqueConstraint("account_id", "superseded_version", name="uq_envelope_supersession"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    superseded_version: Mapped[int] = mapped_column(Integer)
    superseded_by: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DurableAccountSafetyState(Base):
    """Mutable fencing state for the sole V1 account; all entry grants bind to it."""

    __tablename__ = "durable_account_safety_states"

    account_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    failure_epoch: Mapped[int] = mapped_column(Integer, default=0)
    recovery_epoch: Mapped[int] = mapped_column(Integer, default=0)
    envelope_version: Mapped[int] = mapped_column(Integer, default=0)
    envelope_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    grant_generation: Mapped[int] = mapped_column(Integer, default=0)
    recovery_required: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class DurableAccountScopeLock(Base):
    """One durable row per account used for PostgreSQL row/advisory locks."""

    __tablename__ = "durable_account_scope_locks"

    account_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    lock_generation: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class DurableEvidenceQuarantine(Base):
    """Immutable deny evidence; it cannot be silently converted into ABSENT."""

    __tablename__ = "durable_evidence_quarantines"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "economic_key",
            "provenance_fingerprint",
            name="uq_evidence_quarantine_identity",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    quarantine_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    economic_key: Mapped[str] = mapped_column(String(256), index=True)
    client_order_id: Mapped[str] = mapped_column(String(128), index=True)
    query_reference: Mapped[str] = mapped_column(String(128))
    provenance_fingerprint: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(128))
    evidence: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DurableEvidenceQuarantineResolution(Base):
    """Append-only operator resolution that requires fresh verified evidence."""

    __tablename__ = "durable_evidence_quarantine_resolutions"
    __table_args__ = (UniqueConstraint("quarantine_id", name="uq_evidence_quarantine_resolution"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resolution_id: Mapped[str] = mapped_column(String(128), unique=True)
    quarantine_id: Mapped[str] = mapped_column(
        ForeignKey("durable_evidence_quarantines.quarantine_id"), index=True
    )
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    operator_id: Mapped[str] = mapped_column(String(128))
    verified_evidence_source: Mapped[str] = mapped_column(String(64))
    verified_query_reference: Mapped[str] = mapped_column(String(128))
    verified_evidence_fingerprint: Mapped[str] = mapped_column(String(64))
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MigrationExecutionMarker(Base):
    """Resumable forward-migration checkpoint for SQLite's partial DDL behavior."""

    __tablename__ = "migration_execution_markers"
    __table_args__ = (
        UniqueConstraint("migration_revision", "checkpoint", name="uq_migration_checkpoint"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    migration_revision: Mapped[str] = mapped_column(String(64))
    checkpoint: Mapped[str] = mapped_column(String(64))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


@event.listens_for(AuditEvent, "before_update")
def _reject_audit_update(*_: object) -> None:
    raise AppendOnlyViolation("audit_events are append-only")


@event.listens_for(AuditEvent, "before_delete")
def _reject_audit_delete(*_: object) -> None:
    raise AppendOnlyViolation("audit_events are append-only")


@event.listens_for(ProcessedEvent, "before_update")
@event.listens_for(ProcessedEvent, "before_delete")
def _reject_processed_event_mutation(*_: object) -> None:
    raise AppendOnlyViolation("processed_events are append-only")


@event.listens_for(DurableIntentFill, "before_update")
@event.listens_for(DurableIntentFill, "before_delete")
@event.listens_for(DurableIntentAbsenceObservation, "before_update")
@event.listens_for(DurableIntentAbsenceObservation, "before_delete")
def _reject_durable_evidence_mutation(*_: object) -> None:
    raise AppendOnlyViolation("durable reconciliation evidence is append-only")


@event.listens_for(DurableActualRiskPolicy, "before_update")
@event.listens_for(DurableActualRiskPolicy, "before_delete")
@event.listens_for(DurableActualRiskPolicyVersion, "before_update")
@event.listens_for(DurableActualRiskPolicyVersion, "before_delete")
@event.listens_for(DurableAccountPortfolioEnvelope, "before_update")
@event.listens_for(DurableAccountPortfolioEnvelope, "before_delete")
@event.listens_for(MigrationQuarantineRecord, "before_update")
@event.listens_for(MigrationQuarantineRecord, "before_delete")
@event.listens_for(DurableEntryAuthorizationGrant, "before_update")
@event.listens_for(DurableEntryAuthorizationGrant, "before_delete")
@event.listens_for(DurableEntryAuthorizationRevocation, "before_update")
@event.listens_for(DurableEntryAuthorizationRevocation, "before_delete")
@event.listens_for(DurablePortfolioEnvelopeHead, "before_update")
@event.listens_for(DurablePortfolioEnvelopeHead, "before_delete")
@event.listens_for(DurablePortfolioEnvelopeSupersession, "before_update")
@event.listens_for(DurablePortfolioEnvelopeSupersession, "before_delete")
@event.listens_for(DurableEvidenceQuarantine, "before_update")
@event.listens_for(DurableEvidenceQuarantine, "before_delete")
@event.listens_for(DurableEvidenceQuarantineResolution, "before_update")
@event.listens_for(DurableEvidenceQuarantineResolution, "before_delete")
def _reject_durable_policy_mutation(*_: object) -> None:
    raise AppendOnlyViolation("durable policy and authorization evidence is append-only")
