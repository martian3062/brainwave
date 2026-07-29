"""The revenue ledger.

Six tables: Author -> Tool -> Call, grouped by Session, evidenced by Receipt,
settled by Batch. Every one of them exists to make one claim checkable from the
database alone:

    sum(Call.captured_atomic) for a session
      == the Batch.gross_atomic that was settled on-chain
      == what the tx hash actually moved.

Design rules, enforced not just stated:

* **Money is BIGINT atomic units.** No Float column exists in this file. USDC has
  6 decimals, so $0.002 is the integer 2000. `app.money` is the only converter.
  BigInteger is explicit because SQLModel maps a bare `int` to a 32-bit INTEGER
  on Postgres, which overflows at ~2147 USDC.
* **Capture never exceeds authorization.** A CHECK constraint on `call`, so an
  `upto` capture bug is a database error, not a silent overcharge.
* **Indexes follow the dashboard.** Author revenue, session drill-down, batch
  reconciliation and the live call feed each have a covering index.
* **Demo data is labelled in the schema, not in a convention.** Every table
  carries `is_demo`. `app.cli.seed_demo` can only write rows with it set, the
  admin lists it as a column, and `app.demo` gives the dashboard a one-call
  banner. Fabricated revenue that can pass for real revenue is the one bug in a
  payments demo that is not recoverable, so the flag is a column with an index
  rather than a naming convention on `session_id`.

Naming note: the `Session` model collides with `sqlmodel.Session`. The class is
named `Session` as designed, and `PaySession` is exported as an unambiguous
alias -- prefer `PaySession` in modules that also open DB sessions.
"""

# NOTE: no `from __future__ import annotations` here, deliberately.
# It stringifies every annotation, and SQLAlchemy then cannot resolve
# `list["Tool"]` on a Relationship -- it raises
# "seems to be using a generic class as the argument to relationship()".
# Every other module in the package does use the future import; this one cannot.
from datetime import UTC, datetime
from enum import StrEnum
from typing import Optional

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy import false as sa_false
from sqlmodel import Field, Relationship, SQLModel

__all__ = [
    "Author",
    "Tool",
    "Session",
    "PaySession",
    "Call",
    "Receipt",
    "Batch",
    "ChannelState",
    "Scheme",
    "SessionStatus",
    "CallStatus",
    "BatchStatus",
    "SettlementMode",
    "utcnow",
]


def utcnow() -> datetime:
    """Timezone-aware UTC. Never `datetime.utcnow()` -- that returns a naive
    value that silently claims to be local time."""
    return datetime.now(UTC)


#: Every timestamp column uses this. On Postgres (the ledger of record) it is
#: TIMESTAMPTZ, so a receipt's issuedAt is unambiguous across regions. SQLite,
#: the local fallback, has no tz-aware storage and hands the value back naive --
#: acceptable for development, never for reconciliation.
TS = DateTime(timezone=True)


# --------------------------------------------------------------------------
# Enumerations.
#
# Every enum field below carries an explicit `sa_type=ENUM_COL`. Without it
# SQLModel emits `sa.Enum(...)`, which does two unwanted things: on Postgres it
# creates a NATIVE ENUM TYPE (adding a value later needs ALTER TYPE, removing
# one is a table rebuild), and it stores the member NAME, so the ledger would
# hold 'BATCH_SETTLEMENT' where the x402 wire format says 'batch-settlement'.
# Verified by inspecting the generated migration. VARCHAR(32) stores the value,
# matches the protocol, and leaves headroom for statuses added later.
# --------------------------------------------------------------------------

#: Storage type for every StrEnum column.
ENUM_COL = String(32)


class Scheme(StrEnum):
    """x402 payment schemes. These are the real module names under
    `x402.mechanisms.evm`, not invented labels."""

    EXACT = "exact"
    UPTO = "upto"
    BATCH_SETTLEMENT = "batch-settlement"


class SettlementMode(StrEnum):
    #: One on-chain settlement per call. The naive design; kept so the dashboard
    #: can price it against batching.
    PER_CALL = "per_call"
    #: N authorizations, one settlement. The product.
    BATCHED = "batched"


