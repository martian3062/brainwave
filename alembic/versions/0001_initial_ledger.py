"""initial ledger: author, tool, session, call, batch, receipt

Revision ID: 0001_initial_ledger
Revises:
Create Date: 2026-07-27 20:48:13.448322
"""

from collections.abc import Sequence

import sqlalchemy as sa

# Autogenerate emits sqlmodel.sql.sqltypes.AutoString for str columns; without
# this import every generated migration fails with NameError at upgrade time.
import sqlmodel

from alembic import op

revision: str = "0001_initial_ledger"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "author",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("display_name", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("pay_to", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("contact_email", sqlmodel.sql.sqltypes.AutoString(length=254), nullable=True),
        sa.Column("platform_take_bps", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("author", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_author_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_author_is_active"), ["is_active"], unique=False)
        batch_op.create_index(batch_op.f("ix_author_pay_to"), ["pay_to"], unique=False)
        batch_op.create_index(batch_op.f("ix_author_slug"), ["slug"], unique=True)

    op.create_table(
        "pay_session",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("payer", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("agent_label", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True),
        sa.Column("agent_identity", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=True),
        sa.Column("network", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("asset", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("asset_decimals", sa.Integer(), nullable=False),
        sa.Column("scheme", sa.String(length=32), nullable=False),
        sa.Column("settlement_mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("channel_id", sqlmodel.sql.sqltypes.AutoString(length=80), nullable=True),
        sa.Column("budget_atomic", sa.BigInteger(), nullable=True),
        sa.Column("authorized_atomic", sa.BigInteger(), nullable=False),
        sa.Column("captured_atomic", sa.BigInteger(), nullable=False),
        sa.Column("settled_atomic", sa.BigInteger(), nullable=False),
        sa.Column("call_count", sa.Integer(), nullable=False),
        sa.Column("declined_count", sa.Integer(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_call_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("captured_atomic >= 0", name="ck_session_captured_nonneg"),
        sa.CheckConstraint(
            "settled_atomic <= captured_atomic", name="ck_session_settled_le_captured"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("pay_session", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_pay_session_agent_identity"), ["agent_identity"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_pay_session_channel_id"), ["channel_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_pay_session_last_call_at"), ["last_call_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_pay_session_network"), ["network"], unique=False)
        batch_op.create_index(batch_op.f("ix_pay_session_opened_at"), ["opened_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_pay_session_payer"), ["payer"], unique=False)
        batch_op.create_index(batch_op.f("ix_pay_session_session_id"), ["session_id"], unique=True)
        batch_op.create_index(
            batch_op.f("ix_pay_session_settlement_mode"), ["settlement_mode"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_pay_session_status"), ["status"], unique=False)
        batch_op.create_index("ix_session_payer_status", ["payer", "status"], unique=False)
        batch_op.create_index("ix_session_status_opened", ["status", "opened_at"], unique=False)

    op.create_table(
        "batch",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sqlmodel.sql.sqltypes.AutoString(length=80), nullable=True),
        sa.Column("network", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("asset", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("asset_decimals", sa.Integer(), nullable=False),
        sa.Column("pay_to", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("call_count", sa.Integer(), nullable=False),
        sa.Column("gross_atomic", sa.BigInteger(), nullable=False),
        sa.Column("platform_fee_atomic", sa.BigInteger(), nullable=False),
        sa.Column("author_net_atomic", sa.BigInteger(), nullable=False),
        sa.Column("facilitator_fee_atomic", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("claim_tx_hash", sqlmodel.sql.sqltypes.AutoString(length=80), nullable=True),
        sa.Column("settle_tx_hash", sqlmodel.sql.sqltypes.AutoString(length=80), nullable=True),
        sa.Column("explorer_url", sqlmodel.sql.sqltypes.AutoString(length=256), nullable=True),
        sa.Column("error", sqlmodel.sql.sqltypes.AutoString(length=512), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("gross_atomic >= 0", name="ck_batch_gross_nonneg"),
        sa.CheckConstraint(
            "platform_fee_atomic + author_net_atomic = gross_atomic",
            name="ck_batch_split_conserves",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["pay_session.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("batch", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_batch_batch_id"), ["batch_id"], unique=True)
        batch_op.create_index(batch_op.f("ix_batch_channel_id"), ["channel_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_batch_claim_tx_hash"), ["claim_tx_hash"], unique=False)
        batch_op.create_index(batch_op.f("ix_batch_network"), ["network"], unique=False)
        batch_op.create_index(batch_op.f("ix_batch_opened_at"), ["opened_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_batch_pay_to"), ["pay_to"], unique=False)
        batch_op.create_index(batch_op.f("ix_batch_session_id"), ["session_id"], unique=False)
        batch_op.create_index("ix_batch_session_status", ["session_id", "status"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_batch_settle_tx_hash"), ["settle_tx_hash"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_batch_settled_at"), ["settled_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_batch_status"), ["status"], unique=False)
        batch_op.create_index("ix_batch_status_opened", ["status", "opened_at"], unique=False)

    op.create_table(
        "tool",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(length=128), nullable=False),
        sa.Column("resource_url", sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
        sa.Column("description", sqlmodel.sql.sqltypes.AutoString(length=1024), nullable=True),
        sa.Column("tags", sqlmodel.sql.sqltypes.AutoString(length=256), nullable=True),
        sa.Column("scheme", sa.String(length=32), nullable=False),
        sa.Column("network", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("asset", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("asset_decimals", sa.Integer(), nullable=False),
        sa.Column("price_atomic", sa.BigInteger(), nullable=False),
        sa.Column("max_price_atomic", sa.BigInteger(), nullable=True),
        sa.Column("meter", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=True),
        sa.Column("price_per_unit_atomic", sa.BigInteger(), nullable=True),
        sa.Column("max_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("total_calls", sa.Integer(), nullable=False),
        sa.Column("total_captured_atomic", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "max_price_atomic IS NULL OR max_price_atomic >= price_atomic",
            name="ck_tool_ceiling_ge_price",
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["author.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("tool", schema=None) as batch_op:
        batch_op.create_index("ix_tool_author_enabled", ["author_id", "enabled"], unique=False)
        batch_op.create_index(batch_op.f("ix_tool_author_id"), ["author_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_tool_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_tool_enabled"), ["enabled"], unique=False)
        batch_op.create_index(batch_op.f("ix_tool_name"), ["name"], unique=True)
        batch_op.create_index(batch_op.f("ix_tool_network"), ["network"], unique=False)
        batch_op.create_index(batch_op.f("ix_tool_scheme"), ["scheme"], unique=False)

    op.create_table(
        "call",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("call_id", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("tool_id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=True),
        sa.Column("payer", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("pay_to", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("network", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("asset", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("scheme", sa.String(length=32), nullable=False),
        sa.Column("authorized_atomic", sa.BigInteger(), nullable=False),
        sa.Column("captured_atomic", sa.BigInteger(), nullable=False),
        sa.Column("platform_fee_atomic", sa.BigInteger(), nullable=False),
        sa.Column("author_net_atomic", sa.BigInteger(), nullable=False),
        sa.Column("meter", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=True),
        sa.Column("meter_units", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("decline_reason", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=True),
        sa.Column("error", sqlmodel.sql.sqltypes.AutoString(length=512), nullable=True),
        sa.Column("nonce", sqlmodel.sql.sqltypes.AutoString(length=80), nullable=True),
        sa.Column("verify_ms", sa.Integer(), nullable=True),
        sa.Column("execute_ms", sa.Integer(), nullable=True),
        sa.Column("settle_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "captured_atomic <= authorized_atomic", name="ck_call_capture_le_authorized"
        ),
        sa.CheckConstraint("captured_atomic >= 0", name="ck_call_captured_nonneg"),
        sa.CheckConstraint(
            "platform_fee_atomic + author_net_atomic = captured_atomic",
            name="ck_call_split_conserves",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["batch.id"],
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["pay_session.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tool_id"],
            ["tool.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("network", "nonce", name="uq_call_network_nonce"),
    )
    with op.batch_alter_table("call", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_call_batch_id"), ["batch_id"], unique=False)
        batch_op.create_index("ix_call_batch_status", ["batch_id", "status"], unique=False)
        batch_op.create_index(batch_op.f("ix_call_call_id"), ["call_id"], unique=True)
        batch_op.create_index(batch_op.f("ix_call_created_at"), ["created_at"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_call_decline_reason"), ["decline_reason"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_call_payer"), ["payer"], unique=False)
        batch_op.create_index("ix_call_session_created", ["session_id", "created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_call_session_id"), ["session_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_call_status"), ["status"], unique=False)
        batch_op.create_index("ix_call_status_created", ["status", "created_at"], unique=False)
        batch_op.create_index("ix_call_tool_created", ["tool_id", "created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_call_tool_id"), ["tool_id"], unique=False)

    op.create_table(
        "receipt",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("receipt_id", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("call_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=True),
        sa.Column("scheme", sa.String(length=32), nullable=False),
        sa.Column("network", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("asset", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("asset_decimals", sa.Integer(), nullable=False),
        sa.Column("authorized_atomic", sa.BigInteger(), nullable=False),
        sa.Column("captured_atomic", sa.BigInteger(), nullable=False),
        sa.Column("payer", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("pay_to", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("resource_url", sqlmodel.sql.sqltypes.AutoString(length=512), nullable=False),
        sa.Column("settlement", sa.String(length=32), nullable=False),
        sa.Column("tx_hash", sqlmodel.sql.sqltypes.AutoString(length=80), nullable=True),
        sa.Column("explorer_url", sqlmodel.sql.sqltypes.AutoString(length=256), nullable=True),
        sa.Column("facilitator", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("attestation", sqlmodel.sql.sqltypes.AutoString(length=256), nullable=True),
        sa.Column("body_hash", sqlmodel.sql.sqltypes.AutoString(length=80), nullable=False),
        sa.Column("body_json", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verify_status", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=True),
        sa.CheckConstraint(
            "captured_atomic <= authorized_atomic", name="ck_receipt_capture_le_authorized"
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["batch.id"],
        ),
        sa.ForeignKeyConstraint(
            ["call_id"],
            ["call.id"],
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["pay_session.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("receipt", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_receipt_batch_id"), ["batch_id"], unique=False)
        batch_op.create_index("ix_receipt_batch_issued", ["batch_id", "issued_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_receipt_body_hash"), ["body_hash"], unique=False)
        batch_op.create_index(batch_op.f("ix_receipt_call_id"), ["call_id"], unique=True)
        batch_op.create_index(batch_op.f("ix_receipt_issued_at"), ["issued_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_receipt_network"), ["network"], unique=False)
        batch_op.create_index(batch_op.f("ix_receipt_pay_to"), ["pay_to"], unique=False)
        batch_op.create_index(batch_op.f("ix_receipt_payer"), ["payer"], unique=False)
        batch_op.create_index(batch_op.f("ix_receipt_receipt_id"), ["receipt_id"], unique=True)
        batch_op.create_index(batch_op.f("ix_receipt_session_id"), ["session_id"], unique=False)
        batch_op.create_index(
            "ix_receipt_session_issued", ["session_id", "issued_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_receipt_tx_hash"), ["tx_hash"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_receipt_verify_status"), ["verify_status"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("receipt", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_receipt_verify_status"))
        batch_op.drop_index(batch_op.f("ix_receipt_tx_hash"))
        batch_op.drop_index("ix_receipt_session_issued")
        batch_op.drop_index(batch_op.f("ix_receipt_session_id"))
        batch_op.drop_index(batch_op.f("ix_receipt_receipt_id"))
        batch_op.drop_index(batch_op.f("ix_receipt_payer"))
        batch_op.drop_index(batch_op.f("ix_receipt_pay_to"))
        batch_op.drop_index(batch_op.f("ix_receipt_network"))
        batch_op.drop_index(batch_op.f("ix_receipt_issued_at"))
        batch_op.drop_index(batch_op.f("ix_receipt_call_id"))
        batch_op.drop_index(batch_op.f("ix_receipt_body_hash"))
        batch_op.drop_index("ix_receipt_batch_issued")
        batch_op.drop_index(batch_op.f("ix_receipt_batch_id"))

    op.drop_table("receipt")
    with op.batch_alter_table("call", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_call_tool_id"))
        batch_op.drop_index("ix_call_tool_created")
        batch_op.drop_index("ix_call_status_created")
        batch_op.drop_index(batch_op.f("ix_call_status"))
        batch_op.drop_index(batch_op.f("ix_call_session_id"))
        batch_op.drop_index("ix_call_session_created")
        batch_op.drop_index(batch_op.f("ix_call_payer"))
        batch_op.drop_index(batch_op.f("ix_call_decline_reason"))
        batch_op.drop_index(batch_op.f("ix_call_created_at"))
        batch_op.drop_index(batch_op.f("ix_call_call_id"))
        batch_op.drop_index("ix_call_batch_status")
        batch_op.drop_index(batch_op.f("ix_call_batch_id"))

    op.drop_table("call")
    with op.batch_alter_table("tool", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_tool_scheme"))
        batch_op.drop_index(batch_op.f("ix_tool_network"))
        batch_op.drop_index(batch_op.f("ix_tool_name"))
        batch_op.drop_index(batch_op.f("ix_tool_enabled"))
        batch_op.drop_index(batch_op.f("ix_tool_created_at"))
        batch_op.drop_index(batch_op.f("ix_tool_author_id"))
        batch_op.drop_index("ix_tool_author_enabled")

    op.drop_table("tool")
    with op.batch_alter_table("batch", schema=None) as batch_op:
        batch_op.drop_index("ix_batch_status_opened")
        batch_op.drop_index(batch_op.f("ix_batch_status"))
        batch_op.drop_index(batch_op.f("ix_batch_settled_at"))
        batch_op.drop_index(batch_op.f("ix_batch_settle_tx_hash"))
        batch_op.drop_index("ix_batch_session_status")
        batch_op.drop_index(batch_op.f("ix_batch_session_id"))
        batch_op.drop_index(batch_op.f("ix_batch_pay_to"))
        batch_op.drop_index(batch_op.f("ix_batch_opened_at"))
        batch_op.drop_index(batch_op.f("ix_batch_network"))
        batch_op.drop_index(batch_op.f("ix_batch_claim_tx_hash"))
        batch_op.drop_index(batch_op.f("ix_batch_channel_id"))
        batch_op.drop_index(batch_op.f("ix_batch_batch_id"))

    op.drop_table("batch")
    with op.batch_alter_table("pay_session", schema=None) as batch_op:
        batch_op.drop_index("ix_session_status_opened")
        batch_op.drop_index("ix_session_payer_status")
        batch_op.drop_index(batch_op.f("ix_pay_session_status"))
        batch_op.drop_index(batch_op.f("ix_pay_session_settlement_mode"))
        batch_op.drop_index(batch_op.f("ix_pay_session_session_id"))
        batch_op.drop_index(batch_op.f("ix_pay_session_payer"))
        batch_op.drop_index(batch_op.f("ix_pay_session_opened_at"))
        batch_op.drop_index(batch_op.f("ix_pay_session_network"))
        batch_op.drop_index(batch_op.f("ix_pay_session_last_call_at"))
        batch_op.drop_index(batch_op.f("ix_pay_session_channel_id"))
        batch_op.drop_index(batch_op.f("ix_pay_session_agent_identity"))

    op.drop_table("pay_session")
    with op.batch_alter_table("author", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_author_slug"))
        batch_op.drop_index(batch_op.f("ix_author_pay_to"))
        batch_op.drop_index(batch_op.f("ix_author_is_active"))
        batch_op.drop_index(batch_op.f("ix_author_created_at"))

    op.drop_table("author")
