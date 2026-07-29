"""encrypted durable storage for x402 batch-settlement channels

The six reporting-ledger tables deliberately do not store signed payment
artefacts. This separate table holds the SDK channel record encrypted by
`app.channels.DatabaseChannelStorage`, allowing the web process and a one-shot
batch closer to share the same cumulative voucher without exposing it through
the dashboard.

Revision ID: 0003_channel_state
Revises: 0002_demo_flag
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_channel_state"
down_revision: str | None = "0002_demo_flag"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "channel_state",
        sa.Column("channel_id", sa.String(length=80), nullable=False),
        sa.Column("payload_encrypted", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("channel_id"),
    )
    op.create_index(
        "ix_channel_state_updated_at",
        "channel_state",
        ["updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_channel_state_updated_at", table_name="channel_state")
    op.drop_table("channel_state")
