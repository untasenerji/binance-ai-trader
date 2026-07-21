"""Add execution-fact identity and complete runtime policy ACL proof.

Revision ID: 0013_execution_safety_core
Revises: 0012_account_scope_safety
Create Date: 2026-07-16
"""

import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0013_execution_safety_core"
down_revision: str | None = "0012_account_scope_safety"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MARKERS = "migration_execution_markers"
_FILL_TABLE = "durable_intent_fills"
_INTENT_TABLE = "durable_order_intents"
_LEGACY_QUARANTINE_TABLE = "migration_quarantine_records"
_QUARANTINE_TABLE = "durable_evidence_quarantines"
_QUARANTINE_SOURCE_TABLE = "durable_evidence_quarantine_sources"
_POLICY_TABLES = (
    "durable_actual_risk_policies",
    "durable_actual_risk_policy_versions",
    _QUARANTINE_SOURCE_TABLE,
)
_FILL_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("symbol", sa.String(length=32)),
    ("side", sa.String(length=8)),
    ("observation_source", sa.String(length=64)),
    ("observation_reference", sa.String(length=128)),
)


def _table_exists(bind: sa.Connection, table_name: str) -> bool:
    return sa.inspect(bind).has_table(table_name)


def _column_names(bind: sa.Connection, table_name: str) -> set[str]:
    return {str(column["name"]) for column in sa.inspect(bind).get_columns(table_name)}


def _marker_completed(bind: sa.Connection, checkpoint: str) -> bool:
    marker = sa.Table(_MARKERS, sa.MetaData(), autoload_with=bind)
    return bool(
        bind.scalar(
            sa.select(sa.func.count())
            .select_from(marker)
            .where(
                marker.c.migration_revision == revision,
                marker.c.checkpoint == checkpoint,
            )
        )
    )


def _mark_completed(bind: sa.Connection, checkpoint: str) -> None:
    marker = sa.Table(_MARKERS, sa.MetaData(), autoload_with=bind)
    if _marker_completed(bind, checkpoint):
        return
    bind.execute(marker.insert().values(migration_revision=revision, checkpoint=checkpoint))


