"""Engine and session construction kept separate from application startup."""

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

from app.persistence.models import Base


def create_database_engine(database_url: str) -> Engine:
    if not database_url:
        raise ValueError("database_url is required")
    return create_engine(
        database_url, future=True, pool_pre_ping=not database_url.startswith("sqlite")
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def create_schema(engine: Engine) -> None:
    """Development/test helper; production schema changes use Alembic migrations."""
    Base.metadata.create_all(engine)
    _install_audit_append_only_guards(engine)


def _install_sqlite_fill_fact_guards(connection: Connection) -> None:
    table_name = "exchange_fill_fact_journal"
    immutable_columns = (
        "account_id",
        "exchange_trade_id",
        "client_order_id",
        "intent_id",
        "symbol",
        "side",
        "quantity",
        "cumulative_quantity",
        "price",
        "fee",
        "fee_asset",
        "exchange_timestamp",
        "observation_source",
        "observation_reference",
        "observation_correlation",
        "provenance_fingerprint",
        "semantic_fingerprint",
        "materialize_simulated_protection",
        "received_at",
    )
    predicate = " OR ".join(f"NEW.{column} IS NOT OLD.{column}" for column in immutable_columns)
    connection.execute(text(f"DROP TRIGGER IF EXISTS prevent_{table_name}_economic_update"))
    connection.execute(text(f"DROP TRIGGER IF EXISTS prevent_{table_name}_delete"))
    connection.execute(
        text(
            f"CREATE TRIGGER prevent_{table_name}_economic_update "
            f"BEFORE UPDATE ON {table_name} WHEN {predicate} BEGIN "
            "SELECT RAISE(ABORT, 'exchange fill facts are immutable'); END"
        )
    )
    connection.execute(
        text(
            f"CREATE TRIGGER prevent_{table_name}_delete "
            f"BEFORE DELETE ON {table_name} BEGIN "
            "SELECT RAISE(ABORT, 'exchange fill facts are append-only'); END"
        )
    )


def _install_postgresql_fill_fact_guards(connection: Connection) -> None:
    table_name = "exchange_fill_fact_journal"
    connection.execute(
        text(
            "CREATE OR REPLACE FUNCTION reject_exchange_fill_fact_economic_mutation() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN "
            "IF TG_OP = 'DELETE' THEN "
            "RAISE EXCEPTION 'exchange fill facts are append-only'; END IF; "
            "IF NEW.account_id IS DISTINCT FROM OLD.account_id "
            "OR NEW.exchange_trade_id IS DISTINCT FROM OLD.exchange_trade_id "
            "OR NEW.client_order_id IS DISTINCT FROM OLD.client_order_id "
            "OR NEW.intent_id IS DISTINCT FROM OLD.intent_id "
            "OR NEW.symbol IS DISTINCT FROM OLD.symbol OR NEW.side IS DISTINCT FROM OLD.side "
            "OR NEW.quantity IS DISTINCT FROM OLD.quantity "
            "OR NEW.cumulative_quantity IS DISTINCT FROM OLD.cumulative_quantity "
            "OR NEW.price IS DISTINCT FROM OLD.price OR NEW.fee IS DISTINCT FROM OLD.fee "
            "OR NEW.fee_asset IS DISTINCT FROM OLD.fee_asset "
            "OR NEW.exchange_timestamp IS DISTINCT FROM OLD.exchange_timestamp "
            "OR NEW.observation_source IS DISTINCT FROM OLD.observation_source "
            "OR NEW.observation_reference IS DISTINCT FROM OLD.observation_reference "
            "OR NEW.observation_correlation IS DISTINCT FROM OLD.observation_correlation "
            "OR NEW.provenance_fingerprint IS DISTINCT FROM OLD.provenance_fingerprint "
            "OR NEW.semantic_fingerprint IS DISTINCT FROM OLD.semantic_fingerprint "
            "OR NEW.materialize_simulated_protection "
            "IS DISTINCT FROM OLD.materialize_simulated_protection "
            "OR NEW.received_at IS DISTINCT FROM OLD.received_at THEN "
            "RAISE EXCEPTION 'exchange fill fact economic mutation denied'; END IF; "
            "RETURN NEW; END; $$"
        )
    )
    trigger_name = f"prevent_{table_name}_mutation"
    connection.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}"))
    connection.execute(
        text(
            f"CREATE TRIGGER {trigger_name} BEFORE UPDATE OR DELETE ON {table_name} "
            "FOR EACH ROW EXECUTE FUNCTION reject_exchange_fill_fact_economic_mutation()"
        )
    )


