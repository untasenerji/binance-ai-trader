"""add replayable state transition sequence metadata

Revision ID: 0003_transition_reducer
Revises: 0002_audit_chain_head
Create Date: 2026-07-12
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_transition_reducer"
down_revision: str | None = "0002_audit_chain_head"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trade_plan_projections",
        sa.Column("plan_version", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "trade_plan_projections",
        sa.Column("source_sequence", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("trade_plan_projections", "source_sequence")
    op.drop_column("trade_plan_projections", "plan_version")
