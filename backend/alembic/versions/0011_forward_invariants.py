"""Forward-repair legacy evidence and install account-level safety invariants.

Revision ID: 0011_forward_invariants
Revises: 0010_runtime_policy_owner
Create Date: 2026-07-15
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa

from alembic import op

revision: str = "0011_forward_invariants"
down_revision: str | None = "0010_runtime_policy_owner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ABSENCE_TABLE = "durable_intent_absence_observations"
_POLICY_TABLES = (
    "durable_actual_risk_policies",
    "durable_actual_risk_policy_versions",
)
_IMMUTABLE_TABLES = (
    *_POLICY_TABLES,
    "durable_account_portfolio_envelopes",
    "migration_quarantine_records",
    "durable_entry_authorization_grants",
    "durable_entry_authorization_revocations",
)
_QUERY_COLUMNS = (
    ("query_reference", sa.String(length=128)),
    ("query_client_order_id", sa.String(length=128)),
    ("query_economic_key", sa.String(length=256)),
    ("query_started_at_ms", sa.Integer()),
)
_READ_ONLY_ROLE_ALLOWLIST = frozenset(
    {
        "pg_monitor",
        "pg_read_all_data",
        "pg_read_all_settings",
        "pg_read_all_stats",
        "pg_stat_scan_tables",
    }
)


def _canonical_hash(value: Mapping[str, object]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _query_provenance(values: Mapping[str, object]) -> str:
    return _canonical_hash(
        {
            "attempt_number": int(values["attempt_number"]),
            "client_order_namespace": str(values["client_order_namespace"]),
            "economic_key": str(values["query_economic_key"]),
            "found": bool(values["found"]),
            "observed_at_ms": int(values["observed_at_ms"]),
            "query_client_order_id": str(values["query_client_order_id"]),
            "query_economic_key": str(values["query_economic_key"]),
            "query_reference": str(values["query_reference"]),
            "query_started_at_ms": int(values["query_started_at_ms"]),
            "source": str(values["source"]),
            "stream_watermark_ms": int(values["stream_watermark_ms"]),
        }
    )


def _legacy_query_provenance(values: Mapping[str, object]) -> str:
    return _canonical_hash(
        {
            "attempt_number": int(values["attempt_number"]),
            "client_order_namespace": "NORMAL",
            "economic_key": str(values["economic_key"]),
            "found": bool(values["found"]),
            "observed_at_ms": int(values["observed_at_ms"]),
            "query_client_order_id": str(values["original_query_client_order_id"]),
            "query_economic_key": str(values["original_query_economic_key"]),
            "query_reference": str(values["original_query_reference"]),
            "query_started_at_ms": int(values["original_query_started_at_ms"]),
            "source": str(values["source"]),
            "stream_watermark_ms": int(values["stream_watermark_ms"]),
        }
    )


def _table_exists(bind: sa.Connection, table_name: str) -> bool:
    return sa.inspect(bind).has_table(table_name)


def _column_names(bind: sa.Connection, table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table_name)}


def _index_names(bind: sa.Connection, table_name: str) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(table_name)}


def _create_quarantine_table(bind: sa.Connection) -> None:
    if _table_exists(bind, "migration_quarantine_records"):
        return
    op.create_table(
        "migration_quarantine_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("migration_revision", sa.String(length=64), nullable=False),
        sa.Column("source_table", sa.String(length=128), nullable=False),
        sa.Column("source_identity", sa.String(length=256), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column(
            "reconciliation_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "migration_revision",
            "source_table",
            "source_identity",
            "reason",
            name="uq_migration_quarantine_record",
        ),
    )


def _json_safe_evidence(row: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in row.items():
        if value is None or isinstance(value, (bool, int, float, str)):
            result[str(key)] = value
        else:
            result[str(key)] = str(value)
    return result


def _quarantine(
    bind: sa.Connection,
    *,
    source_table: str,
    source_identity: str,
    reason: str,
    evidence: Mapping[str, object],
) -> None:
    table = sa.Table(
        "migration_quarantine_records",
        sa.MetaData(),
        autoload_with=bind,
    )
    exists = bind.scalar(
        sa.select(sa.func.count())
        .select_from(table)
        .where(
            table.c.migration_revision == revision,
            table.c.source_table == source_table,
            table.c.source_identity == source_identity,
            table.c.reason == reason,
        )
    )
    if exists:
        return
    bind.execute(
        table.insert().values(
            migration_revision=revision,
            source_table=source_table,
            source_identity=source_identity,
            reason=reason,
            evidence=_json_safe_evidence(evidence),
            reconciliation_required=True,
        )
    )


def _drop_guard(bind: sa.Connection, table_name: str) -> None:
    if bind.dialect.name == "sqlite":
        for operation in ("update", "delete"):
            bind.execute(sa.text(f"DROP TRIGGER IF EXISTS prevent_{table_name}_{operation}"))
        return
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(f"DROP TRIGGER IF EXISTS prevent_{table_name}_mutation ON {table_name}")
        )
        if table_name == _ABSENCE_TABLE:
            bind.execute(
                sa.text(
                    "DROP TRIGGER IF EXISTS "
                    "prevent_durable_intent_absence_observations_mutation "
                    "ON durable_intent_absence_observations"
                )
            )


def _install_guard(bind: sa.Connection, table_name: str) -> None:
    if bind.dialect.name == "sqlite":
        for operation in ("update", "delete"):
            bind.execute(
                sa.text(
                    f"CREATE TRIGGER IF NOT EXISTS prevent_{table_name}_{operation} "
                    f"BEFORE {operation.upper()} ON {table_name} "
                    f"BEGIN SELECT RAISE(ABORT, '{table_name} are append-only'); END"
                )
            )
        return
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "CREATE OR REPLACE FUNCTION reject_immutable_evidence_mutation() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'immutable evidence is append-only'; "
                "RETURN NULL; END; $$"
            )
        )
        trigger_name = f"prevent_{table_name}_mutation"
        bind.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}"))
        bind.execute(
            sa.text(
                f"CREATE TRIGGER {trigger_name} BEFORE UPDATE OR DELETE OR TRUNCATE "
                f"ON {table_name} FOR EACH STATEMENT "
                "EXECUTE FUNCTION reject_immutable_evidence_mutation()"
            )
        )


def _normalize_absence_evidence(bind: sa.Connection) -> None:
    if not _table_exists(bind, _ABSENCE_TABLE):
        return
    _drop_guard(bind, _ABSENCE_TABLE)
    for index_name in (
        "uq_absence_query_reference",
        "uq_absence_provenance_fingerprint",
    ):
        if index_name in _index_names(bind, _ABSENCE_TABLE):
            op.drop_index(index_name, table_name=_ABSENCE_TABLE)

    rows = tuple(
        bind.execute(
            sa.text(
                "SELECT o.id, o.client_order_id, o.economic_key, o.source, "
                "o.query_reference, o.query_client_order_id, o.query_economic_key, "
                "o.query_started_at_ms, o.attempt_number, o.client_order_namespace, "
                "o.provenance_fingerprint, o.observed_at_ms, o.stream_watermark_ms, "
                "o.found, i.submitted_at_ms, i.unknown_at_ms, "
                "i.attempt_number AS intent_attempt_number "
                "FROM durable_intent_absence_observations o "
                "LEFT JOIN durable_order_intents i "
                "ON i.client_order_id = o.client_order_id ORDER BY o.id"
            )
        ).mappings()
    )
    seen_references: set[str] = set()
    seen_fingerprints: set[str] = set()
    for row in rows:
        source_identity = str(row["id"])
        try:
            submitted_at = int(row["submitted_at_ms"])
            unknown_at = int(row["unknown_at_ms"])
            observed_at = int(row["observed_at_ms"])
            stream_watermark = int(row["stream_watermark_ms"])
            query_started = (
                unknown_at
                if row["query_started_at_ms"] is None
                else int(row["query_started_at_ms"])
            )
        except (TypeError, ValueError):
            _quarantine(
                bind,
                source_table=_ABSENCE_TABLE,
                source_identity=source_identity,
                reason="LEGACY_CAUSAL_TIME_MISSING",
                evidence=row,
            )
            bind.execute(sa.text(f"DELETE FROM {_ABSENCE_TABLE} WHERE id = :id"), {"id": row["id"]})
            continue
        if (
            min(submitted_at, unknown_at, observed_at, stream_watermark, query_started) < 0
            or submitted_at > unknown_at
            or query_started < unknown_at
            or query_started > observed_at
            or observed_at < unknown_at
            or stream_watermark < observed_at
        ):
            _quarantine(
                bind,
                source_table=_ABSENCE_TABLE,
                source_identity=source_identity,
                reason="LEGACY_CAUSAL_TIME_INVALID",
                evidence=row,
            )
            bind.execute(sa.text(f"DELETE FROM {_ABSENCE_TABLE} WHERE id = :id"), {"id": row["id"]})
            continue

        client_order_id = str(row["client_order_id"])
        economic_key = str(row["economic_key"])
        query_reference = str(row["query_reference"] or "")
        if not query_reference:
            seed = f"{row['id']}:{client_order_id}:{economic_key}:{row['source']}:{observed_at}"
            query_reference = (
                f"legacy-query:{row['id']}:{hashlib.sha256(seed.encode()).hexdigest()[:32]}"
            )
        query_client_order_id = str(row["query_client_order_id"] or client_order_id)
        query_economic_key = str(row["query_economic_key"] or economic_key)
        attempt_number = int(row["intent_attempt_number"] or row["attempt_number"] or 1)
        namespace = str(row["client_order_namespace"] or "NORMAL")
        normalized: dict[str, object] = {
            **dict(row),
            "attempt_number": attempt_number,
            "client_order_namespace": namespace,
            "query_reference": query_reference,
            "query_client_order_id": query_client_order_id,
            "query_economic_key": query_economic_key,
            "query_started_at_ms": query_started,
            "original_query_reference": row["query_reference"],
            "original_query_client_order_id": row["query_client_order_id"],
            "original_query_economic_key": row["query_economic_key"],
            "original_query_started_at_ms": row["query_started_at_ms"] or query_started,
        }
        expected = _query_provenance(normalized)
        stored = str(row["provenance_fingerprint"] or "")
        allowed_stored = {"", expected, _legacy_query_provenance(normalized)}
        if stored not in allowed_stored:
            _quarantine(
                bind,
                source_table=_ABSENCE_TABLE,
                source_identity=source_identity,
                reason="LEGACY_PROVENANCE_INVALID",
                evidence=row,
            )
            bind.execute(sa.text(f"DELETE FROM {_ABSENCE_TABLE} WHERE id = :id"), {"id": row["id"]})
            continue
        if query_reference in seen_references or expected in seen_fingerprints:
            _quarantine(
                bind,
                source_table=_ABSENCE_TABLE,
                source_identity=source_identity,
                reason="LEGACY_DUPLICATE_QUERY_EVIDENCE",
                evidence=row,
            )
            bind.execute(sa.text(f"DELETE FROM {_ABSENCE_TABLE} WHERE id = :id"), {"id": row["id"]})
            continue
        seen_references.add(query_reference)
        seen_fingerprints.add(expected)
        bind.execute(
            sa.text(
                f"UPDATE {_ABSENCE_TABLE} SET query_reference = :query_reference, "
                "query_client_order_id = :query_client_order_id, "
                "query_economic_key = :query_economic_key, "
                "query_started_at_ms = :query_started_at_ms, "
                "attempt_number = :attempt_number, "
                "client_order_namespace = :client_order_namespace, "
                "provenance_fingerprint = :provenance_fingerprint WHERE id = :id"
            ),
            {
                "id": row["id"],
                "query_reference": query_reference,
                "query_client_order_id": query_client_order_id,
                "query_economic_key": query_economic_key,
                "query_started_at_ms": query_started,
                "attempt_number": attempt_number,
                "client_order_namespace": namespace,
                "provenance_fingerprint": expected,
            },
        )

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(_ABSENCE_TABLE, recreate="always") as batch_op:
            for column_name, column_type in _QUERY_COLUMNS:
                batch_op.alter_column(
                    column_name,
                    existing_type=column_type,
                    nullable=False,
                )
    else:
        for column_name, column_type in _QUERY_COLUMNS:
            op.alter_column(
                _ABSENCE_TABLE,
                column_name,
                existing_type=column_type,
                nullable=False,
            )
    op.create_index(
        "uq_absence_query_reference",
        _ABSENCE_TABLE,
        ["query_reference"],
        unique=True,
    )
    op.create_index(
        "uq_absence_provenance_fingerprint",
        _ABSENCE_TABLE,
        ["provenance_fingerprint"],
        unique=True,
    )
    _install_guard(bind, _ABSENCE_TABLE)


def _create_envelope_table(bind: sa.Connection) -> None:
    if _table_exists(bind, "durable_account_portfolio_envelopes"):
        return
    op.create_table(
        "durable_account_portfolio_envelopes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_scope", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("verified_account_equity_usdt", sa.String(length=64), nullable=False),
        sa.Column("bot_equity_cap_usdt", sa.String(length=64), nullable=False),
        sa.Column("required_reserve_usdt", sa.String(length=64), nullable=False),
        sa.Column("max_total_exposure_usdt", sa.String(length=64), nullable=False),
        sa.Column("max_symbol_exposure_usdt", sa.String(length=64), nullable=False),
        sa.Column("max_required_margin_usdt", sa.String(length=64), nullable=False),
        sa.Column("daily_remaining_risk_usdt", sa.String(length=64), nullable=False),
        sa.Column("weekly_remaining_risk_usdt", sa.String(length=64), nullable=False),
        sa.Column("open_position_count", sa.Integer(), nullable=False),
        sa.Column("pending_order_count", sa.Integer(), nullable=False),
        sa.Column("exposure_slices", sa.JSON(), nullable=False),
        sa.Column(
            "reconciliation_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("envelope_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_scope",
            "version",
            name="uq_account_portfolio_envelope_version",
        ),
        sa.UniqueConstraint("envelope_fingerprint"),
    )
    op.create_index(
        "ix_durable_account_portfolio_envelopes_account_scope",
        "durable_account_portfolio_envelopes",
        ["account_scope"],
    )


def _create_entry_authorization_tables(bind: sa.Connection) -> None:
    if not _table_exists(bind, "durable_entry_authorization_grants"):
        op.create_table(
            "durable_entry_authorization_grants",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("grant_id", sa.String(length=128), nullable=False),
            sa.Column("recovery_probe_event_id", sa.String(length=128), nullable=False),
            sa.Column("recovery_status", sa.String(length=32), nullable=False),
            sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("audit_event_count", sa.Integer(), nullable=False),
            sa.Column("audit_last_sequence", sa.Integer(), nullable=False),
            sa.Column("audit_last_record_hash", sa.String(length=64), nullable=False),
            sa.Column("replay_valid", sa.Boolean(), nullable=False),
            sa.Column("projection_matches_replay", sa.Boolean(), nullable=False),
            sa.Column("unresolved_intent_count", sa.Integer(), nullable=False),
            sa.Column("reconciliation_clean", sa.Boolean(), nullable=False),
            sa.Column("reconciliation_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("exchange_snapshot", sa.JSON(), nullable=False),
            sa.Column("exchange_snapshot_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("local_state_snapshot", sa.JSON(), nullable=False),
            sa.Column("local_state_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("capability_fingerprint", sa.String(length=64), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("grant_id"),
            sa.UniqueConstraint("capability_fingerprint"),
        )
        op.create_index(
            "ix_durable_entry_authorization_grants_grant_id",
            "durable_entry_authorization_grants",
            ["grant_id"],
        )
        op.create_index(
            "ix_durable_entry_authorization_grants_expires_at",
            "durable_entry_authorization_grants",
            ["expires_at"],
        )
    if not _table_exists(bind, "durable_entry_authorization_revocations"):
        op.create_table(
            "durable_entry_authorization_revocations",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("revocation_id", sa.String(length=128), nullable=False),
            sa.Column("grant_id", sa.String(length=128), nullable=False),
            sa.Column("reason", sa.String(length=128), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.ForeignKeyConstraint(
                ["grant_id"],
                ["durable_entry_authorization_grants.grant_id"],
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("revocation_id"),
            sa.UniqueConstraint("grant_id"),
        )
        op.create_index(
            "ix_durable_entry_authorization_revocations_grant_id",
            "durable_entry_authorization_revocations",
            ["grant_id"],
        )
    _install_guard(bind, "durable_entry_authorization_grants")
    _install_guard(bind, "durable_entry_authorization_revocations")


def _decimal_string(value: object) -> str:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise RuntimeError("legacy risk policy contains a non-Decimal value") from error
    if not decimal.is_finite() or decimal < 0:
        raise RuntimeError("legacy risk policy contains an invalid financial value")
    return format(decimal, "f")


def _envelope_fingerprint(values: Mapping[str, object]) -> str:
    return _canonical_hash(
        {
            "account_scope": values["account_scope"],
            "bot_equity_cap_usdt": values["bot_equity_cap_usdt"],
            "daily_remaining_risk_usdt": values["daily_remaining_risk_usdt"],
            "exposure_slices": values["exposure_slices"],
            "max_required_margin_usdt": values["max_required_margin_usdt"],
            "max_symbol_exposure_usdt": values["max_symbol_exposure_usdt"],
            "max_total_exposure_usdt": values["max_total_exposure_usdt"],
            "open_position_count": values["open_position_count"],
            "pending_order_count": values["pending_order_count"],
            "reconciliation_required": values["reconciliation_required"],
            "required_reserve_usdt": values["required_reserve_usdt"],
            "verified_account_equity_usdt": values["verified_account_equity_usdt"],
            "version": values["version"],
            "weekly_remaining_risk_usdt": values["weekly_remaining_risk_usdt"],
        }
    )


def _policy_fingerprint(row: Mapping[str, object]) -> str:
    return _canonical_hash(
        {
            "account_envelope_fingerprint": row["account_envelope_fingerprint"],
            "account_envelope_scope": row["account_envelope_scope"],
            "account_envelope_version": int(row["account_envelope_version"]),
            "direction": str(row["direction"]),
            "effective_leverage": int(row["effective_leverage"]),
            "exit_fee_rate": _decimal_string(row["exit_fee_rate"]),
            "funding_buffer_rate": _decimal_string(row["funding_buffer_rate"]),
            "funding_interval_count": int(row["funding_interval_count"]),
            "plan_id": str(row["plan_id"]),
            "protective_stop_reference": str(row["protective_stop_reference"]),
            "reduce_only_exit_reference": str(row["reduce_only_exit_reference"]),
            "risk_budget": _decimal_string(row["risk_budget"]),
            "symbol": str(row["symbol"]),
            "worst_stop_exit_price": _decimal_string(row["worst_stop_exit_price"]),
        }
    )


def _legacy_envelope_values(
    row: Mapping[str, object],
    *,
    version: int,
    reconciliation_required: bool,
    open_position_count: int,
    pending_order_count: int,
) -> dict[str, object]:
    total = Decimal(_decimal_string(row["existing_total_exposure_usdt"]))
    symbol_total = Decimal(_decimal_string(row["existing_symbol_exposure_usdt"]))
    if symbol_total > total:
        raise RuntimeError("legacy symbol exposure exceeds total exposure")
    leverage = int(row["effective_leverage"])
    if leverage < 1:
        raise RuntimeError("legacy risk policy leverage is invalid")
    slices: list[dict[str, object]] = []
    if symbol_total > 0:
        slices.append(
            {
                "direction": str(row["direction"]),
                "leverage": leverage,
                "notional_usdt": format(symbol_total, "f"),
                "plan_id": f"legacy-external:{row['plan_id']}",
                "required_margin_usdt": format(symbol_total / Decimal(leverage), "f"),
                "slice_id": f"legacy-symbol:{row['plan_id']}",
                "source_state": "EXTERNAL_CONFIRMED",
                "symbol": str(row["symbol"]),
            }
        )
    remainder = total - symbol_total
    if remainder > 0:
        slices.append(
            {
                "direction": str(row["direction"]),
                "leverage": leverage,
                "notional_usdt": format(remainder, "f"),
                "plan_id": f"legacy-external:{row['plan_id']}",
                "required_margin_usdt": format(remainder / Decimal(leverage), "f"),
                "slice_id": f"legacy-other:{row['plan_id']}",
                "source_state": "EXTERNAL_CONFIRMED",
                "symbol": "__LEGACY_OTHER__",
            }
        )
    equity = _decimal_string(row["effective_equity_usdt"])
    values: dict[str, object] = {
        "account_scope": "legacy-account",
        "version": version,
        "verified_account_equity_usdt": equity,
        "bot_equity_cap_usdt": equity,
        "required_reserve_usdt": _decimal_string(row["required_reserve_usdt"]),
        "max_total_exposure_usdt": _decimal_string(row["max_total_exposure_usdt"]),
        "max_symbol_exposure_usdt": _decimal_string(row["max_symbol_exposure_usdt"]),
        "max_required_margin_usdt": equity,
        "daily_remaining_risk_usdt": _decimal_string(row["risk_budget"]),
        "weekly_remaining_risk_usdt": _decimal_string(row["risk_budget"]),
        "open_position_count": open_position_count,
        "pending_order_count": pending_order_count,
        "exposure_slices": sorted(slices, key=lambda item: str(item["slice_id"])),
        "reconciliation_required": reconciliation_required or total > 0,
    }
    values["envelope_fingerprint"] = _envelope_fingerprint(values)
    return values


def _add_policy_envelope_columns(bind: sa.Connection, table_name: str) -> None:
    columns = _column_names(bind, table_name)
    additions = (
        ("account_envelope_scope", sa.String(length=128)),
        ("account_envelope_version", sa.Integer()),
        ("account_envelope_fingerprint", sa.String(length=64)),
    )
    for column_name, column_type in additions:
        if column_name not in columns:
            op.add_column(
                table_name,
                sa.Column(column_name, column_type, nullable=True),
            )


def _backfill_account_envelopes(bind: sa.Connection) -> None:
    _create_envelope_table(bind)
    for table_name in _POLICY_TABLES:
        _add_policy_envelope_columns(bind, table_name)
        _drop_guard(bind, table_name)

    base_rows = tuple(
        bind.execute(
            sa.text("SELECT * FROM durable_actual_risk_policies ORDER BY plan_id")
        ).mappings()
    )
    if base_rows:
        open_position_count = int(
            bind.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM durable_actual_risk_states "
                    "WHERE CAST(position_quantity AS NUMERIC) > 0"
                )
            )
            or 0
        )
        pending_order_count = int(
            bind.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM durable_order_intents WHERE status IN "
                    "('PREPARED','SUBMITTING','UNKNOWN','NEW','PARTIALLY_FILLED',"
                    "'CANCEL_REQUIRED')"
                )
            )
            or 0
        )
        unlinked = [row for row in base_rows if row["account_envelope_fingerprint"] is None]
        provisional: list[tuple[Mapping[str, object], dict[str, object]]] = []
        for row in unlinked:
            provisional.append(
                (
                    row,
                    _legacy_envelope_values(
                        row,
                        version=1,
                        reconciliation_required=False,
                        open_position_count=open_position_count,
                        pending_order_count=pending_order_count,
                    ),
                )
            )
        distinct_facts = {
            json.dumps(
                {
                    key: value
                    for key, value in values.items()
                    if key not in {"version", "envelope_fingerprint"}
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            for _, values in provisional
        }
        has_conflict = len(distinct_facts) > 1
        ordered_facts = {fact: index + 1 for index, fact in enumerate(sorted(distinct_facts))}
        envelope_table = sa.Table(
            "durable_account_portfolio_envelopes",
            sa.MetaData(),
            autoload_with=bind,
        )
        for row, values in provisional:
            fact = json.dumps(
                {
                    key: value
                    for key, value in values.items()
                    if key not in {"version", "envelope_fingerprint"}
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            values["version"] = ordered_facts[fact]
            if has_conflict:
                values["reconciliation_required"] = True
                _quarantine(
                    bind,
                    source_table="durable_actual_risk_policies",
                    source_identity=str(row["plan_id"]),
                    reason="CONFLICTING_LEGACY_ACCOUNT_ENVELOPE",
                    evidence=row,
                )
            values["envelope_fingerprint"] = _envelope_fingerprint(values)
            existing = bind.scalar(
                sa.select(sa.func.count())
                .select_from(envelope_table)
                .where(
                    envelope_table.c.account_scope == values["account_scope"],
                    envelope_table.c.version == values["version"],
                )
            )
            if not existing:
                bind.execute(envelope_table.insert().values(**values))
            link = {
                "scope": values["account_scope"],
                "version": values["version"],
                "fingerprint": values["envelope_fingerprint"],
                "plan_id": row["plan_id"],
            }
            bind.execute(
                sa.text(
                    "UPDATE durable_actual_risk_policies SET "
                    "account_envelope_scope = :scope, account_envelope_version = :version, "
                    "account_envelope_fingerprint = :fingerprint WHERE plan_id = :plan_id"
                ),
                link,
            )
            bind.execute(
                sa.text(
                    "UPDATE durable_actual_risk_policy_versions SET "
                    "account_envelope_scope = :scope, account_envelope_version = :version, "
                    "account_envelope_fingerprint = :fingerprint WHERE plan_id = :plan_id"
                ),
                link,
            )

    for table_name in _POLICY_TABLES:
        rows = tuple(bind.execute(sa.text(f"SELECT * FROM {table_name}")).mappings())
        for row in rows:
            if any(
                row[name] is None
                for name in (
                    "account_envelope_scope",
                    "account_envelope_version",
                    "account_envelope_fingerprint",
                )
            ):
                raise RuntimeError("legacy policy could not be linked to an account envelope")
            bind.execute(
                sa.text(
                    f"UPDATE {table_name} SET policy_fingerprint = :fingerprint "
                    + (
                        "WHERE plan_id = :identity"
                        if table_name == _POLICY_TABLES[0]
                        else "WHERE id = :identity"
                    )
                ),
                {
                    "fingerprint": _policy_fingerprint(row),
                    "identity": row["plan_id"] if table_name == _POLICY_TABLES[0] else row["id"],
                },
            )
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table(table_name, recreate="always") as batch_op:
                batch_op.alter_column(
                    "account_envelope_scope",
                    existing_type=sa.String(length=128),
                    nullable=False,
                )
                batch_op.alter_column(
                    "account_envelope_version",
                    existing_type=sa.Integer(),
                    nullable=False,
                )
                batch_op.alter_column(
                    "account_envelope_fingerprint",
                    existing_type=sa.String(length=64),
                    nullable=False,
                )
        else:
            op.alter_column(table_name, "account_envelope_scope", nullable=False)
            op.alter_column(table_name, "account_envelope_version", nullable=False)
            op.alter_column(table_name, "account_envelope_fingerprint", nullable=False)
        _install_guard(bind, table_name)
    _install_guard(bind, "durable_account_portfolio_envelopes")


def _harden_postgresql_roles(bind: sa.Connection) -> None:
    if bind.dialect.name != "postgresql":
        return
    reachable_roles = {
        str(name)
        for name in bind.scalars(
            sa.text(
                "SELECT rolname FROM pg_roles WHERE rolname <> 'uta_runtime' "
                "AND pg_has_role('uta_runtime', oid, 'SET')"
            )
        )
    }
    forbidden = sorted(reachable_roles - _READ_ONLY_ROLE_ALLOWLIST)
    if forbidden:
        raise RuntimeError(
            "uta_runtime has a non-allowlisted SET ROLE path: " + ", ".join(forbidden)
        )

    bind.execute(
        sa.text(
            "ALTER ROLE uta_runtime WITH NOSUPERUSER NOCREATEROLE NOCREATEDB "
            "NOREPLICATION NOBYPASSRLS NOINHERIT"
        )
    )
    bind.execute(
        sa.text(
            "ALTER ROLE uta_policy_config WITH NOLOGIN NOSUPERUSER NOCREATEROLE "
            "NOCREATEDB NOREPLICATION NOBYPASSRLS NOINHERIT"
        )
    )
    bind.execute(sa.text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
    bind.execute(sa.text("REVOKE CREATE ON SCHEMA public FROM uta_runtime"))
    bind.execute(
        sa.text(
            "DO $$ BEGIN "
            "EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database()); "
            "EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM uta_runtime', "
            "current_database()); "
            "END $$"
        )
    )
    for table_name in _IMMUTABLE_TABLES:
        bind.execute(sa.text(f"ALTER TABLE {table_name} OWNER TO uta_policy_config"))
        bind.execute(sa.text(f"REVOKE ALL ON TABLE {table_name} FROM PUBLIC"))
        bind.execute(sa.text(f"REVOKE ALL ON TABLE {table_name} FROM uta_runtime"))
        bind.execute(sa.text(f"GRANT SELECT ON TABLE {table_name} TO uta_runtime"))
    for sequence_name in (
        "durable_actual_risk_policy_versions_id_seq",
        "durable_account_portfolio_envelopes_id_seq",
        "migration_quarantine_records_id_seq",
        "durable_entry_authorization_grants_id_seq",
        "durable_entry_authorization_revocations_id_seq",
    ):
        bind.execute(sa.text(f"ALTER SEQUENCE {sequence_name} OWNER TO uta_policy_config"))
        bind.execute(sa.text(f"REVOKE ALL ON SEQUENCE {sequence_name} FROM PUBLIC"))
        bind.execute(sa.text(f"REVOKE ALL ON SEQUENCE {sequence_name} FROM uta_runtime"))


def _verify_invariants(bind: sa.Connection) -> None:
    for table_name in (*_IMMUTABLE_TABLES, _ABSENCE_TABLE):
        if not _table_exists(bind, table_name):
            raise RuntimeError(f"required forward-invariant table is missing: {table_name}")
    absence_columns = {
        column["name"]: column for column in sa.inspect(bind).get_columns(_ABSENCE_TABLE)
    }
    if any(absence_columns[name]["nullable"] for name, _ in _QUERY_COLUMNS):
        raise RuntimeError("query identity columns remain nullable")
    indexes = {index["name"]: index for index in sa.inspect(bind).get_indexes(_ABSENCE_TABLE)}
    for name in ("uq_absence_query_reference", "uq_absence_provenance_fingerprint"):
        if name not in indexes or not indexes[name]["unique"]:
            raise RuntimeError("query evidence uniqueness was not restored")
    for table_name in _POLICY_TABLES:
        columns = {column["name"]: column for column in sa.inspect(bind).get_columns(table_name)}
        if any(
            columns[name]["nullable"]
            for name in (
                "account_envelope_scope",
                "account_envelope_version",
                "account_envelope_fingerprint",
            )
        ):
            raise RuntimeError("policy envelope links remain nullable")
    authorization_columns = _column_names(
        bind,
        "durable_entry_authorization_grants",
    )
    if {
        "exchange_snapshot",
        "local_state_snapshot",
        "capability_fingerprint",
        "expires_at",
    } - authorization_columns:
        raise RuntimeError("durable entry authorization evidence is incomplete")
    guarded_tables = (*_IMMUTABLE_TABLES, _ABSENCE_TABLE)
    if bind.dialect.name == "sqlite":
        trigger_names = {
            str(name)
            for name in bind.scalars(
                sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            )
        }
        for table_name in guarded_tables:
            for operation in ("update", "delete"):
                if f"prevent_{table_name}_{operation}" not in trigger_names:
                    raise RuntimeError(f"append-only trigger missing for {table_name} {operation}")
    elif bind.dialect.name == "postgresql":
        for table_name in guarded_tables:
            if not bind.scalar(
                sa.text(
                    "SELECT EXISTS (SELECT 1 FROM pg_trigger "
                    "WHERE tgrelid = to_regclass(:table_name) AND NOT tgisinternal "
                    "AND tgname LIKE 'prevent_%')"
                ),
                {"table_name": table_name},
            ):
                raise RuntimeError(f"append-only trigger missing for {table_name}")


def upgrade() -> None:
    bind = op.get_bind()
    _create_quarantine_table(bind)
    _normalize_absence_evidence(bind)
    _backfill_account_envelopes(bind)
    _create_entry_authorization_tables(bind)
    _install_guard(bind, "migration_quarantine_records")
    _harden_postgresql_roles(bind)
    _verify_invariants(bind)


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in _IMMUTABLE_TABLES:
        if _table_exists(bind, table_name):
            _drop_guard(bind, table_name)
    for table_name in _POLICY_TABLES:
        columns = _column_names(bind, table_name)
        for column_name in (
            "account_envelope_fingerprint",
            "account_envelope_version",
            "account_envelope_scope",
        ):
            if column_name in columns:
                op.drop_column(table_name, column_name)
    if _table_exists(bind, "durable_entry_authorization_revocations"):
        op.drop_table("durable_entry_authorization_revocations")
    if _table_exists(bind, "durable_entry_authorization_grants"):
        op.drop_table("durable_entry_authorization_grants")
    if _table_exists(bind, "durable_account_portfolio_envelopes"):
        op.drop_table("durable_account_portfolio_envelopes")
    if _table_exists(bind, "migration_quarantine_records"):
        op.drop_table("migration_quarantine_records")
    for table_name in _POLICY_TABLES:
        _install_guard(bind, table_name)
