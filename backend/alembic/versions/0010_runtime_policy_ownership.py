"""Separate runtime reads from durable risk-policy ownership.

Revision ID: 0010_runtime_policy_owner
Revises: 0009_evidence_risk_hardening
Create Date: 2026-07-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_runtime_policy_owner"
down_revision: str | None = "0009_evidence_risk_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY_TABLES = (
    "durable_actual_risk_policies",
    "durable_actual_risk_policy_versions",
)
_POLICY_VERSION_SEQUENCE = "durable_actual_risk_policy_versions_id_seq"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    bind.execute(
        sa.text(
            "ALTER ROLE uta_runtime WITH NOSUPERUSER NOCREATEROLE NOCREATEDB "
            "NOREPLICATION NOBYPASSRLS NOINHERIT"
        )
    )
    bind.execute(
        sa.text(
            "DO $$ BEGIN IF NOT EXISTS "
            "(SELECT 1 FROM pg_roles WHERE rolname = 'uta_policy_config') THEN "
            "CREATE ROLE uta_policy_config NOLOGIN; END IF; END $$"
        )
    )
    bind.execute(
        sa.text(
            "ALTER ROLE uta_policy_config WITH NOLOGIN NOSUPERUSER NOCREATEROLE "
            "NOCREATEDB NOREPLICATION NOBYPASSRLS NOINHERIT"
        )
    )
    bind.execute(sa.text("REVOKE uta_policy_config FROM uta_runtime"))
    bind.execute(sa.text("GRANT USAGE ON SCHEMA public TO uta_policy_config"))
    bind.execute(sa.text("REVOKE CREATE ON SCHEMA public FROM uta_policy_config"))
    bind.execute(
        sa.text(
            "DO $$ BEGIN "
            "EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', "
            "current_database()); "
            "EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM uta_runtime', "
            "current_database()); END $$"
        )
    )

    for table_name in _POLICY_TABLES:
        bind.execute(sa.text(f"ALTER TABLE {table_name} OWNER TO uta_policy_config"))
        bind.execute(sa.text(f"REVOKE ALL ON TABLE {table_name} FROM PUBLIC"))
        bind.execute(sa.text(f"REVOKE ALL ON TABLE {table_name} FROM uta_runtime"))
        bind.execute(sa.text(f"GRANT SELECT ON TABLE {table_name} TO uta_runtime"))

    bind.execute(sa.text(f"ALTER SEQUENCE {_POLICY_VERSION_SEQUENCE} OWNER TO uta_policy_config"))
    bind.execute(sa.text(f"REVOKE ALL ON SEQUENCE {_POLICY_VERSION_SEQUENCE} FROM PUBLIC"))
    bind.execute(sa.text(f"REVOKE ALL ON SEQUENCE {_POLICY_VERSION_SEQUENCE} FROM uta_runtime"))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for table_name in _POLICY_TABLES:
        bind.execute(
            sa.text(
                "DO $$ BEGIN EXECUTE format('ALTER TABLE "
                f"{table_name} OWNER TO %I', current_user); END $$"
            )
        )
        bind.execute(sa.text(f"GRANT SELECT, INSERT ON TABLE {table_name} TO uta_runtime"))
    bind.execute(
        sa.text(
            "DO $$ BEGIN EXECUTE format('ALTER SEQUENCE "
            f"{_POLICY_VERSION_SEQUENCE} OWNER TO %I', current_user); END $$"
        )
    )
    bind.execute(
        sa.text(f"GRANT USAGE, SELECT ON SEQUENCE {_POLICY_VERSION_SEQUENCE} TO uta_runtime")
    )