def _run_resumable_step(
    bind: sa.Connection,
    checkpoint: str,
    operation: Callable[[], None],
    postcondition: Callable[[], bool],
) -> None:
    if _marker_completed(bind, checkpoint) and postcondition():
        return
    operation()
    if not postcondition():
        raise RuntimeError(f"0013 postcondition failed: {checkpoint}")
    if os.environ.get("UTA_0013_FAIL_AFTER") == checkpoint:
        raise RuntimeError(f"0013 checkpoint failure injected after {checkpoint}")
    _mark_completed(bind, checkpoint)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _fill_fingerprint(row: sa.RowMapping, *, symbol: str, side: str, reference: str) -> str:
    canonical = json.dumps(
        {
            "account_id": str(row["account_id"]),
            "client_order_id": str(row["client_order_id"]),
            "cumulative_quantity": str(row["cumulative_quantity"]),
            "fee": str(row["fee"]),
            "fee_asset": str(row["fee_asset"]),
            "fill_price": str(row["fill_price"]),
            "last_quantity": str(row["last_quantity"]),
            "observation_reference": reference,
            "observation_source": "legacy_migration",
            "occurred_at": _as_utc(row["occurred_at"]).isoformat(),
            "side": side,
            "symbol": symbol,
            "trade_id": str(row["trade_id"]),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _recover_sqlite_batch_table(bind: sa.Connection, table_name: str) -> None:
    if bind.dialect.name != "sqlite":
        return
    temporary_name = f"_alembic_tmp_{table_name}"
    table_names = set(sa.inspect(bind).get_table_names())
    if temporary_name not in table_names:
        return
    if table_name in table_names:
        bind.execute(sa.text(f'DROP TABLE "{temporary_name}"'))
    else:
        bind.execute(sa.text(f'ALTER TABLE "{temporary_name}" RENAME TO "{table_name}"'))


def _add_fill_fact_columns(bind: sa.Connection) -> None:
    _recover_sqlite_batch_table(bind, _FILL_TABLE)
    if not _table_exists(bind, _FILL_TABLE):
        raise RuntimeError("0013 requires the durable fill table")
    existing = _column_names(bind, _FILL_TABLE)
    for column_name, column_type in _FILL_COLUMNS:
        if column_name in existing:
            continue
        op.add_column(
            _FILL_TABLE,
            sa.Column(column_name, column_type, nullable=True),
        )
        existing.add(column_name)
        if os.environ.get("UTA_0013_FAIL_MID_OPERATION") == f"fill:{column_name}":
            raise RuntimeError(f"0013 mid-operation failure injected at fill:{column_name}")

    metadata = sa.MetaData()
    fills = sa.Table(_FILL_TABLE, metadata, autoload_with=bind)
    intents = sa.Table(_INTENT_TABLE, metadata, autoload_with=bind)
    rows = tuple(
        bind.execute(
            sa.select(
                fills.c.id,
                fills.c.account_id,
                fills.c.client_order_id,
                fills.c.trade_id,
                fills.c.last_quantity,
                fills.c.cumulative_quantity,
                fills.c.fill_price,
                fills.c.fee,
                fills.c.fee_asset,
                fills.c.occurred_at,
                intents.c.symbol,
                intents.c.direction,
                intents.c.role,
            ).join(
                intents,
                intents.c.client_order_id == fills.c.client_order_id,
            )
        ).mappings()
    )
    for row in rows:
        symbol = str(row["symbol"])
        direction = str(row["direction"])
        role = str(row["role"])
        side = (
            "BUY"
            if (role == "ENTRY" and direction == "LONG")
            or (role != "ENTRY" and direction == "SHORT")
            else "SELL"
        )
        reference = f"legacy:{row['client_order_id']}:{row['trade_id']}"
        bind.execute(
            fills.update()
            .where(fills.c.id == row["id"])
            .values(
                symbol=symbol,
                side=side,
                observation_source="legacy_migration",
                observation_reference=reference,
                semantic_fingerprint=_fill_fingerprint(
                    row,
                    symbol=symbol,
                    side=side,
                    reference=reference,
                ),
            )
        )

    null_count = bind.scalar(
        sa.select(sa.func.count())
        .select_from(fills)
        .where(
            sa.or_(
                fills.c.symbol.is_(None),
                fills.c.side.is_(None),
                fills.c.observation_source.is_(None),
                fills.c.observation_reference.is_(None),
            )
        )
    )
    if null_count:
        raise RuntimeError("0013 could not derive complete legacy fill identity")
    current_columns = {
        str(column["name"]): column for column in sa.inspect(bind).get_columns(_FILL_TABLE)
    }
    if any(bool(current_columns[name]["nullable"]) for name, _ in _FILL_COLUMNS):
        with op.batch_alter_table(_FILL_TABLE) as batch:
            for column_name, column_type in _FILL_COLUMNS:
                batch.alter_column(
                    column_name,
                    existing_type=column_type,
                    nullable=False,
                )
    if bind.dialect.name == "sqlite":
        for operation in ("update", "delete"):
            bind.execute(
                sa.text(
                    f"CREATE TRIGGER IF NOT EXISTS prevent_{_FILL_TABLE}_{operation} "
                    f"BEFORE {operation.upper()} ON {_FILL_TABLE} BEGIN "
                    "SELECT RAISE(ABORT, 'append-only table mutation denied'); END"
                )
            )


def _fill_columns_complete(bind: sa.Connection) -> bool:
    if not _table_exists(bind, _FILL_TABLE):
        return False
    columns = {str(column["name"]): column for column in sa.inspect(bind).get_columns(_FILL_TABLE)}
    columns_complete = all(
        name in columns and not bool(columns[name]["nullable"]) for name, _ in _FILL_COLUMNS
    )
    if not columns_complete or bind.dialect.name != "sqlite":
        return columns_complete
    trigger_names = set(
        bind.scalars(sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))
    )
    return all(
        f"prevent_{_FILL_TABLE}_{operation}" in trigger_names for operation in ("update", "delete")
    )


def _legacy_quarantine_evidence(raw: object) -> dict[str, object]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError("0013 legacy quarantine evidence is not JSON") from error
    if not isinstance(raw, Mapping):
        raise RuntimeError("0013 legacy quarantine evidence is not structured")
    return {str(key): value for key, value in raw.items()}


def _legacy_quarantine_identity(
    row: sa.RowMapping,
) -> tuple[str, str, str, str, str | None, dict[str, object]]:
    evidence = _legacy_quarantine_evidence(row["evidence"])
    account_id = str(evidence.get("account_id") or "v1-primary")
    economic_key = evidence.get("economic_key")
    client_order_id = evidence.get("client_order_id")
    query_reference = evidence.get("query_reference")
    if (
        account_id != "v1-primary"
        or not isinstance(economic_key, str)
        or not economic_key
        or not isinstance(client_order_id, str)
        or not client_order_id
        or query_reference is not None
        and (not isinstance(query_reference, str) or not query_reference)
    ):
        raise RuntimeError("0013 legacy quarantine cannot be scoped safely")
    provenance = evidence.get("provenance_fingerprint")
    if not isinstance(provenance, str) or len(provenance) != 64:
        canonical = json.dumps(
            evidence,
            default=str,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        provenance = hashlib.sha256(canonical.encode()).hexdigest()
    return (
        account_id,
        economic_key,
        client_order_id,
        provenance,
        query_reference,
        evidence,
    )


def _create_quarantine_source_table(bind: sa.Connection) -> None:
    if not _table_exists(bind, _QUARANTINE_SOURCE_TABLE):
        op.create_table(
            _QUARANTINE_SOURCE_TABLE,
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("quarantine_id", sa.String(length=128), nullable=False),
            sa.Column("account_id", sa.String(length=128), nullable=False),
            sa.Column("source_kind", sa.String(length=64), nullable=False),
            sa.Column("source_table", sa.String(length=128), nullable=False),
            sa.Column("source_row_id", sa.String(length=128), nullable=False),
            sa.Column("source_identity", sa.String(length=256), nullable=False),
            sa.Column("client_order_id", sa.String(length=128), nullable=False),
            sa.Column("query_reference", sa.String(length=128), nullable=True),
            sa.Column("provenance_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("evidence", sa.JSON(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.ForeignKeyConstraint(
                ["quarantine_id"],
                [f"{_QUARANTINE_TABLE}.quarantine_id"],
                name="fk_evidence_quarantine_source_case",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "source_kind",
                "source_table",
                "source_row_id",
                name="uq_evidence_quarantine_source_identity",
            ),
        )
    if os.environ.get("UTA_0013_FAIL_MID_OPERATION") == "quarantine:table":
        raise RuntimeError("0013 mid-operation failure injected at quarantine:table")

    existing_indexes = {
        str(index["name"]) for index in sa.inspect(bind).get_indexes(_QUARANTINE_SOURCE_TABLE)
    }
    for column_name in ("quarantine_id", "account_id"):
        index_name = f"ix_{_QUARANTINE_SOURCE_TABLE}_{column_name}"
        if index_name not in existing_indexes:
            op.create_index(index_name, _QUARANTINE_SOURCE_TABLE, [column_name])
    if os.environ.get("UTA_0013_FAIL_MID_OPERATION") == "quarantine:indexes":
        raise RuntimeError("0013 mid-operation failure injected at quarantine:indexes")


def _backfill_quarantine_sources(bind: sa.Connection) -> None:
    if not _table_exists(bind, _QUARANTINE_TABLE):
        raise RuntimeError("0013 requires canonical quarantine cases")
    canonical = sa.Table(_QUARANTINE_TABLE, sa.MetaData(), autoload_with=bind)
    sources = sa.Table(_QUARANTINE_SOURCE_TABLE, sa.MetaData(), autoload_with=bind)
    if _table_exists(bind, _LEGACY_QUARANTINE_TABLE):
        legacy = sa.Table(_LEGACY_QUARANTINE_TABLE, sa.MetaData(), autoload_with=bind)
        legacy_rows = tuple(
            bind.execute(
                sa.select(legacy)
                .where(legacy.c.reconciliation_required.is_(True))
                .order_by(legacy.c.id)
            ).mappings()
        )
        for row in legacy_rows:
            (
                account_id,
                economic_key,
                client_order_id,
                provenance,
                query_reference,
                evidence,
            ) = _legacy_quarantine_identity(row)
            quarantine = (
                bind.execute(
                    sa.select(canonical).where(
                        canonical.c.account_id == account_id,
                        canonical.c.economic_key == economic_key,
                        canonical.c.provenance_fingerprint == provenance,
                    )
                )
                .mappings()
                .first()
            )
            if quarantine is None:
                quarantine_id = f"migration-quarantine-{row['id']}"
                bind.execute(
                    canonical.insert().values(
                        quarantine_id=quarantine_id,
                        account_id=account_id,
                        economic_key=economic_key,
                        client_order_id=client_order_id,
                        query_reference=query_reference,
                        provenance_fingerprint=provenance,
                        reason=f"MIGRATION_{row['reason']}"[:128],
                        evidence=evidence,
                    )
                )
            else:
                quarantine_id = str(quarantine["quarantine_id"])
            source_row_id = str(row["id"])
            existing = (
                bind.execute(
                    sa.select(sources).where(
                        sources.c.source_kind == "migration_quarantine",
                        sources.c.source_table == str(row["source_table"]),
                        sources.c.source_row_id == source_row_id,
                    )
                )
                .mappings()
                .first()
            )
            values = {
                "quarantine_id": quarantine_id,
                "account_id": account_id,
                "source_kind": "migration_quarantine",
                "source_table": str(row["source_table"]),
                "source_row_id": source_row_id,
                "source_identity": str(row["source_identity"]),
                "client_order_id": client_order_id,
                "query_reference": query_reference,
                "provenance_fingerprint": provenance,
                "evidence": evidence,
            }
            if existing is None:
                bind.execute(sources.insert().values(**values))
            elif any(str(existing[key]) != str(value) for key, value in values.items()):
                raise RuntimeError("0013 quarantine source identity has conflicting evidence")

    canonical_rows = tuple(bind.execute(sa.select(canonical)).mappings())
    for quarantine in canonical_rows:
        linked = bind.scalar(
            sa.select(sa.func.count())
            .select_from(sources)
            .where(sources.c.quarantine_id == quarantine["quarantine_id"])
        )
        if linked:
            continue
        bind.execute(
            sources.insert().values(
                quarantine_id=str(quarantine["quarantine_id"]),
                account_id=str(quarantine["account_id"]),
                source_kind="durable_quarantine",
                source_table=_QUARANTINE_TABLE,
                source_row_id=str(quarantine["quarantine_id"]),
                source_identity=str(quarantine["client_order_id"]),
                client_order_id=str(quarantine["client_order_id"]),
                query_reference=quarantine["query_reference"],
                provenance_fingerprint=str(quarantine["provenance_fingerprint"]),
                evidence=quarantine["evidence"],
            )
        )
    if os.environ.get("UTA_0013_FAIL_MID_OPERATION") == "quarantine:backfill":
        raise RuntimeError("0013 mid-operation failure injected at quarantine:backfill")


def _install_quarantine_source_guard(bind: sa.Connection) -> None:
    if bind.dialect.name == "sqlite":
        for operation in ("update", "delete"):
            bind.execute(
                sa.text(
                    f"CREATE TRIGGER IF NOT EXISTS prevent_{_QUARANTINE_SOURCE_TABLE}_{operation} "
                    f"BEFORE {operation.upper()} ON {_QUARANTINE_SOURCE_TABLE} BEGIN "
                    "SELECT RAISE(ABORT, 'quarantine sources are append-only'); END"
                )
            )
    elif bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "CREATE OR REPLACE FUNCTION reject_immutable_evidence_mutation() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'immutable evidence is append-only'; "
                "RETURN NULL; END; $$"
            )
        )
        trigger_name = f"prevent_{_QUARANTINE_SOURCE_TABLE}_mutation"
        bind.execute(
            sa.text(f"DROP TRIGGER IF EXISTS {trigger_name} ON {_QUARANTINE_SOURCE_TABLE}")
        )
        bind.execute(
            sa.text(
                f"CREATE TRIGGER {trigger_name} BEFORE UPDATE OR DELETE OR TRUNCATE "
                f"ON {_QUARANTINE_SOURCE_TABLE} FOR EACH STATEMENT "
                "EXECUTE FUNCTION reject_immutable_evidence_mutation()"
            )
        )
    if os.environ.get("UTA_0013_FAIL_MID_OPERATION") == "quarantine:guard":
        raise RuntimeError("0013 mid-operation failure injected at quarantine:guard")


