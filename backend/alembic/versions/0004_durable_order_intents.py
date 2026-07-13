"""add durable simulated order-intent ledger

Revision ID: 0004_durable_order_intents
Revises: 0003_transition_reducer
Create Date: 2026-07-12
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_durable_order_intents"
down_revision: str | None = "0003_transition_reducer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "durable_order_intents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("economic_key", sa.String(length=256), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("client_order_id", sa.String(length=128), nullable=False),
        sa.Column("plan_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("stage_index", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.String(length=64), nullable=False),
        sa.Column("price", sa.String(length=64), nullable=False),
        sa.Column("filled_quantity", sa.String(length=64), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_order_id"),
        sa.UniqueConstraint("economic_key", "attempt_number", name="uq_durable_intent_attempt"),
    )
    op.create_index(
        "ix_durable_order_intents_economic_key",
        "durable_order_intents",
        ["economic_key"],
        unique=False,
    )
    op.create_index(
        "ix_durable_order_intents_client_order_id",
        "durable_order_intents",
        ["client_order_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_durable_order_intents_client_order_id", table_name="durable_order_intents")
    op.drop_index("ix_durable_order_intents_economic_key", table_name="durable_order_intents")
    op.drop_table("durable_order_intents")
