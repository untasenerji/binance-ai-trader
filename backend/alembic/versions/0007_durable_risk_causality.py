"""persist post-fill risk evidence and causal UNKNOWN query facts

Revision ID: 0007_durable_risk_causality
Revises: 0006_legacy_audit_evidence
Create Date: 2026-07-14
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_durable_risk_causality"
down_revision: str | None = "0006_legacy_audit_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "durable_order_intents",
        sa.Column("submitted_at_ms", sa.Integer(), nullable=True),
    )
    op.add_column(
        "durable_order_intents",
        sa.Column("unknown_at_ms", sa.Integer(), nullable=True),
    )
    op.add_column(
        "durable_intent_absence_observations",
        sa.Column("query_reference", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "durable_intent_absence_observations",
        sa.Column("query_client_order_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "durable_intent_absence_observations",
        sa.Column("query_economic_key", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "durable_intent_absence_observations",
        sa.Column("query_started_at_ms", sa.Integer(), nullable=True),
    )
    op.create_table(
        "durable_actual_risk_policies",
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("worst_stop_exit_price", sa.String(length=64), nullable=False),
        sa.Column("exit_fee_rate", sa.String(length=64), nullable=False),
        sa.Column("funding_buffer_rate", sa.String(length=64), nullable=False),
        sa.Column("funding_interval_count", sa.Integer(), nullable=False),
        sa.Column("risk_budget", sa.String(length=64), nullable=False),
        sa.Column("max_symbol_exposure_usdt", sa.String(length=64), nullable=False),
        sa.Column("max_total_exposure_usdt", sa.String(length=64), nullable=False),
        sa.Column("existing_symbol_exposure_usdt", sa.String(length=64), nullable=False),
        sa.Column("existing_total_exposure_usdt", sa.String(length=64), nullable=False),
        sa.Column("effective_leverage", sa.Integer(), nullable=False),
        sa.Column("required_reserve_usdt", sa.String(length=64), nullable=False),
        sa.Column("effective_equity_usdt", sa.String(length=64), nullable=False),
        sa.Column("protective_stop_reference", sa.String(length=128), nullable=False),
        sa.Column("reduce_only_exit_reference", sa.String(length=128), nullable=False),
        sa.Column("policy_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("plan_id"),
        sa.UniqueConstraint("policy_fingerprint"),
    )
    op.create_table(
        "durable_plan_protections",
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("protective_stop_reference", sa.String(length=128), nullable=False),
        sa.Column("reduce_only_exit_reference", sa.String(length=128), nullable=False),
        sa.Column("confirmed_position_quantity", sa.String(length=64), nullable=False),
        sa.Column("stop_confirmed", sa.Boolean(), nullable=False),
        sa.Column("reduce_only_exit_confirmed", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["durable_actual_risk_policies.plan_id"]),
        sa.PrimaryKeyConstraint("plan_id"),
    )
    op.create_table(
        "durable_actual_risk_states",
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("confirmed_position_quantity", sa.String(length=64), nullable=False),
        sa.Column("average_entry_price", sa.String(length=64), nullable=True),
        sa.Column("actual_notional_usdt", sa.String(length=64), nullable=False),
        sa.Column("actual_required_margin_usdt", sa.String(length=64), nullable=False),
        sa.Column("actual_stop_risk", sa.String(length=64), nullable=False),
        sa.Column("pending_entries_blocked", sa.Boolean(), nullable=False),
        sa.Column("hard_halted", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=96), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["durable_actual_risk_policies.plan_id"]),
        sa.PrimaryKeyConstraint("plan_id"),
    )

    if op.get_bind().dialect.name == "postgresql":
        for table_name in (
            "durable_actual_risk_policies",
            "durable_plan_protections",
            "durable_actual_risk_states",
        ):
            op.execute(sa.text(f"REVOKE ALL ON TABLE {table_name} FROM uta_runtime"))
            op.execute(
                sa.text(f"GRANT SELECT, INSERT, UPDATE ON TABLE {table_name} TO uta_runtime")
            )


def downgrade() -> None:
    op.drop_table("durable_actual_risk_states")
    op.drop_table("durable_plan_protections")
    op.drop_table("durable_actual_risk_policies")
    op.drop_column("durable_intent_absence_observations", "query_started_at_ms")
    op.drop_column("durable_intent_absence_observations", "query_economic_key")
    op.drop_column("durable_intent_absence_observations", "query_client_order_id")
    op.drop_column("durable_intent_absence_observations", "query_reference")
    op.drop_column("durable_order_intents", "unknown_at_ms")
    op.drop_column("durable_order_intents", "submitted_at_ms")