def _quarantine_sources_complete(bind: sa.Connection) -> bool:
    if not _table_exists(bind, _QUARANTINE_SOURCE_TABLE):
        return False
    inspector = sa.inspect(bind)
    columns = {
        str(column["name"]): column for column in inspector.get_columns(_QUARANTINE_SOURCE_TABLE)
    }
    required_non_null = {
        "id",
        "quarantine_id",
        "account_id",
        "source_kind",
        "source_table",
        "source_row_id",
        "source_identity",
        "client_order_id",
        "provenance_fingerprint",
        "evidence",
        "created_at",
    }
    if not required_non_null.issubset(columns) or any(
        bool(columns[name]["nullable"]) for name in required_non_null
    ):
        return False
    if "query_reference" not in columns:
        return False
    indexes = {str(index["name"]) for index in inspector.get_indexes(_QUARANTINE_SOURCE_TABLE)}
    if not {
        f"ix_{_QUARANTINE_SOURCE_TABLE}_quarantine_id",
        f"ix_{_QUARANTINE_SOURCE_TABLE}_account_id",
    }.issubset(indexes):
        return False
    unique_sets = {
        tuple(str(column) for column in constraint["column_names"])
        for constraint in inspector.get_unique_constraints(_QUARANTINE_SOURCE_TABLE)
    }
    if ("source_kind", "source_table", "source_row_id") not in unique_sets:
        return False
    foreign_keys = inspector.get_foreign_keys(_QUARANTINE_SOURCE_TABLE)
    if not any(
        tuple(key.get("constrained_columns", ())) == ("quarantine_id",)
        and key.get("referred_table") == _QUARANTINE_TABLE
        for key in foreign_keys
    ):
        return False
    if bind.dialect.name == "sqlite":
        trigger_names = set(
            bind.scalars(sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))
        )
        if not all(
            f"prevent_{_QUARANTINE_SOURCE_TABLE}_{operation}" in trigger_names
            for operation in ("update", "delete")
        ):
            return False
    elif bind.dialect.name == "postgresql":
        trigger_name = f"prevent_{_QUARANTINE_SOURCE_TABLE}_mutation"
        if not bind.scalar(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM pg_trigger triggers "
                "JOIN pg_class tables ON tables.oid = triggers.tgrelid "
                "WHERE tables.relname = :table_name AND triggers.tgname = :trigger_name "
                "AND NOT triggers.tgisinternal)"
            ),
            {"table_name": _QUARANTINE_SOURCE_TABLE, "trigger_name": trigger_name},
        ):
            return False

    canonical = sa.Table(_QUARANTINE_TABLE, sa.MetaData(), autoload_with=bind)
    sources = sa.Table(_QUARANTINE_SOURCE_TABLE, sa.MetaData(), autoload_with=bind)
    unlinked_cases = bind.scalar(
        sa.select(sa.func.count())
        .select_from(canonical)
        .where(
            ~sa.exists(
                sa.select(sources.c.id).where(sources.c.quarantine_id == canonical.c.quarantine_id)
            )
        )
    )
    if unlinked_cases:
        return False
    if _table_exists(bind, _LEGACY_QUARANTINE_TABLE):
        legacy = sa.Table(_LEGACY_QUARANTINE_TABLE, sa.MetaData(), autoload_with=bind)
        linked_legacy_ids = {
            str(value)
            for value in bind.scalars(
                sa.select(sources.c.source_row_id).where(
                    sources.c.source_kind == "migration_quarantine"
                )
            )
        }
        legacy_ids = {
            str(value)
            for value in bind.scalars(
                sa.select(legacy.c.id).where(legacy.c.reconciliation_required.is_(True))
            )
        }
        if not legacy_ids.issubset(linked_legacy_ids):
            return False
    return True


