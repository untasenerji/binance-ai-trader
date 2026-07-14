"""repair legacy audit evidence and add durable reconciliation facts

Revision ID: 0006_legacy_audit_evidence
Revises: 0005_audit_database_guards
Create Date: 2026-07-13
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from alembic import op
from app.persistence.reducer import (
    ProjectionState,
    TransitionEventSchemaError,
    TransitionSequenceError,
    parse_state_transition_payload,
    reduce_state_transition,
)

revision: str = "0006_legacy_audit_evidence"
down_revision: str | None = "0005_audit_database_guards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _utc_iso(value: object) -> str:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise TypeError("legacy audit timestamp is invalid")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _payload(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise TypeError("legacy audit payload is not a mapping")
    normalized = dict(value)
    json.dumps(normalized, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return normalized


def _semantic_fingerprint(
    *,
    event_id: str,
    source: str,
    event_type: str,
    occurred_at: object,
    payload: Mapping[str, Any],
) -> str:
    canonical = json.dumps(
        {
            "event_id": event_id,
            "event_type": event_type,
            "occurred_at": _utc_iso(occurred_at),
            "payload": payload,
            "source": source,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _record_hash(
    *,
    previous_hash: str | None,
    event_id: str,
    source: str,
    event_type: str,
    occurred_at: object,
    payload: Mapping[str, Any],
    chain_sequence: int,
    delivery_status: str,
    semantic_fingerprint: str,
) -> str:
    canonical = json.dumps(
        {
            "chain_sequence": chain_sequence,
            "delivery_status": delivery_status,
            "event_id": event_id,
            "event_type": event_type,
            "occurred_at": _utc_iso(occurred_at),
            "payload": payload,
            "semantic_fingerprint": semantic_fingerprint,
            "source": source,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(f"{previous_hash or ''}{canonical}".encode()).hexdigest()


def _legacy_transition_payload(
    payload: Mapping[str, Any],
    projection: ProjectionState | None,
) -> dict[str, Any]:
    required_fields = {
        "plan_id",
        "from_state",
        "to_state",
        "plan_version",
        "source_sequence",
        "transition_evidence",
    }
    if required_fields <= set(payload):
        return dict(payload)

    plan_id = payload.get("plan_id")
    to_state = payload.get("to_state")
    if not isinstance(plan_id, str) or not plan_id or not isinstance(to_state, str) or not to_state:
        raise TransitionEventSchemaError("legacy state transition is missing its identity")
    prior_state = projection.state.value if projection is not None else "DRAFT"
    prior_version = projection.plan_version if projection is not None else 0
    prior_sequence = projection.source_sequence if projection is not None else 0
    upgraded = dict(payload)
    upgraded.update(
        {
            "from_state": prior_state,
            "plan_version": prior_version + 1,
            "source_sequence": prior_sequence + 1,
            "transition_evidence": {
                "risk_permits_entry": to_state == "ENTRY_PENDING",
                "stop_confirmed": to_state
                in {"POSITION_PROTECTED", "MANAGING_POSITION", "ADD_PENDING"},
                "reduction_only": prior_state == "HALTED",
            },
        }
    )
    return upgraded


def _drop_legacy_audit_guards(bind: sa.Connection) -> None:
    if bind.dialect.name == "sqlite":
        bind.execute(sa.text("DROP TRIGGER IF EXISTS prevent_audit_events_update"))
        bind.execute(sa.text("DROP TRIGGER IF EXISTS prevent_audit_events_delete"))
    elif bind.dialect.name == "postgresql":
        bind.execute(
            sa.text("DROP TRIGGER IF EXISTS prevent_audit_events_mutation ON audit_events")
        )
        bind.execute(sa.text("DROP FUNCTION IF EXISTS reject_audit_event_mutation()"))


def _install_immutable_evidence_guards(bind: sa.Connection) -> None:
    if bind.dialect.name == "sqlite":
        for table_name in (
            "audit_events",
            "durable_intent_fills",
            "durable_intent_absence_observations",
        ):
            for operation in ("update", "delete"):
                bind.execute(
                    sa.text(
                        f"CREATE TRIGGER prevent_{table_name}_{operation} "
                        f"BEFORE {operation.upper()} ON {table_name} "
                        f"BEGIN SELECT RAISE(ABORT, '{table_name} are append-only'); END"
                    )
                )
        return
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "CREATE FUNCTION reject_immutable_evidence_mutation() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'immutable evidence is append-only'; RETURN NULL; END; $$"
            )
        )
        for table_name, trigger_name in (
            ("audit_events", "prevent_audit_events_mutation"),
            ("durable_intent_fills", "prevent_durable_intent_fills_mutation"),
            (
                "durable_intent_absence_observations",
                "prevent_durable_intent_absence_observations_mutation",
            ),
        ):
            bind.execute(
                sa.text(
                    f"CREATE TRIGGER {trigger_name} "
                    f"BEFORE UPDATE OR DELETE OR TRUNCATE ON {table_name} "
                    "FOR EACH STATEMENT EXECUTE FUNCTION reject_immutable_evidence_mutation()"
                )
            )


def _grant_postgresql_runtime_role(bind: sa.Connection) -> None:
    if bind.dialect.name != "postgresql":
        return
    bind.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'uta_runtime') THEN "
            "CREATE ROLE uta_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT; "
            "END IF; END $$"
        )
    )
    bind.execute(sa.text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
    bind.execute(sa.text("REVOKE ALL ON SCHEMA public FROM uta_runtime"))
    bind.execute(sa.text("GRANT USAGE ON SCHEMA public TO uta_runtime"))
    bind.execute(
        sa.text(
            "DO $$ BEGIN "
            "EXECUTE format('REVOKE ALL ON DATABASE %I FROM uta_runtime', current_database()); "
            "EXECUTE format('GRANT CONNECT ON DATABASE %I TO uta_runtime', current_database()); "
            "END $$"
        )
    )
    for table_name in (
        "audit_events",
        "processed_events",
        "audit_chain_heads",
        "trade_plan_projections",
        "reconciliation_runs",
        "durable_order_intents",
        "durable_intent_fills",
        "durable_intent_absence_observations",
    ):
        bind.execute(sa.text(f"REVOKE ALL ON TABLE {table_name} FROM uta_runtime"))
    for table_name in (
        "audit_events",
        "processed_events",
        "reconciliation_runs",
        "durable_intent_fills",
        "durable_intent_absence_observations",
    ):
        bind.execute(sa.text(f"GRANT SELECT, INSERT ON TABLE {table_name} TO uta_runtime"))
    for table_name in (
        "audit_chain_heads",
        "trade_plan_projections",
        "durable_order_intents",
    ):
        bind.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE ON TABLE {table_name} TO uta_runtime"))
    bind.execute(sa.text("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO uta_runtime"))


def _repair_legacy_audit(bind: sa.Connection) -> None:
    metadata = sa.MetaData()
    audit_events = sa.Table("audit_events", metadata, autoload_with=bind)
    processed_events = sa.Table("processed_events", metadata, autoload_with=bind)
    audit_chain_heads = sa.Table("audit_chain_heads", metadata, autoload_with=bind)
    projections = sa.Table("trade_plan_projections", metadata, autoload_with=bind)
    reconciliation_runs = sa.Table("reconciliation_runs", metadata, autoload_with=bind)
    rows = tuple(bind.execute(sa.select(audit_events).order_by(audit_events.c.id.asc())).mappings())

    # Temporary unique values avoid collisions while an old chain is renumbered and rehashed.
    for row in rows:
        bind.execute(
            audit_events.update()
            .where(audit_events.c.id == row["id"])
            .values(
                chain_sequence=-int(row["id"]),
                record_hash=hashlib.sha256(f"legacy-repair-{row['id']}".encode()).hexdigest(),
            )
        )

    canonical_by_event_id: dict[str, dict[str, Any]] = {}
    projection_states: dict[str, ProjectionState] = {}
    repaired_rows: list[dict[str, Any]] = []
    reconciliation_rows: list[dict[str, str]] = []
    for sequence, row in enumerate(rows, start=1):
        event_id = str(row["event_id"])
        source = str(row["source"])
        event_type = str(row["event_type"])
        raw_payload = _payload(row["payload"])
        raw_fingerprint = _semantic_fingerprint(
            event_id=event_id,
            source=source,
            event_type=event_type,
            occurred_at=row["occurred_at"],
            payload=raw_payload,
        )
        prior = canonical_by_event_id.get(event_id)
        if prior is not None:
            delivery_status = (
                "EXACT_DUPLICATE"
                if prior["raw_fingerprint"] == raw_fingerprint
                else "SEMANTIC_CONFLICT"
            )
            final_event_type = (
                prior["event_type"] if delivery_status == "EXACT_DUPLICATE" else event_type
            )
            final_payload = (
                prior["payload"] if delivery_status == "EXACT_DUPLICATE" else raw_payload
            )
        else:
            delivery_status = "CANONICAL"
            final_event_type = event_type
            final_payload = raw_payload
            if final_event_type == "state_transition":
                try:
                    plan_id = final_payload.get("plan_id")
                    current = projection_states.get(plan_id) if isinstance(plan_id, str) else None
                    final_payload = _legacy_transition_payload(final_payload, current)
                    transition_event = parse_state_transition_payload(final_payload)
                    reduction = reduce_state_transition(
                        projection_states.get(transition_event.plan_id), transition_event
                    )
                    if reduction.mutated:
                        projection_states[transition_event.plan_id] = reduction.projection
                except (TransitionEventSchemaError, TransitionSequenceError, ValueError):
                    final_event_type = "legacy_transition_requires_reconciliation"
                    final_payload = {
                        "legacy_event_type": "state_transition",
                        "legacy_payload": raw_payload,
                        "reason": "LEGACY_TRANSITION_UNREPLAYABLE",
                    }
                    reconciliation_rows.append(
                        {
                            "run_id": hashlib.sha256(
                                f"legacy-transition-{event_id}".encode()
                            ).hexdigest(),
                            "status": "RECONCILIATION_REQUIRED",
                            "reason": "LEGACY_TRANSITION_UNREPLAYABLE",
                        }
                    )
            final_fingerprint = _semantic_fingerprint(
                event_id=event_id,
                source=source,
                event_type=final_event_type,
                occurred_at=row["occurred_at"],
                payload=final_payload,
            )
            canonical_by_event_id[event_id] = {
                "event_type": final_event_type,
                "payload": final_payload,
                "raw_fingerprint": raw_fingerprint,
                "semantic_fingerprint": final_fingerprint,
                "first_seen_at": row["created_at"],
                "source": source,
            }

        final_fingerprint = _semantic_fingerprint(
            event_id=event_id,
            source=source,
            event_type=final_event_type,
            occurred_at=row["occurred_at"],
            payload=final_payload,
        )
        repaired_rows.append(
            {
                "id": row["id"],
                "event_id": event_id,
                "source": source,
                "event_type": final_event_type,
                "occurred_at": row["occurred_at"],
                "payload": final_payload,
                "chain_sequence": sequence,
                "delivery_status": delivery_status,
                "semantic_fingerprint": final_fingerprint,
            }
        )

    previous_hash: str | None = None
    for row in repaired_rows:
        record_hash = _record_hash(
            previous_hash=previous_hash,
            event_id=row["event_id"],
            source=row["source"],
            event_type=row["event_type"],
            occurred_at=row["occurred_at"],
            payload=row["payload"],
            chain_sequence=row["chain_sequence"],
            delivery_status=row["delivery_status"],
            semantic_fingerprint=row["semantic_fingerprint"],
        )
        bind.execute(
            audit_events.update()
            .where(audit_events.c.id == row["id"])
            .values(
                event_type=row["event_type"],
                payload=row["payload"],
                chain_sequence=row["chain_sequence"],
                delivery_status=row["delivery_status"],
                semantic_fingerprint=row["semantic_fingerprint"],
                previous_hash=previous_hash,
                record_hash=record_hash,
            )
        )
        previous_hash = record_hash

    bind.execute(processed_events.delete())
    for event_id, canonical in canonical_by_event_id.items():
        legacy_fingerprint = canonical["raw_fingerprint"]
        if legacy_fingerprint == canonical["semantic_fingerprint"]:
            legacy_fingerprint = None
        bind.execute(
            processed_events.insert().values(
                event_id=event_id,
                source=canonical["source"],
                semantic_fingerprint=canonical["semantic_fingerprint"],
                legacy_semantic_fingerprint=legacy_fingerprint,
                first_seen_at=canonical["first_seen_at"],
            )
        )

    bind.execute(projections.delete())
    for plan_id, projection in projection_states.items():
        last_event = next(
            row["event_id"]
            for row in reversed(repaired_rows)
            if row["delivery_status"] == "CANONICAL"
            and row["event_type"] == "state_transition"
            and row["payload"].get("plan_id") == plan_id
        )
        bind.execute(
            projections.insert().values(
                plan_id=plan_id,
                state=projection.state.value,
                plan_version=projection.plan_version,
                source_sequence=projection.source_sequence,
                last_event_id=last_event,
                updated_at=datetime.now(UTC),
            )
        )

    for reconciliation in reconciliation_rows:
        bind.execute(reconciliation_runs.insert().values(**reconciliation))

    bind.execute(audit_chain_heads.delete())
    bind.execute(
        audit_chain_heads.insert().values(
            chain_id=1,
            event_count=len(repaired_rows),
            last_sequence=len(repaired_rows),
            last_record_hash=previous_hash,
            updated_at=datetime.now(UTC),
        )
    )


def upgrade() -> None:
    bind = op.get_bind()
    _drop_legacy_audit_guards(bind)
    op.add_column(
        "processed_events",
        sa.Column("legacy_semantic_fingerprint", sa.String(length=64), nullable=True),
    )
    op.create_table(
        "durable_intent_fills",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("client_order_id", sa.String(length=128), nullable=False),
        sa.Column("trade_id", sa.String(length=128), nullable=False),
        sa.Column("semantic_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("last_quantity", sa.String(length=64), nullable=False),
        sa.Column("cumulative_quantity", sa.String(length=64), nullable=False),
        sa.Column("fill_price", sa.String(length=64), nullable=False),
        sa.Column("fee", sa.String(length=64), nullable=False),
        sa.Column("fee_asset", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["client_order_id"], ["durable_order_intents.client_order_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_order_id", "trade_id", name="uq_durable_intent_fill_trade"),
    )
    op.create_index(
        "ix_durable_intent_fills_client_order_id",
        "durable_intent_fills",
        ["client_order_id"],
        unique=False,
    )
    op.create_table(
        "durable_intent_absence_observations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("client_order_id", sa.String(length=128), nullable=False),
        sa.Column("economic_key", sa.String(length=256), nullable=False),
        sa.Column("source", sa.String(length=48), nullable=False),
        sa.Column("observed_at_ms", sa.Integer(), nullable=False),
        sa.Column("stream_watermark_ms", sa.Integer(), nullable=False),
        sa.Column("found", sa.Boolean(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["client_order_id"], ["durable_order_intents.client_order_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "client_order_id",
            "source",
            "observed_at_ms",
            name="uq_durable_intent_absence_observation",
        ),
    )
    op.create_index(
        "ix_durable_intent_absence_observations_client_order_id",
        "durable_intent_absence_observations",
        ["client_order_id"],
        unique=False,
    )
    _repair_legacy_audit(bind)
    _install_immutable_evidence_guards(bind)
    _grant_postgresql_runtime_role(bind)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        for table_name in (
            "audit_events",
            "durable_intent_fills",
            "durable_intent_absence_observations",
        ):
            for operation in ("update", "delete"):
                bind.execute(sa.text(f"DROP TRIGGER IF EXISTS prevent_{table_name}_{operation}"))
    elif bind.dialect.name == "postgresql":
        for table_name, trigger_name in (
            ("audit_events", "prevent_audit_events_mutation"),
            ("durable_intent_fills", "prevent_durable_intent_fills_mutation"),
            (
                "durable_intent_absence_observations",
                "prevent_durable_intent_absence_observations_mutation",
            ),
        ):
            bind.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}"))
        bind.execute(sa.text("DROP FUNCTION IF EXISTS reject_immutable_evidence_mutation()"))
    op.drop_index(
        "ix_durable_intent_absence_observations_client_order_id",
        table_name="durable_intent_absence_observations",
    )
    op.drop_table("durable_intent_absence_observations")
    op.drop_index("ix_durable_intent_fills_client_order_id", table_name="durable_intent_fills")
    op.drop_table("durable_intent_fills")
    op.drop_column("processed_events", "legacy_semantic_fingerprint")
    if bind.dialect.name == "sqlite":
        for operation in ("update", "delete"):
            bind.execute(
                sa.text(
                    f"CREATE TRIGGER prevent_audit_events_{operation} "
                    f"BEFORE {operation.upper()} ON audit_events "
                    "BEGIN SELECT RAISE(ABORT, 'audit_events are append-only'); END"
                )
            )
    elif bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "CREATE FUNCTION reject_audit_event_mutation() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'audit_events are append-only'; RETURN NULL; END; $$"
            )
        )
        bind.execute(
            sa.text(
                "CREATE TRIGGER prevent_audit_events_mutation "
                "BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_events "
                "FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_event_mutation()"
            )
        )
