"""add audit chain head and semantic dedupe metadata

Revision ID: 0002_audit_chain_head
Revises: 0001_initial_persistence
Create Date: 2026-07-12
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_audit_chain_head"
down_revision: str | None = "0001_initial_persistence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_chain_heads",
        sa.Column("chain_id", sa.Integer(), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("last_sequence", sa.Integer(), nullable=False),
        sa.Column("last_record_hash", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("chain_id"),
    )
    op.add_column(
        "audit_events",
        sa.Column("chain_sequence", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "audit_events",
        sa.Column(
            "semantic_fingerprint",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.add_column(
        "audit_events",
        sa.Column(
            "delivery_status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'CANONICAL'"),
        ),
    )
    op.add_column(
        "processed_events",
        sa.Column(
            "semantic_fingerprint",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )

    bind = op.get_bind()
    bind.execute(sa.text("UPDATE audit_events SET chain_sequence = id WHERE chain_sequence = 0"))
    bind.execute(
        sa.text(
            "UPDATE audit_events SET semantic_fingerprint = record_hash "
            "WHERE semantic_fingerprint = ''"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE processed_events SET semantic_fingerprint = event_id "
            "WHERE semantic_fingerprint = ''"
        )
    )
    op.create_index(
        "uq_audit_events_chain_sequence", "audit_events", ["chain_sequence"], unique=True
    )
    head = (
        bind.execute(
            sa.text(
                "SELECT COUNT(*) AS event_count, COALESCE(MAX(chain_sequence), 0) AS last_sequence "
                "FROM audit_events"
            )
        )
        .mappings()
        .one()
    )
    last_record_hash = bind.execute(
        sa.text("SELECT record_hash FROM audit_events ORDER BY chain_sequence DESC LIMIT 1")
    ).scalar_one_or_none()
    bind.execute(
        sa.text(
            "INSERT INTO audit_chain_heads "
            "(chain_id, event_count, last_sequence, last_record_hash, updated_at) "
            "VALUES (1, :event_count, :last_sequence, :last_record_hash, CURRENT_TIMESTAMP)"
        ),
        {
            "event_count": head["event_count"],
            "last_sequence": head["last_sequence"],
            "last_record_hash": last_record_hash,
        },
    )


def downgrade() -> None:
    op.drop_column("processed_events", "semantic_fingerprint")
    op.drop_index("uq_audit_events_chain_sequence", table_name="audit_events")
    op.drop_column("audit_events", "delivery_status")
    op.drop_column("audit_events", "semantic_fingerprint")
    op.drop_column("audit_events", "chain_sequence")
    op.drop_table("audit_chain_heads")