def _migrate_quarantine_sources(bind: sa.Connection) -> None:
    _create_quarantine_source_table(bind)
    _backfill_quarantine_sources(bind)
    _install_quarantine_source_guard(bind)


def _reachable_runtime_roles(bind: sa.Connection) -> tuple[str, ...]:
    rows = bind.execute(
        sa.text(
            "WITH RECURSIVE reachable(oid) AS ("
            " SELECT oid FROM pg_roles WHERE rolname = 'uta_runtime'"
            " UNION"
            " SELECT memberships.roleid FROM pg_auth_members memberships"
            " JOIN reachable ON memberships.member = reachable.oid"
            " WHERE memberships.inherit_option OR memberships.set_option"
            ") SELECT roles.rolname FROM reachable"
            " JOIN pg_roles roles ON roles.oid = reachable.oid ORDER BY roles.rolname"
        )
    )
    names = tuple(str(row[0]) for row in rows)
    if "uta_runtime" not in names:
        raise RuntimeError("uta_runtime role is missing")
    return names


def _assert_safe_role_graph(bind: sa.Connection) -> tuple[str, ...]:
    admin_path = bind.scalar(
        sa.text(
            "WITH RECURSIVE reachable(oid) AS ("
            " SELECT oid FROM pg_roles WHERE rolname = 'uta_runtime'"
            " UNION"
            " SELECT memberships.roleid FROM pg_auth_members memberships"
            " JOIN reachable ON memberships.member = reachable.oid"
            " WHERE memberships.inherit_option OR memberships.set_option"
            ") SELECT EXISTS (SELECT 1 FROM pg_auth_members memberships"
            " JOIN reachable ON memberships.member = reachable.oid"
            " WHERE memberships.admin_option)"
        )
    )
    if admin_path:
        raise RuntimeError("uta_runtime role graph contains an admin-capable membership")
    reachable = _reachable_runtime_roles(bind)
    forbidden = {
        "uta_policy_config",
        "pg_database_owner",
        "pg_write_all_data",
    }
    for role_name in reachable:
        role = bind.execute(
            sa.text(
                "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls "
                "FROM pg_roles WHERE rolname = :role_name"
            ),
            {"role_name": role_name},
        ).one()
        owns_policy_table = bind.scalar(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM pg_class classes"
                " JOIN pg_roles owners ON owners.oid = classes.relowner"
                " WHERE owners.rolname = :role_name AND classes.relname = ANY(:tables))"
            ),
            {"role_name": role_name, "tables": list(_POLICY_TABLES)},
        )
        if role_name in forbidden or any(bool(value) for value in role) or owns_policy_table:
            raise RuntimeError(f"uta_runtime effective role graph reaches write role: {role_name}")
    return reachable


