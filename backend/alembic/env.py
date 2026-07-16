import os
from collections import Counter
from logging.config import fileConfig

import sqlalchemy as sa
from sqlalchemy import Connection, engine_from_config, inspect, pool, text

from alembic import context
from app.persistence.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = os.environ.get("DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


_ABSENCE_TABLE = "durable_intent_absence_observations"
_PREFLIGHT_REVISION = "preflight_0008_to_0009"
_QUARANTINE_TABLE = "migration_quarantine_records"


def _ensure_preflight_quarantine_table(connection: Connection) -> sa.Table:
    """Create the 0011-compatible append-only quarantine target before 0009 runs."""
    metadata = sa.MetaData()
    table = sa.Table(
        _QUARANTINE_TABLE,
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
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
        sa.UniqueConstraint(
            "migration_revision",
            "source_table",
            "source_identity",
            "reason",
            name="uq_migration_quarantine_record",
        ),
    )
    metadata.create_all(connection, tables=[table])
    return sa.Table(_QUARANTINE_TABLE, sa.MetaData(), autoload_with=connection)


def _absence_guard_is_present(connection: Connection) -> bool:
    if connection.dialect.name == "sqlite":
        names = set(
            connection.scalars(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))
        )
        return {
            "prevent_durable_intent_absence_observations_update",
            "prevent_durable_intent_absence_observations_delete",
        } <= names
    if connection.dialect.name == "postgresql":
        return bool(
            connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_trigger "
                    "WHERE tgrelid = to_regclass(:table_name) AND NOT tgisinternal "
                    "AND tgname = 'prevent_durable_intent_absence_observations_mutation')"
                ),
                {"table_name": _ABSENCE_TABLE},
            )
        )
    return True


def _drop_absence_guard(connection: Connection) -> None:
    if connection.dialect.name == "sqlite":
        for operation in ("update", "delete"):
            connection.execute(text(f"DROP TRIGGER IF EXISTS prevent_{_ABSENCE_TABLE}_{operation}"))
        return
    if connection.dialect.name == "postgresql":
        connection.execute(
            text(
                "DROP TRIGGER IF EXISTS "
                "prevent_durable_intent_absence_observations_mutation "
                "ON durable_intent_absence_observations"
            )
        )


def _install_absence_guard(connection: Connection) -> None:
    if connection.dialect.name == "sqlite":
        for operation in ("update", "delete"):
            connection.execute(
                text(
                    f"CREATE TRIGGER IF NOT EXISTS prevent_{_ABSENCE_TABLE}_{operation} "
                    f"BEFORE {operation.upper()} ON {_ABSENCE_TABLE} "
                    "BEGIN SELECT RAISE(ABORT, "
                    "'durable_intent_absence_observations are append-only'); END"
                )
            )
        return
    if connection.dialect.name == "postgresql":
        connection.execute(
            text(
                "CREATE OR REPLACE FUNCTION reject_immutable_evidence_mutation() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'immutable evidence is append-only'; "
                "RETURN NULL; END; $$"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER prevent_durable_intent_absence_observations_mutation "
                "BEFORE UPDATE OR DELETE OR TRUNCATE "
                "ON durable_intent_absence_observations FOR EACH STATEMENT "
                "EXECUTE FUNCTION reject_immutable_evidence_mutation()"
            )
        )


def _json_safe(row: sa.RowMapping) -> dict[str, object]:
    safe: dict[str, object] = {}
    for key, value in row.items():
        if value is None or isinstance(value, (bool, int, float, str)):
            safe[str(key)] = value
        else:
            safe[str(key)] = str(value)
    return safe


