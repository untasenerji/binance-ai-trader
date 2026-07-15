import hashlib
import os
from logging.config import fileConfig

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


def _prepare_published_0009_upgrade(connection: Connection) -> None:
    """Let untouched 0009 run over populated 0008 evidence safely.

    Published 0009 updates append-only rows and creates a unique query-reference
    index before the new forward repair can run. This bootstrap only removes that
    mechanical obstruction; 0011 performs validation, quarantine, and restoration.
    """
    inspector = inspect(connection)
    if not inspector.has_table("alembic_version"):
        return
    current_revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    if current_revision != "0008_processed_events_temp" or not inspector.has_table(
        "durable_intent_absence_observations"
    ):
        return
    table_name = "durable_intent_absence_observations"
    if connection.dialect.name == "sqlite":
        for operation in ("update", "delete"):
            connection.execute(text(f"DROP TRIGGER IF EXISTS prevent_{table_name}_{operation}"))
    elif connection.dialect.name == "postgresql":
        connection.execute(
            text(
                "DROP TRIGGER IF EXISTS "
                "prevent_durable_intent_absence_observations_mutation "
                "ON durable_intent_absence_observations"
            )
        )

    seen: set[str] = set()
    rows = connection.execute(
        text(
            "SELECT o.id, o.client_order_id, o.economic_key, o.source, "
            "o.query_reference, o.query_client_order_id, o.query_economic_key, "
            "o.query_started_at_ms, o.observed_at_ms, i.unknown_at_ms "
            "FROM durable_intent_absence_observations o "
            "LEFT JOIN durable_order_intents i "
            "ON i.client_order_id = o.client_order_id ORDER BY o.id"
        )
    ).mappings()
    for row in rows:
        reference = str(row["query_reference"] or "")
        if not reference or reference in seen:
            seed = (
                f"{row['id']}:{row['client_order_id']}:{row['economic_key']}:"
                f"{row['source']}:{row['observed_at_ms']}"
            )
            reference = f"legacy-query:{row['id']}:{hashlib.sha256(seed.encode()).hexdigest()[:24]}"
        connection.execute(
            text(
                "UPDATE durable_intent_absence_observations SET "
                "query_reference = :reference, "
                "query_client_order_id = :query_client_order_id, "
                "query_economic_key = :query_economic_key, "
                "query_started_at_ms = :query_started_at_ms WHERE id = :id"
            ),
            {
                "reference": reference,
                "query_client_order_id": row["query_client_order_id"] or row["client_order_id"],
                "query_economic_key": row["query_economic_key"] or row["economic_key"],
                "query_started_at_ms": row["query_started_at_ms"]
                if row["query_started_at_ms"] is not None
                else (row["unknown_at_ms"] or row["observed_at_ms"]),
                "id": row["id"],
            },
        )
        seen.add(reference)


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