def _quote(bind: sa.Connection, identifier: str) -> str:
    return bind.dialect.identifier_preparer.quote(identifier)


def _revoke_policy_acl(bind: sa.Connection) -> None:
    if bind.dialect.name != "postgresql":
        return
    reachable = _assert_safe_role_graph(bind)
    bind.execute(
        sa.text(
            "ALTER ROLE uta_runtime WITH NOSUPERUSER NOCREATEROLE NOCREATEDB "
            "NOREPLICATION NOBYPASSRLS NOINHERIT"
        )
    )
    bind.execute(sa.text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
    bind.execute(sa.text("REVOKE CREATE ON SCHEMA public FROM uta_runtime"))
    bind.execute(
        sa.text(
            "DO $$ BEGIN "
            "EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database()); "
            "EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM uta_runtime', "
            "current_database()); END $$"
        )
    )
    recipients = ("PUBLIC", *reachable)
    for table_name in _POLICY_TABLES:
        quoted_table = _quote(bind, table_name)
        columns = tuple(_column_names(bind, table_name))
        for recipient in recipients:
            quoted_recipient = recipient if recipient == "PUBLIC" else _quote(bind, recipient)
            bind.execute(
                sa.text(f"REVOKE ALL PRIVILEGES ON TABLE {quoted_table} FROM {quoted_recipient}")
            )
            for column_name in columns:
                quoted_column = _quote(bind, column_name)
                for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                    bind.execute(
                        sa.text(
                            f"REVOKE {privilege} ({quoted_column}) ON TABLE "
                            f"{quoted_table} FROM {quoted_recipient}"
                        )
                    )
        bind.execute(sa.text(f"GRANT SELECT ON TABLE {quoted_table} TO uta_runtime"))


def _runtime_probe_is_denied(bind: sa.Connection, statement: str) -> bool:
    bind.execute(sa.text("SAVEPOINT uta_runtime_0013_probe"))
    try:
        bind.execute(sa.text(statement))
    except sa.exc.DBAPIError:
        bind.execute(sa.text("ROLLBACK TO SAVEPOINT uta_runtime_0013_probe"))
        bind.execute(sa.text("RELEASE SAVEPOINT uta_runtime_0013_probe"))
        return True
    bind.execute(sa.text("ROLLBACK TO SAVEPOINT uta_runtime_0013_probe"))
    bind.execute(sa.text("RELEASE SAVEPOINT uta_runtime_0013_probe"))
    return False


def _verify_runtime_session_privileges(bind: sa.Connection) -> None:
    """Prove effective policy access under the actual runtime identity."""
    if bind.dialect.name != "postgresql":
        return
    _assert_safe_role_graph(bind)
    bind.execute(sa.text("RESET ROLE"))
    bind.execute(sa.text("SET LOCAL SESSION AUTHORIZATION uta_runtime"))
    try:
        if bind.scalar(sa.text("SELECT current_user")) != "uta_runtime":
            raise RuntimeError("unable to enter uta_runtime role")
        for table_name in _POLICY_TABLES:
            quoted_table = _quote(bind, table_name)
            columns = tuple(sorted(_column_names(bind, table_name)))
            if not bind.scalar(
                sa.text("SELECT has_table_privilege(current_user, :table, 'SELECT')"),
                {"table": table_name},
            ):
                raise RuntimeError("uta_runtime lost required policy SELECT privilege")
            bind.execute(sa.text(f"SELECT 1 FROM {quoted_table} LIMIT 0"))
            for column_name in columns:
                for privilege in ("INSERT", "UPDATE", "REFERENCES"):
                    if bind.scalar(
                        sa.text(
                            "SELECT has_column_privilege(current_user, :table, :column, :privilege)"
                        ),
                        {
                            "table": table_name,
                            "column": column_name,
                            "privilege": privilege,
                        },
                    ):
                        raise RuntimeError(
                            "uta_runtime effective policy-column write was not denied"
                        )
                quoted_column = _quote(bind, column_name)
                for statement in (
                    f"INSERT INTO {quoted_table} ({quoted_column}) "
                    f"SELECT {quoted_column} FROM {quoted_table} WHERE FALSE",
                    f"UPDATE {quoted_table} SET {quoted_column} = {quoted_column} WHERE FALSE",
                ):
                    if not _runtime_probe_is_denied(bind, statement):
                        raise RuntimeError(
                            "uta_runtime effective policy-column write was not denied"
                        )
            for privilege in ("DELETE", "TRUNCATE", "TRIGGER"):
                if bind.scalar(
                    sa.text("SELECT has_table_privilege(current_user, :table, :privilege)"),
                    {"table": table_name, "privilege": privilege},
                ):
                    raise RuntimeError("uta_runtime effective policy-table write was not denied")
            for statement in (
                f"DELETE FROM {quoted_table} WHERE FALSE",
                f"TRUNCATE TABLE {quoted_table}",
                f"ALTER TABLE {quoted_table} ADD COLUMN uta_runtime_0013_probe integer",
                f"DROP TABLE {quoted_table}",
                f"CREATE TRIGGER uta_runtime_0013_probe BEFORE UPDATE ON {quoted_table} "
                "FOR EACH ROW EXECUTE FUNCTION suppress_redundant_updates_trigger()",
            ):
                if not _runtime_probe_is_denied(bind, statement):
                    raise RuntimeError("uta_runtime effective policy DDL/write was not denied")
        for target_role in ("uta_policy_config", "pg_write_all_data", "pg_database_owner"):
            exists = bind.scalar(
                sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role_name)"),
                {"role_name": target_role},
            )
            if exists and not _runtime_probe_is_denied(
                bind,
                f"SET LOCAL ROLE {_quote(bind, target_role)}",
            ):
                raise RuntimeError("uta_runtime can SET ROLE into a write-capable role")
    finally:
        bind.execute(sa.text("RESET SESSION AUTHORIZATION"))