class SessionStatus(StrEnum):
    OPEN = "open"
    #: Budget exhausted or Guardian tripped; no further calls, but whatever was
    #: consumed still settles honestly.
    FROZEN = "frozen"
    CLOSING = "closing"
    SETTLED = "settled"
    EXPIRED = "expired"


class CallStatus(StrEnum):
    #: 402 issued, nothing signed yet.
    CHALLENGED = "challenged"
    #: Facilitator said the authorization is valid. Nothing has executed.
    VERIFIED = "verified"
    #: Tool code ran. Capture amount is now known (matters for `upto`).
    EXECUTED = "executed"
    #: Captured into the session's running total, awaiting batch close.
    CAPTURED = "captured"
    #: Covered by an on-chain settlement.
    SETTLED = "settled"
    #: Guardian said no. Pre-signature -- there is no authorization artefact.
    DECLINED = "declined"
    FAILED = "failed"


class BatchStatus(StrEnum):
    OPEN = "open"
    CLAIMING = "claiming"
    CLAIMED = "claimed"
    SETTLING = "settling"
    SETTLED = "settled"
    FAILED = "failed"


# --------------------------------------------------------------------------
# Author
# --------------------------------------------------------------------------


class Author(SQLModel, table=True):
    """An MCP tool author. The customer who currently has no way to charge."""

    __tablename__ = "author"

    id: int | None = Field(default=None, primary_key=True)
    slug: str = Field(index=True, unique=True, max_length=64)
    display_name: str = Field(max_length=128)

    #: Where this author's revenue lands. Overrides settings.pay_to_address.
    pay_to: str = Field(index=True, max_length=64)
    contact_email: str | None = Field(default=None, max_length=254)

    #: Per-author override of the platform take, in basis points. None = use the
    #: global default, so changing platform economics does not require a
    #: backfill.
    platform_take_bps: int | None = Field(default=None, ge=0, le=10_000)

    is_active: bool = Field(default=True, index=True)

    #: Seeded by `app.cli.seed_demo`, not earned. Indexed because every read that
    #: shows money either filters on it or labels it; see `app.demo`.
    #:
    #: `server_default` is declared on the MODEL, not only in the migration.
    #: Migration 0002 has to add a NOT NULL column to tables that already have
    #: rows, which Postgres refuses without a default -- and `alembic/env.py`
    #: runs with `compare_server_default=True`, so a default that exists in the
    #: database but not in the model shows up as permanent schema drift. It
    #: belongs in both places. It also means a row inserted by raw SQL is `real`
    #: unless it says otherwise, which is the safe direction to fail.
    is_demo: bool = Field(
        default=False, index=True, sa_column_kwargs={"server_default": sa_false()}
    )

    created_at: datetime = Field(sa_type=TS, default_factory=utcnow, index=True)

    tools: list["Tool"] = Relationship(back_populates="author")


# --------------------------------------------------------------------------
# Tool
# --------------------------------------------------------------------------