def _legacy_absence_reason(row: sa.RowMapping) -> str | None:
    try:
        submitted_at = int(row["submitted_at_ms"])
        unknown_at = int(row["unknown_at_ms"])
        observed_at = int(row["observed_at_ms"])
        stream_watermark = int(row["stream_watermark_ms"])
        query_started = row["query_started_at_ms"]
        query_started_at = unknown_at if query_started is None else int(query_started)
    except (TypeError, ValueError):
        return "PRE_0009_LEGACY_CAUSAL_TIME_MISSING"
    if (
        min(submitted_at, unknown_at, observed_at, stream_watermark, query_started_at) < 0
        or submitted_at > unknown_at
        or query_started_at < unknown_at
        or query_started_at > observed_at
        or observed_at < unknown_at
        or stream_watermark < observed_at
    ):
        return "PRE_0009_LEGACY_CAUSAL_TIME_INVALID"
    return None


def _quarantine_preflight_row(
    connection: Connection,
    table: sa.Table,
    *,
    row: sa.RowMapping,
    reason: str,
) -> None:
    source_identity = f"{row['client_order_id']}:{row['id']}"
    existing = connection.scalar(
        sa.select(sa.func.count())
        .select_from(table)
        .where(
            table.c.migration_revision == _PREFLIGHT_REVISION,
            table.c.source_table == _ABSENCE_TABLE,
            table.c.source_identity == source_identity,
            table.c.reason == reason,
        )
    )
    if existing:
        return
    connection.execute(
        table.insert().values(
            migration_revision=_PREFLIGHT_REVISION,
            source_table=_ABSENCE_TABLE,
            source_identity=source_identity,
            reason=reason,
            evidence=_json_safe(row),
            reconciliation_required=True,
        )
    )


def _prepare_published_0009_upgrade(connection: Connection) -> None:
    """Quarantine unsafe 0008 evidence before published 0009 changes its schema.

    The published 0009 migration cannot handle invalid legacy timelines and has to
    touch an append-only table. This preflight validates with the guard intact,
    preserves the exact original provenance in an append-only quarantine row, and
    removes only rows that cannot be safely normalized. A retry is idempotent.
    """
    inspector = inspect(connection)
    if not inspector.has_table("alembic_version"):
        return
    current_revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    if current_revision != "0008_processed_events_temp" or not inspector.has_table(_ABSENCE_TABLE):
        return
    if not _absence_guard_is_present(connection):
        raise RuntimeError("0008 absence evidence guard is missing before preflight")
    quarantine_table = _ensure_preflight_quarantine_table(connection)
    rows = tuple(
        connection.execute(
            text(
                "SELECT o.id, o.client_order_id, o.economic_key, o.source, "
                "o.query_reference, o.query_client_order_id, o.query_economic_key, "
                "o.query_started_at_ms, o.observed_at_ms, o.stream_watermark_ms, "
                "o.found, i.attempt_number, i.submitted_at_ms, i.unknown_at_ms "
                "FROM durable_intent_absence_observations o "
                "LEFT JOIN durable_order_intents i "
                "ON i.client_order_id = o.client_order_id ORDER BY o.id"
            )
        ).mappings()
    )
    duplicate_references = {
        reference
        for reference, count in Counter(
            str(row["query_reference"])
            for row in rows
            if row["query_reference"] is not None and str(row["query_reference"])
        ).items()
        if count > 1
    }
    invalid_ids: list[int] = []
    for row in rows:
        reason = _legacy_absence_reason(row)
        if reason is None and not isinstance(row["query_reference"], str):
            reason = "PRE_0009_QUERY_REFERENCE_MISSING"
        if reason is None and str(row["query_reference"] or "") in duplicate_references:
            reason = "PRE_0009_DUPLICATE_QUERY_REFERENCE"
        if reason is None:
            continue
        _quarantine_preflight_row(connection, quarantine_table, row=row, reason=reason)
        invalid_ids.append(int(row["id"]))
    if not invalid_ids:
        return
    _drop_absence_guard(connection)
    try:
        for row_id in invalid_ids:
            connection.execute(text(f"DELETE FROM {_ABSENCE_TABLE} WHERE id = :id"), {"id": row_id})
    finally:
        _install_absence_guard(connection)
        if not _absence_guard_is_present(connection):
            raise RuntimeError("0008 absence evidence guard was not restored after preflight")


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            _prepare_published_0009_upgrade(connection)
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
