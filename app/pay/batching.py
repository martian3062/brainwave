"""Session windows and batch close, built ON the SDK's batch-settlement mechanism.

READ THIS BEFORE CHANGING ANYTHING HERE
=======================================
There is already a batching mechanism in the x402 SDK and this module does not
replace it, wrap it in a second one, or reimplement any part of it. The relevant
facts, established by reading `x402/mechanisms/evm/batch_settlement/` rather than
any README:

* `batch-settlement` is NOT "add up N authorizations and settle the sum". It is a
  payment CHANNEL plus a monotonic cumulative VOUCHER:

      deposit   ERC-3009 `receiveWithAuthorization` or Permit2, opens the channel
      voucher   every subsequent request raises `maxClaimableAmount`, signed EIP-712
      claim     the server submits a batch of `VoucherClaim` on-chain
      settle    the claimed funds are swept to the receiver

  Consequently `Session.authorized_atomic` is a CEILING that only ever rises, not a
  running total, and `Batch` carries `claim_tx_hash` AND `settle_tx_hash` because
  a batch whose claim landed but whose sweep did not is a real, recoverable state.

* THE PER-CALL SETTLE DOES NOT TOUCH THE CHAIN. This is the single most important
  verified fact in the project and it is what the economic argument rests on.
  `batch_settlement/server/settle.py::handle_before_settle` intercepts every
  voucher payload, increments the channel's local `charged_cumulative_amount`, and
  returns `SkipSettleResult(SettleResponse(success=True, transaction="", ...))`.
  So `x402.mcp.create_payment_wrapper` calling `settle_payment` on every single
  tool call costs nothing on-chain -- an empty `transaction` field is the tell.
  The chain is touched exactly twice per batch, by `BatchSettlementChannelManager`.

* The channel manager (`scheme.create_channel_manager(facilitator, network)`)
  already implements claim, settle, refund, idle-channel selection and a periodic
  auto-settlement loop. `ChannelManagerDriver` below CALLS it. We contribute the
  ledger: which `Call` rows a `Batch` contains, what it grossed, how it splits
  between platform and author, and which transaction paid for it.

WHAT WE ADD, PRECISELY
----------------------
The SDK's channel storage knows `charged_cumulative_amount` per channel. It does
not know which tool calls produced it, who the author was, what the platform took,
or how to hand a buyer a receipt that reconciles to a transaction. That is this
module plus `receipts.py`, and it is the only thing here that did not already exist.

NOTHING IN THIS MODULE SPENDS. `close_window` is pure bookkeeping. `settle_batch`
delegates to a driver, and the only driver that reaches a chain is
`ChannelManagerDriver`, which must be constructed explicitly with a live channel
manager. Absent a driver a batch simply stays closed and unsettled, which is an
honest state the dashboard displays rather than a hidden failure.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from sqlmodel import Session as DBSession
from sqlmodel import select

from app.config import settings
from app.db import session_scope
from app.models import (
    Batch,
    BatchStatus,
    Call,
    CallStatus,
    PaySession,
    Receipt,
    Scheme,
    SessionStatus,
    SettlementMode,
    Tool,
    utcnow,
)
from app.money import split_take
from app.pay import receipts as receipts_mod
from app.pay.economics import batch_conservation

log = logging.getLogger(__name__)

__all__ = [
    "LEDGER_LOCK",
    "ledger_write",
    "open_or_reuse_session",
    "note_channel",
    "WindowDecision",
    "should_close",
    "close_window",
    "SettlementDriver",
    "ChannelManagerDriver",
    "SettlementOutcome",
    "settle_batch",
    "close_and_settle",
]


#: Serialises ledger writes across the worker threads `@paid` dispatches DB work
#: to. Reentrant, so a write that calls another write does not deadlock.
#:
#: This is not belt-and-braces, it fixes two real races:
#:
#: 1. `open_or_reuse_session` is a SELECT followed by an INSERT with no uniqueness
#:    constraint behind it, so two concurrent first-calls from the same payer would
#:    open two sessions and split one agent's budget in half. That happens on
#:    Postgres too, not just on the SQLite fallback.
#: 2. The local SQLite fallback runs on a single shared connection (StaticPool, so
#:    an in-memory database is visible to every thread). `sqlite3` connections are
#:    not safe for concurrent use, and concurrent tool calls produce
#:    "bad parameter or other API misuse" -- found by
#:    tests/test_pay_flow.py::test_concurrent_calls_do_not_cross_contaminate_their_meters.
#:
#: The throughput cost is close to nothing: a ledger write is a handful of indexed
#: statements, while the facilitator round trip it sits next to is a network call
#: and stays fully concurrent.
LEDGER_LOCK = threading.RLock()


@contextmanager
def ledger_write() -> Iterator[DBSession]:
    """A transactional DB session that no other ledger writer holds concurrently."""
    with LEDGER_LOCK:
        with session_scope() as db:
            yield db


def _session_id() -> str:
    return f"sess_{uuid.uuid4().hex[:24]}"


def _batch_id() -> str:
    return f"batch_{uuid.uuid4().hex[:20]}"


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


def open_or_reuse_session(
    db: DBSession,
    *,
    payer: str,
    network: str,
    asset: str,
    scheme: Scheme,
    asset_decimals: int = 6,
    agent_label: str | None = None,
    agent_identity: str | None = None,
    budget_atomic: int | None = None,
    settlement_mode: SettlementMode | None = None,
) -> PaySession:
    """Find this payer's open session, or start one.

    Keyed on `(payer, network, asset, status=OPEN)` rather than on anything the
    client sends, because a session is a BILLING window and letting a caller pick
    its own would let it dodge the budget by opening a fresh one. A frozen session
    is deliberately not reused: freezing is what the Guardian does when a budget is
    exhausted, and quietly handing back a working session would undo it.
    """
    # Only the SDK's batch-settlement scheme yields a durable cumulative
    # voucher. Exact and upto must settle immediately; marking either as batched
    # would extend unsecured credit and let a short-lived authorization expire.
    mode = settlement_mode or (
        SettlementMode.BATCHED if scheme == Scheme.BATCH_SETTLEMENT else SettlementMode.PER_CALL
    )
    existing = db.exec(
        select(PaySession)
        .where(PaySession.payer == payer)
        .where(PaySession.network == network)
        .where(PaySession.asset == asset)
        .where(PaySession.status == SessionStatus.OPEN)
        .order_by(PaySession.opened_at.desc())  # type: ignore[union-attr]
    ).first()
    if existing is not None:
        return existing

    session = PaySession(
        session_id=_session_id(),
        payer=payer,
        agent_label=agent_label,
        agent_identity=agent_identity,
        network=network,
        asset=asset,
        asset_decimals=asset_decimals,
        scheme=scheme,
        settlement_mode=mode,
        status=SessionStatus.OPEN,
        budget_atomic=budget_atomic
        if budget_atomic is not None
        else (settings.session_budget_atomic or None),
    )
    db.add(session)
    db.flush()
    log.info("opened payment session %s for %s", session.session_id, payer)
    return session


def note_channel(db: DBSession, session: PaySession, payload: Any) -> str | None:
    """Record the batch-settlement channel a payload belongs to, if it has one.

    The channelId is recomputed from the `channelConfig` with the SDK's own
    `compute_channel_id` and compared against the one the client sent. They must
    match: the channel id is an EIP-712 hash of the config bound to the chain, so a
    mismatch means the payload's config and its claimed channel disagree -- which
    is either a bug or an attempt to have vouchers claimed against a channel the
    payer did not fund.
    """
    raw = getattr(payload, "payload", None)
    if not isinstance(raw, dict):
        return None
    try:
        from x402.mechanisms.evm.batch_settlement.types import (
            ChannelConfig,
            is_deposit_payload,
            is_voucher_payload,
        )
        from x402.mechanisms.evm.batch_settlement.utils import compute_channel_id
    except Exception:  # noqa: BLE001 -- optional evm extra
        return None

    if not (is_voucher_payload(raw) or is_deposit_payload(raw)):
        return None

    claimed = raw["voucher"]["channelId"]
    computed = compute_channel_id(ChannelConfig.from_dict(raw["channelConfig"]), session.network)
    if str(claimed).lower() != str(computed).lower():
        raise ValueError(
            f"channelId mismatch: payload claims {claimed}, channelConfig hashes to {computed}"
        )

    if session.channel_id != claimed:
        session.channel_id = claimed
        db.add(session)
        db.flush()

    # The voucher ceiling is cumulative and monotonic; store the highest seen.
    ceiling = int(raw["voucher"]["maxClaimableAmount"])
    if ceiling > session.authorized_atomic:
        session.authorized_atomic = ceiling
        db.add(session)
        db.flush()
    return claimed


# --------------------------------------------------------------------------
# Window policy
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowDecision:
    close: bool
    reason: str

    def __bool__(self) -> bool:
        return self.close


def should_close(session: PaySession, pending: list[Call], *, now: Any = None) -> WindowDecision:
    """Is this session's batch window finished?

    Four triggers, in priority order:

      budget      the session hit its cap; settle what was consumed and stop
      max_calls   BATCH_MAX_CALLS -- a `ClaimBatch` is one typed-data structure and
                  the SDK's own default is 100 claims per batch
      window      BATCH_WINDOW_SECONDS since the batch's first call
      dust        below BATCH_MIN_GROSS_PRICE the settlement costs more than it
                  collects, so the window stays open and the gross rolls forward

    The dust rule is checked LAST and only vetoes a time-based close: a full or
    budget-exhausted window closes even if it is small, because holding it open
    forever would be worse than an unprofitable settlement.
    """
    now = now or utcnow()
    if not pending:
        return WindowDecision(False, "nothing captured")

    gross = sum(int(c.captured_atomic) for c in pending)

    if session.budget_atomic is not None and session.captured_atomic >= session.budget_atomic:
        return WindowDecision(True, "session budget reached")

    if len(pending) >= settings.batch_max_calls:
        return WindowDecision(True, f"batch full ({len(pending)} calls)")

    if session.status in (SessionStatus.FROZEN, SessionStatus.CLOSING):
        return WindowDecision(True, f"session {session.status}")

    oldest = min((c.created_at for c in pending), default=None)
    if oldest is not None:
        # SQLite hands back naive datetimes (no TIMESTAMPTZ); compare like with like.
        reference = now if oldest.tzinfo else now.replace(tzinfo=None)
        if reference - oldest >= timedelta(seconds=settings.batch_window_seconds):
            if gross < settings.batch_min_gross_atomic:
                return WindowDecision(
                    False,
                    f"window elapsed but gross {gross} is below the "
                    f"{settings.batch_min_gross_atomic} dust floor; rolling forward",
                )
            return WindowDecision(True, f"window elapsed ({settings.batch_window_seconds}s)")

    return WindowDecision(False, f"{len(pending)} calls, {gross} atomic units, window open")


def _pending_calls(db: DBSession, session: PaySession) -> list[Call]:
    """Captured calls in this session that no batch has claimed yet."""
    return list(
        db.exec(
            select(Call)
            .where(Call.session_id == session.id)
            .where(Call.status == CallStatus.CAPTURED)
            .where(Call.batch_id.is_(None))  # type: ignore[union-attr]
            .order_by(Call.created_at)  # type: ignore[arg-type]
        )
    )


# --------------------------------------------------------------------------
# Close
# --------------------------------------------------------------------------


def close_window(db: DBSession, session: PaySession, *, force: bool = False) -> Batch | None:
    """Turn the session's captured-but-unbatched calls into one `Batch`.

    Pure bookkeeping: no chain, no facilitator, no spending. Returns None when
    there is nothing to close or the window is not due.

    The conservation check is not decorative. `Batch.gross_atomic` is asserted to
    equal `sum(Call.captured_atomic)` over exactly the rows assigned to the batch,
    with integer equality and no tolerance, before the transaction is allowed to
    commit. Everything the submission claims about the ledger is downstream of
    this one assertion holding.
    """
    pending = _pending_calls(db, session)
    if not pending:
        return None
    if not force:
        decision = should_close(session, pending)
        if not decision.close:
            log.debug("session %s stays open: %s", session.session_id, decision.reason)
            return None

    gross = sum(int(c.captured_atomic) for c in pending)
    platform = sum(int(c.platform_fee_atomic) for c in pending)
    author = sum(int(c.author_net_atomic) for c in pending)
    if platform + author != gross:
        raise AssertionError(
            f"session {session.session_id}: per-call splits sum to {platform + author}, "
            f"gross is {gross}. Refusing to open a batch on inconsistent rows."
        )

    pay_to = pending[0].pay_to
    if any(c.pay_to != pay_to for c in pending):
        # One batch settles to one receiver -- `SettlePayload` carries a single
        # `receiver`. Mixing authors in a batch would pay one of them the others'
        # revenue.
        raise ValueError(
            f"session {session.session_id} has calls for multiple payees; "
            "batches are per-receiver because SettlePayload sweeps to one address"
        )

    batch = Batch(
        batch_id=_batch_id(),
        session_id=session.id,
        channel_id=session.channel_id,
        network=session.network,
        asset=session.asset,
        asset_decimals=session.asset_decimals,
        pay_to=pay_to,
        call_count=len(pending),
        gross_atomic=gross,
        platform_fee_atomic=platform,
        author_net_atomic=author,
        # Two transactions: claim, then settle. Counting one would flatter the
        # fee-load number by a factor of two.
        facilitator_fee_atomic=settings.facilitator_fee_atomic * 2,
        status=BatchStatus.OPEN,
        closed_at=utcnow(),
    )
    db.add(batch)
    db.flush()

    call_pks = [c.id for c in pending]
    for call in pending:
        call.batch_id = batch.id
        db.add(call)
    db.flush()

    # The receipt was issued at capture time, when no batch existed yet. Point it
    # at this one now, so `attach_batch_settlement` can find it when the sweep
    # lands -- otherwise every receipt would stay tx-less forever and the whole
    # session -> batch -> transaction reconciliation would be broken.
    linked = select(Receipt).where(Receipt.call_id.in_(call_pks))  # type: ignore[union-attr]
    for receipt in db.exec(linked):
        receipt.batch_id = batch.id
        db.add(receipt)
    db.flush()

    ok, detail = batch_conservation(batch, pending)
    if not ok:
        raise AssertionError(detail)

    session.status = SessionStatus.CLOSING
    db.add(session)
    db.flush()

    log.info("closed %s: %s", batch.batch_id, detail)
    return batch


# --------------------------------------------------------------------------
# Settlement
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SettlementOutcome:
    """What a driver managed to do on-chain."""

    claim_tx_hash: str | None
    settle_tx_hash: str | None
    vouchers_claimed: int = 0
    error: str | None = None

    @property
    def settled(self) -> bool:
        return bool(self.settle_tx_hash)


class SettlementDriver(Protocol):
    """Whatever actually moves the money. One implementation reaches a chain."""

    async def claim(self, batch: Batch) -> SettlementOutcome: ...

    async def settle(self, batch: Batch, outcome: SettlementOutcome) -> SettlementOutcome: ...


class ChannelManagerDriver:
    """The real one. A thin adapter over `BatchSettlementChannelManager`.

    Construct with a manager from `scheme.create_channel_manager(facilitator, network)`
    -- never with a scheme instance of its own, because the scheme object owns the
    channel storage and a second one would claim against empty state.

    Deliberately NOT constructed anywhere by default. Instantiating this class is
    the explicit act of enabling on-chain settlement; `app.pay` never does it for you.
    """

    def __init__(self, manager: Any, *, max_claims_per_batch: int | None = None) -> None:
        self._manager = manager
        self._max_claims = max_claims_per_batch or min(settings.batch_max_calls, 100)

    async def claim(self, batch: Batch) -> SettlementOutcome:
        from x402.mechanisms.evm.batch_settlement.server.channel_manager_common import ClaimOptions

        try:
            results = await self._manager.claim(ClaimOptions(max_claims_per_batch=self._max_claims))
        except Exception as exc:  # noqa: BLE001
            return SettlementOutcome(None, None, error=f"claim failed: {type(exc).__name__}: {exc}")
        if not results:
            return SettlementOutcome(
                None,
                None,
                error="nothing claimable: the channel's charged total does not exceed "
                "what has already been claimed",
            )
        # `claim()` may split into several transactions; the batch records the
        # last one, and `vouchers_claimed` says how many vouchers were involved so
        # a partial claim is visible rather than implied.
        return SettlementOutcome(
            claim_tx_hash=results[-1].transaction,
            settle_tx_hash=None,
            vouchers_claimed=sum(r.vouchers for r in results),
        )

    async def settle(self, batch: Batch, outcome: SettlementOutcome) -> SettlementOutcome:
        try:
            result = await self._manager.settle()
        except Exception as exc:  # noqa: BLE001
            return SettlementOutcome(
                claim_tx_hash=outcome.claim_tx_hash,
                settle_tx_hash=None,
                vouchers_claimed=outcome.vouchers_claimed,
                error=f"settle failed: {type(exc).__name__}: {exc}",
            )
        return SettlementOutcome(
            claim_tx_hash=outcome.claim_tx_hash,
            settle_tx_hash=result.transaction,
            vouchers_claimed=outcome.vouchers_claimed,
        )


async def settle_batch(db: DBSession, batch: Batch, driver: SettlementDriver) -> Batch:
    """Claim then sweep, recording every intermediate state.

    The status walk is OPEN -> CLAIMING -> CLAIMED -> SETTLING -> SETTLED, and each
    step is committed before the next is attempted, because the failure mode this
    exists for is "the claim landed and the process died". Recovery reads the
    ledger, sees CLAIMED with a `claim_tx_hash` and no `settle_tx_hash`, and knows
    to sweep rather than re-claim.

    Calls are marked SETTLED and receipts get their transaction hash only after the
    sweep succeeds.
    """
    batch.status = BatchStatus.CLAIMING
    db.add(batch)
    db.commit()

    outcome = await driver.claim(batch)
    if outcome.error or not outcome.claim_tx_hash:
        batch.status = BatchStatus.FAILED
        batch.error = (outcome.error or "claim produced no transaction")[:512]
        db.add(batch)
        db.commit()
        log.error("batch %s claim failed: %s", batch.batch_id, batch.error)
        return batch

    batch.status = BatchStatus.CLAIMED
    batch.claim_tx_hash = outcome.claim_tx_hash
    batch.explorer_url = settings.explorer_url(outcome.claim_tx_hash)
    db.add(batch)
    db.commit()

    batch.status = BatchStatus.SETTLING
    db.add(batch)
    db.commit()

    outcome = await driver.settle(batch, outcome)
    if outcome.error or not outcome.settle_tx_hash:
        # Claimed but not swept. NOT a failure of the claim -- the money is in the
        # settlement contract and a later sweep collects it. Status says CLAIMED so
        # a retry does the right thing.
        batch.status = BatchStatus.CLAIMED
        batch.error = (outcome.error or "settle produced no transaction")[:512]
        db.add(batch)
        db.commit()
        log.error("batch %s claimed but not swept: %s", batch.batch_id, batch.error)
        return batch

    now = utcnow()
    batch.status = BatchStatus.SETTLED
    batch.settle_tx_hash = outcome.settle_tx_hash
    batch.explorer_url = settings.explorer_url(outcome.settle_tx_hash)
    batch.settled_at = now
    batch.error = None
    db.add(batch)

    calls = list(db.exec(select(Call).where(Call.batch_id == batch.id)))
    for call in calls:
        call.status = CallStatus.SETTLED
        call.settled_at = now
        db.add(call)

    ok, detail = batch_conservation(batch, calls)
    if not ok:
        # The transaction already happened; refusing to record it would be worse
        # than recording it loudly. This is an alert, not a rollback.
        log.error("SETTLED BATCH FAILS CONSERVATION: %s", detail)
        batch.error = detail[:512]
        db.add(batch)

    session = db.get(PaySession, batch.session_id)
    if session is not None:
        session.settled_atomic += batch.gross_atomic
        fully_settled = session.settled_atomic >= session.captured_atomic
        session.status = SessionStatus.SETTLED if fully_settled else SessionStatus.OPEN
        if fully_settled:
            session.closed_at = now
        db.add(session)

    receipts_mod.attach_batch_settlement(db, batch)
    db.commit()
    log.info(
        "batch %s settled: %s (%s calls, %s atomic units)",
        batch.batch_id,
        outcome.settle_tx_hash,
        batch.call_count,
        batch.gross_atomic,
    )
    return batch


async def close_and_settle(
    db: DBSession, session: PaySession, driver: SettlementDriver | None, *, force: bool = False
) -> Batch | None:
    """Close the window and, if a driver was supplied, settle it.

    With `driver=None` the batch is closed and left unsettled. That is the default
    everywhere in this codebase: on-chain settlement is opt-in and explicit.
    """
    batch = close_window(db, session, force=force)
    if batch is None:
        return None
    db.commit()
    if driver is None:
        log.info("batch %s closed and awaiting settlement (no driver configured)", batch.batch_id)
        return batch
    return await settle_batch(db, batch, driver)


def apply_take(gross_atomic: int, tool: Tool | None, author: Any | None) -> tuple[int, int]:
    """`(platform_fee, author_net)` for one capture, honouring a per-author override.

    Lives here rather than in `economics.py` because it needs the ledger objects;
    the arithmetic itself is `app.money.split_take` and conserves exactly.
    """
    take = settings.platform_take_bps
    if author is not None and getattr(author, "platform_take_bps", None) is not None:
        take = int(author.platform_take_bps)
    _ = tool  # reserved: per-tool overrides would be read here
    return split_take(gross_atomic, take)