class Tool(SQLModel, table=True):
    """A priced MCP tool in the catalogue.

    The price lives here rather than in the decorator so an author can reprice
    without a redeploy, and so the dashboard's revenue figures join back to the
    exact price that was in force.
    """

    __tablename__ = "tool"
    __table_args__ = (
        CheckConstraint(
            "max_price_atomic IS NULL OR max_price_atomic >= price_atomic",
            name="ck_tool_ceiling_ge_price",
        ),
        Index("ix_tool_author_enabled", "author_id", "enabled"),
    )

    id: int | None = Field(default=None, primary_key=True)
    author_id: int = Field(foreign_key="author.id", index=True)

    #: The MCP tool name an agent calls.
    name: str = Field(index=True, unique=True, max_length=128)
    #: The x402 resource identifier, e.g. mcp://tool/run_injection_attack_sim.
    resource_url: str = Field(max_length=512)
    description: str | None = Field(default=None, max_length=1024)
    #: Bazaar discovery tags, comma-separated.
    tags: str | None = Field(default=None, max_length=256)

    scheme: Scheme = Field(sa_type=ENUM_COL, default=Scheme.EXACT, index=True)
    #: CAIP-2, e.g. eip155:84532.
    network: str = Field(index=True, max_length=64)
    asset: str = Field(max_length=64)
    asset_decimals: int = Field(default=6)

    #: Base price, atomic units.
    price_atomic: int = Field(sa_type=BigInteger, default=0)
    #: Ceiling for `upto`. NULL for `exact`. An LLM-backed tool does not know
    #: its cost until it has run; the agent authorizes this and only actual
    #: consumption is captured.
    max_price_atomic: int | None = Field(sa_type=BigInteger, default=None)
    #: What drives capture under `upto`: "tokens" | "bytes" | "seconds" | "calls".
    meter: str | None = Field(default=None, max_length=32)
    #: Atomic units charged per metered unit.
    price_per_unit_atomic: int | None = Field(sa_type=BigInteger, default=None)

    max_timeout_seconds: int = Field(default=300)
    enabled: bool = Field(default=True, index=True)
    #: Seeded, not real. See `app.demo`.
    is_demo: bool = Field(
        default=False, index=True, sa_column_kwargs={"server_default": sa_false()}
    )

    #: Denormalised counters. The dashboard's catalogue view reads these instead
    #: of aggregating `call` on every render.
    total_calls: int = Field(default=0)
    total_captured_atomic: int = Field(sa_type=BigInteger, default=0)

    created_at: datetime = Field(sa_type=TS, default_factory=utcnow, index=True)
    updated_at: datetime = Field(sa_type=TS, default_factory=utcnow)

    author: Optional["Author"] = Relationship(back_populates="tools")
    calls: list["Call"] = Relationship(back_populates="tool")


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------