def _acl_complete(bind: sa.Connection) -> bool:
    if bind.dialect.name != "postgresql":
        return True
    try:
        _verify_runtime_session_privileges(bind)
    except (RuntimeError, sa.exc.DBAPIError):
        return False
    return True


def _verify(bind: sa.Connection) -> None:
    if not _fill_columns_complete(bind):
        raise RuntimeError("0013 durable fill identity postcondition failed")
    if not _quarantine_sources_complete(bind):
        raise RuntimeError("0013 quarantine source-link postcondition failed")
    if bind.dialect.name == "postgresql":
        _verify_runtime_session_privileges(bind)
    for checkpoint in (
        "fill_fact_identity",
        "quarantine_source_links",
        "runtime_policy_acl",
    ):
        if not _marker_completed(bind, checkpoint):
            raise RuntimeError(f"0013 migration marker missing: {checkpoint}")


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, _MARKERS):
        raise RuntimeError("0013 requires migration execution markers from 0012")
    _run_resumable_step(
        bind,
        "fill_fact_identity",
        lambda: _add_fill_fact_columns(bind),
        lambda: _fill_columns_complete(bind),
    )
    _run_resumable_step(
        bind,
        "quarantine_source_links",
        lambda: _migrate_quarantine_sources(bind),
        lambda: _quarantine_sources_complete(bind),
    )
    _run_resumable_step(
        bind,
        "runtime_policy_acl",
        lambda: _revoke_policy_acl(bind),
        lambda: _acl_complete(bind),
    )
    _verify(bind)


def downgrade() -> None:
    raise RuntimeError("0013 execution safety migration is forward-only")