def _install_audit_append_only_guards(engine: Engine) -> None:
    """Mirror immutable-evidence migration guards in local test schemas."""
    with engine.begin() as connection:
        if engine.dialect.name == "sqlite":
            for table_name in (
                "audit_events",
                "processed_events",
                "durable_intent_fills",
                "durable_intent_absence_observations",
                "durable_actual_risk_policies",
                "durable_actual_risk_policy_versions",
                "durable_account_portfolio_envelopes",
                "migration_quarantine_records",
                "durable_entry_authorization_grants",
                "durable_entry_authorization_revocations",
                "durable_entry_admission_decisions",
                "durable_portfolio_envelope_heads",
                "durable_portfolio_envelope_supersessions",
                "durable_evidence_quarantines",
                "durable_evidence_quarantine_sources",
                "durable_evidence_quarantine_resolutions",
                "durable_evidence_quarantine_source_resolutions",
            ):
                for operation in ("update", "delete"):
                    connection.execute(
                        text(
                            f"CREATE TRIGGER IF NOT EXISTS prevent_{table_name}_{operation} "
                            f"BEFORE {operation.upper()} ON {table_name} "
                            f"BEGIN SELECT RAISE(ABORT, '{table_name} are append-only'); END"
                        )
                    )
            _install_sqlite_fill_fact_guards(connection)
        elif engine.dialect.name == "postgresql":
            connection.execute(
                text(
                    "CREATE OR REPLACE FUNCTION reject_immutable_evidence_mutation() "
                    "RETURNS trigger LANGUAGE plpgsql AS $$ "
                    "BEGIN RAISE EXCEPTION 'immutable evidence is append-only'; "
                    "RETURN NULL; END; $$"
                )
            )
            for table_name, trigger_name in (
                ("audit_events", "prevent_audit_events_mutation"),
                ("processed_events", "prevent_processed_events_mutation"),
                ("durable_intent_fills", "prevent_durable_intent_fills_mutation"),
                (
                    "durable_intent_absence_observations",
                    "prevent_durable_intent_absence_observations_mutation",
                ),
                (
                    "durable_actual_risk_policies",
                    "prevent_durable_actual_risk_policies_mutation",
                ),
                (
                    "durable_actual_risk_policy_versions",
                    "prevent_durable_actual_risk_policy_versions_mutation",
                ),
                (
                    "durable_account_portfolio_envelopes",
                    "prevent_durable_account_portfolio_envelopes_mutation",
                ),
                (
                    "migration_quarantine_records",
                    "prevent_migration_quarantine_records_mutation",
                ),
                (
                    "durable_entry_authorization_grants",
                    "prevent_durable_entry_authorization_grants_mutation",
                ),
                (
                    "durable_entry_authorization_revocations",
                    "prevent_durable_entry_authorization_revocations_mutation",
                ),
                (
                    "durable_entry_admission_decisions",
                    "prevent_durable_entry_admission_decisions_mutation",
                ),
                (
                    "durable_portfolio_envelope_heads",
                    "prevent_durable_portfolio_envelope_heads_mutation",
                ),
                (
                    "durable_portfolio_envelope_supersessions",
                    "prevent_durable_portfolio_envelope_supersessions_mutation",
                ),
                (
                    "durable_evidence_quarantines",
                    "prevent_durable_evidence_quarantines_mutation",
                ),
                (
                    "durable_evidence_quarantine_sources",
                    "prevent_durable_evidence_quarantine_sources_mutation",
                ),
                (
                    "durable_evidence_quarantine_resolutions",
                    "prevent_durable_evidence_quarantine_resolutions_mutation",
                ),
                (
                    "durable_evidence_quarantine_source_resolutions",
                    "prevent_durable_evidence_quarantine_source_resolutions_mutation",
                ),
            ):
                connection.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}"))
                connection.execute(
                    text(
                        f"CREATE TRIGGER {trigger_name} "
                        f"BEFORE UPDATE OR DELETE OR TRUNCATE ON {table_name} "
                        "FOR EACH STATEMENT EXECUTE FUNCTION reject_immutable_evidence_mutation()"
                    )
                )
            _install_postgresql_fill_fact_guards(connection)