class Session(SQLModel, table=True):
    """One agent's metered run against the gateway.

    This is the unit the economic argument is made in. A session accumulates N
    authorizations and closes into ONE on-chain settlement, which is what drops
    the fee load from 50% to ~0.5% at micro prices.

    Under the `batch-settlement` scheme the accumulator is a payment *channel*
    with a cumulative voucher, not a pile of independent authorizations -- so
    `authorized_atomic` is the latest voucher ceiling (monotonically
    increasing), not a sum. See `x402.mechanisms.evm.batch_settlement.types`.
    """

    __tablename__ = "pay_session"
    __table_args__ = (
        CheckConstraint("captured_atomic >= 0", name="ck_session_captured_nonneg"),
        CheckConstraint("settled_atomic <= captured_atomic", name="ck_session_settled_le_captured"),
        Index("ix_session_payer_status", "payer", "status"),
        Index("ix_session_status_opened", "status", "opened_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    #: Public identifier carried in every receipt: "sess_01HZY...".
    session_id: str = Field(index=True, unique=True, max_length=64)

    #: The paying agent's address.
    payer: str = Field(index=True, max_length=64)
    #: Human label for the dashboard ("claude-desktop", "langchain-quant").
    agent_label: str | None = Field(default=None, max_length=128)
    #: ERC-8004 agent identity, when the caller presents one.
    agent_identity: str | None = Field(default=None, index=True, max_length=128)

    network: str = Field(index=True, max_length=64)
    asset: str = Field(max_length=64)
    asset_decimals: int = Field(default=6)

    scheme: Scheme = Field(sa_type=ENUM_COL, default=Scheme.EXACT)
    settlement_mode: SettlementMode = Field(
        sa_type=ENUM_COL, default=SettlementMode.BATCHED, index=True
    )
    status: SessionStatus = Field(sa_type=ENUM_COL, default=SessionStatus.OPEN, index=True)

    #: batch-settlement channelId (EIP-712 hash of the ChannelConfig).
    channel_id: str | None = Field(default=None, index=True, max_length=80)

    #: Guardian's hard cap for this session, atomic units. NULL = unbounded,
    #: which no operator should ever ship.
    budget_atomic: int | None = Field(sa_type=BigInteger, default=None)
    #: Cumulative ceiling the agent has authorized (voucher maxClaimableAmount).
    authorized_atomic: int = Field(sa_type=BigInteger, default=0)
    #: Sum of actual capture across this session's calls.
    captured_atomic: int = Field(sa_type=BigInteger, default=0)
    #: How much has actually landed on-chain. Lags `captured` until batch close;
    #: the gap is the float the batching earns its keep on.
    settled_atomic: int = Field(sa_type=BigInteger, default=0)

    call_count: int = Field(default=0)
    declined_count: int = Field(default=0)
    #: Seeded, not real. See `app.demo`.
    is_demo: bool = Field(
        default=False, index=True, sa_column_kwargs={"server_default": sa_false()}
    )

    opened_at: datetime = Field(sa_type=TS, default_factory=utcnow, index=True)
    last_call_at: datetime | None = Field(sa_type=TS, default=None, index=True)
    closed_at: datetime | None = Field(sa_type=TS, default=None)

    calls: list["Call"] = Relationship(back_populates="session")
    batches: list["Batch"] = Relationship(back_populates="session")


#: Unambiguous alias -- `from app.models import PaySession` will not shadow
#: `sqlmodel.Session` in modules that do both.
PaySession = Session


# --------------------------------------------------------------------------
# Call
# --------------------------------------------------------------------------


class Call(SQLModel, table=True):
    """One metered tool invocation. The atom of the ledger.

    A row exists from the moment a 402 is issued, so declines and unpaid probes
    are visible too -- the dashboard needs the conversion funnel, not just the
    wins.
    """

    __tablename__ = "call"
    __table_args__ = (
        # The property the whole `upto` scheme depends on. Enforced in the
        # database so a metering bug cannot overcharge, only error.
        CheckConstraint(
            "captured_atomic <= authorized_atomic", name="ck_call_capture_le_authorized"
        ),
        CheckConstraint("captured_atomic >= 0", name="ck_call_captured_nonneg"),
        CheckConstraint(
            "platform_fee_atomic + author_net_atomic = captured_atomic",
            name="ck_call_split_conserves",
        ),
        Index("ix_call_session_created", "session_id", "created_at"),
        Index("ix_call_tool_created", "tool_id", "created_at"),
        Index("ix_call_status_created", "status", "created_at"),
        Index("ix_call_batch_status", "batch_id", "status"),
        # Replay defence: an authorization nonce may be used once per network.
        UniqueConstraint("network", "nonce", name="uq_call_network_nonce"),
    )

    id: int | None = Field(default=None, primary_key=True)
    call_id: str = Field(index=True, unique=True, max_length=64)

    session_id: int = Field(foreign_key="pay_session.id", index=True)
    tool_id: int = Field(foreign_key="tool.id", index=True)
    batch_id: int | None = Field(default=None, foreign_key="batch.id", index=True)

    payer: str = Field(index=True, max_length=64)
    pay_to: str = Field(max_length=64)
    network: str = Field(max_length=64)
    asset: str = Field(max_length=64)
    scheme: Scheme = Field(sa_type=ENUM_COL, default=Scheme.EXACT)

    #: What the agent authorized for this call (the `upto` ceiling, or the exact
    #: price).
    authorized_atomic: int = Field(sa_type=BigInteger, default=0)
    #: What actually got charged after the tool ran.
    captured_atomic: int = Field(sa_type=BigInteger, default=0)
    #: Split of `captured_atomic`; the CHECK above keeps them conserving.
    platform_fee_atomic: int = Field(sa_type=BigInteger, default=0)
    author_net_atomic: int = Field(sa_type=BigInteger, default=0)

    #: Metering evidence: how many units the tool actually consumed.
    meter: str | None = Field(default=None, max_length=32)
    meter_units: int | None = Field(sa_type=BigInteger, default=None)

    status: CallStatus = Field(sa_type=ENUM_COL, default=CallStatus.CHALLENGED, index=True)
    #: Set when the buyer-side Guardian refuses: "over_session_budget",
    #: "per_call_max", "not_allowlisted", "needs_escalation", "no_receipt".
    decline_reason: str | None = Field(default=None, index=True, max_length=64)
    error: str | None = Field(default=None, max_length=512)

    #: The authorization nonce, for replay detection.
    nonce: str | None = Field(default=None, max_length=80)
    #: Seeded, not real. See `app.demo`.
    is_demo: bool = Field(
        default=False, index=True, sa_column_kwargs={"server_default": sa_false()}
    )

    verify_ms: int | None = Field(default=None)
    execute_ms: int | None = Field(default=None)
    settle_ms: int | None = Field(default=None)

    created_at: datetime = Field(sa_type=TS, default_factory=utcnow, index=True)
    executed_at: datetime | None = Field(sa_type=TS, default=None)
    settled_at: datetime | None = Field(sa_type=TS, default=None)

    session: Optional["Session"] = Relationship(back_populates="calls")
    tool: Optional["Tool"] = Relationship(back_populates="calls")
    batch: Optional["Batch"] = Relationship(back_populates="calls")
    receipt: Optional["Receipt"] = Relationship(back_populates="call")


# --------------------------------------------------------------------------
# Batch
# --------------------------------------------------------------------------


class Batch(SQLModel, table=True):
    """N authorizations, one on-chain settlement. The economics live here.

    `batch-settlement` in the x402 SDK is a two-step on-chain flow, not one: a
    `claim` (submit the cumulative vouchers) and a `settle` (sweep the claimed
    funds to the receiver). Both hashes are stored because reconciliation needs
    both -- a batch whose claim landed but whose sweep did not is a real state.
    """

    __tablename__ = "batch"
    __table_args__ = (
        CheckConstraint(
            "platform_fee_atomic + author_net_atomic = gross_atomic",
            name="ck_batch_split_conserves",
        ),
        CheckConstraint("gross_atomic >= 0", name="ck_batch_gross_nonneg"),
        Index("ix_batch_status_opened", "status", "opened_at"),
        Index("ix_batch_session_status", "session_id", "status"),
    )

    id: int | None = Field(default=None, primary_key=True)
    batch_id: str = Field(index=True, unique=True, max_length=64)

    session_id: int = Field(foreign_key="pay_session.id", index=True)
    #: batch-settlement channel this batch claims against.
    channel_id: str | None = Field(default=None, index=True, max_length=80)

    network: str = Field(index=True, max_length=64)
    asset: str = Field(max_length=64)
    asset_decimals: int = Field(default=6)
    pay_to: str = Field(index=True, max_length=64)

    call_count: int = Field(default=0)
    #: Sum of captured across the calls in this batch. Must equal the on-chain
    #: transfer -- that equality is the audit.
    gross_atomic: int = Field(sa_type=BigInteger, default=0)
    platform_fee_atomic: int = Field(sa_type=BigInteger, default=0)
    author_net_atomic: int = Field(sa_type=BigInteger, default=0)
    #: What the facilitator charged for this one settlement. Divided by
    #: `gross_atomic` this is the fee load -- the number the submission argues
    #: about.
    facilitator_fee_atomic: int = Field(sa_type=BigInteger, default=0)

    status: BatchStatus = Field(sa_type=ENUM_COL, default=BatchStatus.OPEN, index=True)
    #: Step 1: vouchers claimed on-chain.
    claim_tx_hash: str | None = Field(default=None, index=True, max_length=80)
    #: Step 2: claimed funds swept to the receiver.
    settle_tx_hash: str | None = Field(default=None, index=True, max_length=80)
    explorer_url: str | None = Field(default=None, max_length=256)
    error: str | None = Field(default=None, max_length=512)
    #: Seeded, not real -- a demo batch's tx hashes are NOT on any chain.
    is_demo: bool = Field(
        default=False, index=True, sa_column_kwargs={"server_default": sa_false()}
    )

    opened_at: datetime = Field(sa_type=TS, default_factory=utcnow, index=True)
    closed_at: datetime | None = Field(sa_type=TS, default=None)
    settled_at: datetime | None = Field(sa_type=TS, default=None, index=True)

    session: Optional["Session"] = Relationship(back_populates="batches")
    calls: list["Call"] = Relationship(back_populates="batch")
    receipts: list["Receipt"] = Relationship(back_populates="batch")


# --------------------------------------------------------------------------
# Receipt
# --------------------------------------------------------------------------


class Receipt(SQLModel, table=True):
    """The proof handed back inside the tool response.

    Three things make a receipt useful rather than ornamental, and each has a
    column:

    * `attestation` -- the facilitator's signature over the body, so anyone can
      verify it without trusting this gateway.
    * `session_id` + `batch_id` -- so an author can tie any one call to the
      on-chain settlement that paid for it.
    * `authorized_atomic` vs `captured_atomic` -- under `upto` these differ, and
      showing both is the difference between a payment system and a black box.

    `body_hash` is ours: a digest of the canonical receipt JSON, so a tampered
    receipt fails locally before anyone calls the facilitator.
    """

    __tablename__ = "receipt"
    __table_args__ = (
        CheckConstraint(
            "captured_atomic <= authorized_atomic", name="ck_receipt_capture_le_authorized"
        ),
        Index("ix_receipt_session_issued", "session_id", "issued_at"),
        Index("ix_receipt_batch_issued", "batch_id", "issued_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    receipt_id: str = Field(index=True, unique=True, max_length=64)

    #: One receipt per call.
    call_id: int = Field(foreign_key="call.id", index=True, unique=True)
    session_id: int = Field(foreign_key="pay_session.id", index=True)
    batch_id: int | None = Field(default=None, foreign_key="batch.id", index=True)

    scheme: Scheme = Field(sa_type=ENUM_COL, default=Scheme.EXACT)
    network: str = Field(index=True, max_length=64)
    asset: str = Field(max_length=64)
    asset_decimals: int = Field(default=6)

    authorized_atomic: int = Field(sa_type=BigInteger, default=0)
    captured_atomic: int = Field(sa_type=BigInteger, default=0)

    payer: str = Field(index=True, max_length=64)
    pay_to: str = Field(index=True, max_length=64)
    resource_url: str = Field(max_length=512)

    #: "immediate" (per-call settlement) or "batched" (settles at window close).
    settlement: SettlementMode = Field(sa_type=ENUM_COL, default=SettlementMode.BATCHED)
    tx_hash: str | None = Field(default=None, index=True, max_length=80)
    explorer_url: str | None = Field(default=None, max_length=256)

    facilitator: str = Field(default="", max_length=64)
    #: Facilitator signature over the receipt body. Independently checkable.
    attestation: str | None = Field(default=None, max_length=256)
    #: sha256 of the canonical body -- local tamper detection.
    body_hash: str = Field(default="", index=True, max_length=80)
    #: The exact JSON that was returned to the agent, verbatim.
    body_json: str = Field(default="{}")
    #: Seeded, not real. A demo receipt is not evidence of anything; the body
    #: itself also carries `"isDemo": true` so a receipt copied out of the
    #: database still says so.
    is_demo: bool = Field(
        default=False, index=True, sa_column_kwargs={"server_default": sa_false()}
    )

    issued_at: datetime = Field(sa_type=TS, default_factory=utcnow, index=True)
    #: Populated by the free GET /receipts/{id}/verify endpoint. Verification is
    #: never paywalled.
    verified_at: datetime | None = Field(sa_type=TS, default=None)
    verify_status: str | None = Field(default=None, index=True, max_length=32)

    call: Optional["Call"] = Relationship(back_populates="receipt")
    batch: Optional["Batch"] = Relationship(back_populates="receipts")


# --------------------------------------------------------------------------
# Encrypted batch-settlement channel storage
# --------------------------------------------------------------------------


class ChannelState(SQLModel, table=True):
    """Durable SDK channel state, encrypted outside the reporting ledger.

    A channel record contains a signed cumulative voucher and is therefore
    spend-capable material. It must survive a web-process restart so a separate
    close command can claim it, but it must not be readable through the ledger
    dashboard or SQLAdmin. `app.channels` encrypts `payload_encrypted` with a
    key derived from STORAGE_SECRET before this model ever sees it.
    """

    __tablename__ = "channel_state"
    __table_args__ = (Index("ix_channel_state_updated_at", "updated_at"),)

    channel_id: str = Field(primary_key=True, max_length=80)
    payload_encrypted: str = Field(sa_type=Text)
    created_at: datetime = Field(sa_type=TS, default_factory=utcnow)
    updated_at: datetime = Field(sa_type=TS, default_factory=utcnow)
