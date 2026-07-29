"""Close a session batch and record the settlement. DRY RUN unless `--live`.

    python -m app.cli close_batch                       # plan only, writes nothing
    python -m app.cli close_batch --session sess_abc123
    python -m app.cli close_batch --all --force-window  # ignore the batch window

    # a real settlement needs all four, and there is no shorter form:
    python -m app.cli close_batch --session sess_abc123 \
        --live --yes --confirm-network eip155:84532

WHY THE DEFAULT IS A DRY RUN
----------------------------
This is the only command in the repository that can move money. A default that
spends is a default that spends by accident -- in a demo, in a CI job, in a
shell-history recall at 2am. So the safe mode is the one you get for free and
the dangerous one costs four flags, one of which makes you type the network you
think you are on. `--confirm-network` exists specifically to catch the mistake
where `.env` still says mainnet.

The dry run is not a stub. It selects exactly the calls a live run would settle,
computes exactly the money a live run would move, and prints the exact
`ClaimPayload` / `SettlePayload` a live run would send. It just does not send
them, and it writes nothing to the ledger.

WHAT `--live` ACTUALLY NEEDS, AND WHY IT REFUSES WITHOUT IT
-----------------------------------------------------------
Settling `batch-settlement` means submitting the payer's SIGNED cumulative
voucher. The ledger does not hold that signature -- deliberately. `Call.nonce`
and `Session.channel_id` are reconciliation keys, not payment artefacts; storing
signed authorizations in the reporting tables would put spendable material in
the table the dashboard reads.

The signed vouchers live in the channel store the gateway's runtime maintains
(the shape is `x402.mechanisms.evm.batch_settlement.server.storage.Channel`).
This command reads them through one seam, `app.channels.claims_for(channel_id)`.
When that module is absent, `--live` refuses and says precisely what is missing
instead of fabricating a payload that would be rejected on-chain -- or worse,
one that would not be.

RECOVERY
--------
Closing is two on-chain steps (`claim`, then `settle`), so "claimed but not
swept" is a real state, and the `Batch` row is written BEFORE the first call to
the facilitator and advanced through `CLAIMING -> CLAIMED -> SETTLING ->
SETTLED`. A crash leaves a row that says exactly how far it got, and `--resume`
picks it up from there. That is why `Batch` has two transaction hash columns.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app.cli._out import Printer
from app.cli._x402 import new_id
from app.config import settings
from app.money import fee_load_bps, format_atomic


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session", default=None, help="close this session id only")
    parser.add_argument("--all", action="store_true", help="consider every eligible open session")
    parser.add_argument(
        "--force-window",
        action="store_true",
        help="ignore BATCH_WINDOW_SECONDS and the dust floor when selecting",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="ACTUALLY SETTLE. Requires --yes and --confirm-network. Spends real funds.",
    )
    parser.add_argument("--yes", action="store_true", help="confirm a --live run")
    parser.add_argument(
        "--confirm-network",
        default="",
        help="must equal X402_NETWORK for --live; guards against a stale .env",
    )
    parser.add_argument(
        "--i-understand-mainnet",
        action="store_true",
        help="additionally required to settle on Base mainnet",
    )
    parser.add_argument("--resume", default=None, help="resume a batch id left mid-settlement")
    parser.add_argument(
        "--limit", type=int, default=10, help="max sessions to plan in one run (default 10)"
    )


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


@dataclass
class Plan:
    """Everything a live run would do, computed without doing any of it."""

    session_id: str
    session_pk: int
    payer: str
    pay_to: str
    scheme: str
    channel_id: str | None
    call_ids: list[int] = field(default_factory=list)
    call_count: int = 0
    gross_atomic: int = 0
    platform_fee_atomic: int = 0
    author_net_atomic: int = 0
    facilitator_fee_atomic: int = 0
    skip_reason: str | None = None

    @property
    def eligible(self) -> bool:
        return self.skip_reason is None and self.call_count > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "payer": self.payer,
            "payTo": self.pay_to,
            "scheme": self.scheme,
            "channelId": self.channel_id,
            "callCount": self.call_count,
            "grossAtomic": str(self.gross_atomic),
            "platformFeeAtomic": str(self.platform_fee_atomic),
            "authorNetAtomic": str(self.author_net_atomic),
            "facilitatorFeeAtomic": str(self.facilitator_fee_atomic),
            "eligible": self.eligible,
            "skipReason": self.skip_reason,
        }


def run(args: argparse.Namespace, out: Printer) -> int:
    from sqlmodel import Session as DBSession

    from app.db import engine

    live = bool(args.live)

    out.title(
        "ERAYA x BRAINWAVE - close batch",
        "LIVE SETTLEMENT" if live else "DRY RUN -- nothing is sent, nothing is written",
    )

    if live:
        refusal = _guard_live(args, out)
        if refusal:
            return refusal

    with DBSession(engine) as db:
        plans = _build_plans(db, args)

    if not plans:
        out.skip("no sessions match")
        out.detail(
            "Sessions become eligible once the batch window has elapsed "
            f"({settings.batch_window_seconds}s since the last call) or the call cap "
            f"({settings.batch_max_calls}) is reached. Use --force-window to override."
        )
        if out.json_mode:
            out.emit_json({"live": live, "plans": [], "settled": []})
        return 0

    _print_plans(out, plans)

    eligible = [p for p in plans if p.eligible]
    if not eligible:
        out.raw()
        out.skip("nothing eligible to close")
        if out.json_mode:
            out.emit_json({"live": live, "plans": [p.as_dict() for p in plans], "settled": []})
        return 0

    if not live:
        _print_payloads(out, eligible)
        out.raw()
        out.rule()
        out.warn("DRY RUN -- no facilitator call was made and no row was written")
        out.detail(f"To settle for real:  --live --yes --confirm-network {settings.x402_network}")
        out.raw()
        if out.json_mode:
            out.emit_json({"live": False, "plans": [p.as_dict() for p in plans], "settled": []})
        return 0

    settled = _settle(eligible, args, out)
    if out.json_mode:
        out.emit_json({"live": True, "plans": [p.as_dict() for p in plans], "settled": settled})
    return 0 if settled else 1


# --------------------------------------------------------------------------
# Guard rails
# --------------------------------------------------------------------------


def _guard_live(args: argparse.Namespace, out: Printer) -> int:
    """Every reason to refuse a live settlement, checked before anything is read.

    Each of these has cost somebody money at least once in the history of
    payments software: an unconfirmed run, a stale network in `.env`, a mainnet
    key in a staging shell, an unfunded facilitator.
    """
    problems: list[str] = []

    if not args.yes:
        problems.append("--live requires --yes")
    if args.confirm_network != settings.x402_network:
        problems.append(
            f"--confirm-network must be {settings.x402_network!r} "
            f"(got {args.confirm_network!r}); this is the check that catches a stale .env"
        )
    if settings.is_mainnet and not args.i_understand_mainnet:
        problems.append(
            "X402_NETWORK is Base MAINNET -- add --i-understand-mainnet to spend real USDC"
        )
    if not args.session and not args.all:
        problems.append("--live needs an explicit --session, or --all if you truly mean all")
    if settings.pay_to_address.lower() == "0x" + "0" * 40:
        problems.append("PAY_TO_ADDRESS is the zero address -- settlement would burn the funds")
    if not settings.facilitator_url:
        problems.append("FACILITATOR_URL is empty")

    if problems:
        out.banner("REFUSING TO SETTLE")
        for problem in problems:
            out.fail(problem)
        out.raw()
        out.detail("Nothing was read, nothing was sent, nothing was written.")
        return 2

    out.banner(
        f"LIVE SETTLEMENT on {settings.x402_network} via {settings.facilitator_url}. "
        "This spends funds and is not reversible."
    )
    return 0


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


def _build_plans(db, args: argparse.Namespace) -> list[Plan]:
    from sqlmodel import select

    from app.models import Batch, Call, CallStatus, PaySession, Scheme, SessionStatus, utcnow

    if args.resume:
        batch = db.exec(select(Batch).where(Batch.batch_id == args.resume)).first()
        if batch is None:
            return []
        pay_session = db.get(PaySession, batch.session_id)
        if pay_session is None:
            return []
        calls = db.exec(select(Call).where(Call.batch_id == batch.id)).all()
        plan = Plan(
            session_id=pay_session.session_id,
            session_pk=pay_session.id,
            payer=pay_session.payer,
            pay_to=batch.pay_to,
            scheme=str(pay_session.scheme),
            channel_id=batch.channel_id or pay_session.channel_id,
            call_ids=[c.id for c in calls],
            call_count=batch.call_count,
            gross_atomic=batch.gross_atomic,
            platform_fee_atomic=batch.platform_fee_atomic,
            author_net_atomic=batch.author_net_atomic,
            facilitator_fee_atomic=batch.facilitator_fee_atomic,
        )
        if str(batch.status) == "settled":
            plan.skip_reason = f"batch {batch.batch_id} is already settled"
        elif not calls:
            plan.skip_reason = f"batch {batch.batch_id} has no attached calls"
        return [plan]

    statement = select(PaySession).where(
        PaySession.status.in_([SessionStatus.OPEN, SessionStatus.FROZEN, SessionStatus.CLOSING])
    )
    if args.session:
        statement = statement.where(PaySession.session_id == args.session)
    statement = statement.order_by(PaySession.opened_at).limit(max(args.limit, 1))

    now = utcnow()
    plans: list[Plan] = []

    for pay_session in db.exec(statement).all():
        plan = Plan(
            session_id=pay_session.session_id,
            session_pk=pay_session.id,
            payer=pay_session.payer,
            pay_to="",
            scheme=str(pay_session.scheme),
            channel_id=pay_session.channel_id,
        )

        if str(pay_session.scheme) != str(Scheme.BATCH_SETTLEMENT):
            plan.skip_reason = (
                f"scheme {pay_session.scheme} has no cumulative voucher; exact and "
                "upto settle on each call"
            )
            plans.append(plan)
            continue

        calls = db.exec(
            select(Call).where(
                Call.session_id == pay_session.id,
                Call.status == CallStatus.CAPTURED,
                Call.batch_id.is_(None),
            )
        ).all()

        if not calls:
            plan.skip_reason = "no captured calls awaiting settlement"
            plans.append(plan)
            continue

        plan.call_ids = [c.id for c in calls]
        plan.call_count = len(calls)
        plan.gross_atomic = sum(c.captured_atomic for c in calls)
        plan.platform_fee_atomic = sum(c.platform_fee_atomic for c in calls)
        plan.author_net_atomic = sum(c.author_net_atomic for c in calls)
        # Two on-chain events per close: claim, then sweep. Charging one would
        # understate the cost of the thing this project is selling.
        plan.facilitator_fee_atomic = settings.facilitator_fee_atomic * 2
        plan.pay_to = calls[0].pay_to

        if not args.force_window:
            last = pay_session.last_call_at or pay_session.opened_at
            # SQLite hands timestamps back naive; compare on the same footing
            # rather than crashing on an aware/naive subtraction.
            if last.tzinfo is None:
                last = last.replace(tzinfo=now.tzinfo)
            window_elapsed = (now - last) >= timedelta(seconds=settings.batch_window_seconds)
            cap_reached = plan.call_count >= settings.batch_max_calls
            if not (window_elapsed or cap_reached):
                remaining = timedelta(seconds=settings.batch_window_seconds) - (now - last)
                plan.skip_reason = (
                    f"batch window still open ({int(remaining.total_seconds())}s left, "
                    f"{plan.call_count}/{settings.batch_max_calls} calls)"
                )
            elif plan.gross_atomic < settings.batch_min_gross_atomic:
                # Settling dust costs more than it collects. Rolling it into the
                # next batch is the correct behaviour, not an error.
                decimals = settings.x402_asset_decimals
                plan.skip_reason = (
                    "below the dust floor "
                    f"({format_atomic(plan.gross_atomic, decimals)} < "
                    f"{format_atomic(settings.batch_min_gross_atomic, decimals)})"
                    " -- rolls into the next batch"
                )

        plans.append(plan)

    return plans


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _print_plans(out: Printer, plans: list[Plan]) -> None:
    d = settings.x402_asset_decimals
    sym = settings.x402_asset_symbol

    out.section("sessions considered")
    for plan in plans:
        out.raw()
        out.kv("session", plan.session_id, width=18)
        out.kv("payer", plan.payer, width=18)
        out.kv("scheme", plan.scheme, width=18)
        if plan.channel_id:
            out.kv("channel", plan.channel_id[:26] + "...", width=18)
        if plan.skip_reason:
            out.skip(plan.skip_reason)
            continue
        out.kv("calls to settle", str(plan.call_count), width=18)
        out.kv("gross", format_atomic(plan.gross_atomic, d, symbol=sym), width=18)
        out.kv("platform take", format_atomic(plan.platform_fee_atomic, d, symbol=sym), width=18)
        out.kv("author net", format_atomic(plan.author_net_atomic, d, symbol=sym), width=18)
        out.kv(
            "facilitator fee",
            format_atomic(plan.facilitator_fee_atomic, d, symbol=sym),
            width=18,
            note="2 events: claim + sweep",
        )
        batched = fee_load_bps(plan.facilitator_fee_atomic, plan.gross_atomic)
        per_call = fee_load_bps(
            settings.facilitator_fee_atomic * plan.call_count, plan.gross_atomic
        )
        out.kv(
            "fee load",
            f"{batched / 100:.2f}%",
            width=18,
            note=f"vs {per_call / 100:.2f}% settling each call",
        )
        out.ok("eligible")

    eligible = [p for p in plans if p.eligible]
    if len(eligible) > 1:
        total_gross = sum(p.gross_atomic for p in eligible)
        out.raw()
        out.section("across this run")
        out.kv("sessions", str(len(eligible)), width=18)
        out.kv("gross", format_atomic(total_gross, d, symbol=sym), width=18)
        out.detail(
            "These can share on-chain events: `ClaimPayload.claims` is a list of "
            "channels, and `SettlePayload` names only a receiver and a token, so one "
            "sweep drains everything claimed for that receiver. The per-session fee "
            "above is therefore an upper bound."
        )


def _print_payloads(out: Printer, plans: list[Plan]) -> None:
    """Render the exact payloads a live run would send.

    Built from the SDK's own dataclasses. Signatures are placeholders here --
    the real ones come from the channel store (see the module docstring), and
    printing an invented signature next to a real payload shape is exactly the
    kind of thing that gets copied into a slide and believed.
    """
    from x402.mechanisms.evm.batch_settlement.types import (
        ChannelConfig,
        ClaimPayload,
        SettlePayload,
        VoucherClaim,
    )

    out.section("payloads a live run would send")

    channelled = [p for p in plans if p.channel_id]
    if not channelled:
        out.skip(
            "no session in this plan uses `batch-settlement`; `exact` settles one "
            "authorization per transaction and has no claim/sweep step"
        )
        return

    claims = []
    for plan in channelled:
        claims.append(
            VoucherClaim(
                channel=ChannelConfig(
                    payer=plan.payer,
                    payer_authorizer="0x" + "0" * 40,
                    receiver=plan.pay_to,
                    receiver_authorizer=plan.pay_to,
                    token=settings.asset_address,
                    withdraw_delay=86_400,
                    salt="0x" + "00" * 32,
                ),
                max_claimable_amount=str(plan.gross_atomic),
                signature="<payer voucher signature, from the channel store>",
                total_claimed=str(plan.gross_atomic),
            )
        )

    out.payload(
        f"ClaimPayload  -- ONE transaction covering {len(claims)} channel(s)",
        ClaimPayload(claims=claims).to_dict(),
        limit=40,
    )

    # One sweep PER RECEIVER, not per batch and not per session. `SettlePayload`
    # carries only a receiver and a token, so channels paying the same author
    # share a sweep and channels paying different authors cannot. Printing a
    # single sweep for a mixed-receiver plan would understate the on-chain cost.
    receivers = sorted({p.pay_to for p in channelled})
    for receiver in receivers:
        out.payload(
            f"SettlePayload -- sweeps everything claimed for {receiver}",
            SettlePayload(receiver=receiver, token=settings.asset_address).to_dict(),
        )
    if len(receivers) > 1:
        out.detail(
            f"{len(receivers)} distinct receivers means {len(receivers)} sweeps: one claim "
            "transaction, but the funds still have to reach different authors."
        )
    out.detail(
        "The `signature` above is a placeholder. The real cumulative voucher "
        "signature lives in the channel store, not in the ledger -- see the module "
        "docstring for why signed material is kept out of the reporting tables."
    )


# --------------------------------------------------------------------------
# The live path
# --------------------------------------------------------------------------


def _settle(plans: list[Plan], args: argparse.Namespace, out: Printer) -> list[dict[str, Any]]:
    """Settle for real. Never invoked by a dry run, and never by the tests.

    The order is deliberate and is the whole recovery story:

        1. write the Batch row (status OPEN) and attach the calls
        2. status CLAIMING  -> facilitator `type=claim`   -> claim_tx_hash
        3. status CLAIMED
        4. status SETTLING  -> facilitator `type=settle`  -> settle_tx_hash
        5. status SETTLED   -> calls, session and receipts updated

    Every transition is committed before the network call that follows it, so a
    process that dies at any point leaves a row saying exactly how far it got.
    A batch stuck at CLAIMED has money claimed on-chain and not yet swept, which
    is recoverable with `--resume` and is invisible if you only store one hash.
    """
    from sqlmodel import Session as DBSession

    from app.db import engine
    from app.models import BatchStatus

    claims_for = _load_channel_store(out)
    if claims_for is None:
        return []

    facilitator = None
    results: list[dict[str, Any]] = []

    for plan in plans:
        out.raw()
        out.section(f"settling {plan.session_id}")

        initial_claims = claims_for(plan.channel_id)
        if not initial_claims and not args.resume:
            out.fail(
                f"channel store has no signed voucher for {plan.channel_id!r}; "
                "nothing can be claimed"
            )
            continue

        if facilitator is None:
            facilitator = _facilitator_client()

        with DBSession(engine) as db:
            batch = _open_batch(db, plan, args)
            out.kv("batch", batch.batch_id, width=16)

            try:
                claims = [] if batch.claim_tx_hash else initial_claims
                if args.resume and not batch.claim_tx_hash and not claims:
                    claims = claims_for(plan.channel_id)
                if not batch.claim_tx_hash and not claims:
                    raise RuntimeError(
                        f"channel store has no signed voucher for {plan.channel_id!r}; "
                        "nothing can be claimed"
                    )

                if not batch.claim_tx_hash:
                    _advance(db, batch, BatchStatus.CLAIMING)
                    claim_response = _post(facilitator, _claim_payload(claims, plan))
                    if not claim_response.success:
                        raise RuntimeError(
                            f"claim rejected: {claim_response.error_reason} "
                            f"{claim_response.error_message or ''}".strip()
                        )
                    batch.claim_tx_hash = claim_response.transaction
                    _advance(db, batch, BatchStatus.CLAIMED)
                    from app.channels import mark_claimed

                    for claim in claims:
                        mark_claimed(plan.channel_id, claim.total_claimed)
                    out.ok(f"claim  {batch.claim_tx_hash}")
                else:
                    out.ok(f"claim already recorded  {batch.claim_tx_hash}")

                if not batch.settle_tx_hash:
                    _advance(db, batch, BatchStatus.SETTLING)
                    settle_response = _post(facilitator, _settle_payload(plan))
                    if not settle_response.success:
                        raise RuntimeError(
                            f"sweep rejected: {settle_response.error_reason} "
                            f"{settle_response.error_message or ''}".strip()
                        )
                    batch.settle_tx_hash = settle_response.transaction
                    batch.explorer_url = settings.explorer_url(batch.settle_tx_hash)
                _finalise(db, batch, plan)
                out.ok(f"settle {batch.settle_tx_hash}")
                if batch.explorer_url:
                    out.kv("explorer", batch.explorer_url, width=16)

                results.append(
                    {
                        "sessionId": plan.session_id,
                        "batchId": batch.batch_id,
                        "claimTxHash": batch.claim_tx_hash,
                        "settleTxHash": batch.settle_tx_hash,
                        "grossAtomic": str(plan.gross_atomic),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                batch.status = BatchStatus.FAILED
                batch.error = f"{type(exc).__name__}: {exc}"[:512]
                db.add(batch)
                db.commit()
                out.fail(batch.error)
                out.detail(
                    f"batch {batch.batch_id} is recorded as FAILED with its last good "
                    f"state. Resume with:  --resume {batch.batch_id}"
                )

    return results


def _load_channel_store(out: Printer):
    """Resolve `app.channels.claims_for`, or refuse with a precise explanation."""
    try:
        from app import channels  # type: ignore[attr-defined]
    except ImportError:
        out.fail("app.channels is not present -- cannot settle")
        out.detail(
            "A live settlement submits the payer's SIGNED cumulative voucher. The "
            "ledger does not hold signatures (Call.nonce and Session.channel_id are "
            "reconciliation keys, not payment artefacts), so the vouchers must come "
            "from the runtime's channel store."
        )
        out.detail(
            "Provide `app/channels.py` exposing "
            "`claims_for(channel_id) -> list[VoucherClaim]`, backed by "
            "x402.mechanisms.evm.batch_settlement.server.storage. The dry run works "
            "without it and shows exactly what would be sent."
        )
        return None

    claims_for = getattr(channels, "claims_for", None)
    if not callable(claims_for):
        out.fail("app.channels exists but has no callable claims_for(channel_id)")
        return None
    return claims_for


def _facilitator_client():
    """The SDK's HTTP facilitator client, configured from settings.

    Sync on purpose: this command is a one-shot script, and a synchronous client
    keeps the failure modes visible in the traceback rather than inside a task
    group.
    """
    from x402.http.facilitator_client import HTTPFacilitatorClientSync
    from x402.http.facilitator_client_base import FacilitatorConfig

    return HTTPFacilitatorClientSync(FacilitatorConfig(url=settings.facilitator_url, timeout=60.0))


def _requirements_for(plan: Plan):
    from x402.schemas.payments import PaymentRequirements

    # `amount` is 0 and `maxTimeoutSeconds` is 0 for settle-actions: the amount
    # lives in the vouchers, not in the requirements. This mirrors the SDK's own
    # `build_payment_requirements` in batch_settlement/server.
    return PaymentRequirements(
        scheme="batch-settlement",
        network=settings.x402_network,
        asset=settings.asset_address,
        amount="0",
        payTo=plan.pay_to,
        maxTimeoutSeconds=0,
        extra={},
    )


def _claim_payload(claims, plan: Plan):
    from x402.schemas.payments import PaymentPayload

    return PaymentPayload(
        x402Version=2,
        payload={"type": "claim", "claims": [c.to_dict() for c in claims]},
        accepted=_requirements_for(plan),
    )


def _settle_payload(plan: Plan):
    from x402.schemas.payments import PaymentPayload

    return PaymentPayload(
        x402Version=2,
        payload={"type": "settle", "receiver": plan.pay_to, "token": settings.asset_address},
        accepted=_requirements_for(plan),
    )


def _post(facilitator, payload):
    return facilitator.settle(payload, payload.accepted)


def _open_batch(db, plan: Plan, args: argparse.Namespace):
    """Create (or resume) the batch row BEFORE any network call."""
    from sqlmodel import select

    from app.models import Batch, BatchStatus, Call, PaySession, Receipt, SessionStatus, utcnow

    if args.resume:
        existing = db.exec(select(Batch).where(Batch.batch_id == args.resume)).first()
        if existing:
            return existing

    batch = Batch(
        batch_id=new_id("batch"),
        session_id=plan.session_pk,
        channel_id=plan.channel_id,
        network=settings.x402_network,
        asset=settings.asset_address,
        asset_decimals=settings.x402_asset_decimals,
        pay_to=plan.pay_to,
        call_count=plan.call_count,
        gross_atomic=plan.gross_atomic,
        platform_fee_atomic=plan.platform_fee_atomic,
        author_net_atomic=plan.author_net_atomic,
        facilitator_fee_atomic=plan.facilitator_fee_atomic,
        status=BatchStatus.OPEN,
        opened_at=utcnow(),
    )
    db.add(batch)
    db.flush()

    calls = db.exec(select(Call).where(Call.id.in_(plan.call_ids))).all()
    for call in calls:
        call.batch_id = batch.id
        db.add(call)
    receipts = db.exec(select(Receipt).where(Receipt.call_id.in_(plan.call_ids))).all()
    for receipt in receipts:
        receipt.batch_id = batch.id
        db.add(receipt)
    pay_session = db.get(PaySession, plan.session_pk)
    if pay_session is not None:
        pay_session.status = SessionStatus.CLOSING
        db.add(pay_session)
    db.commit()
    db.refresh(batch)
    return batch


def _advance(db, batch, status) -> None:
    """Commit a status transition before the network call it precedes."""
    batch.status = status
    db.add(batch)
    db.commit()


def _finalise(db, batch, plan: Plan) -> None:
    """Mark everything settled, in one transaction.

    Calls, session and receipts move together: a receipt pointing at a batch
    that the session does not agree is settled is precisely the inconsistency
    `doctor` looks for, so it must not be possible to produce one by crashing
    between two commits.
    """
    from sqlmodel import select

    from app.models import (
        BatchStatus,
        Call,
        CallStatus,
        PaySession,
        Receipt,
        SessionStatus,
        utcnow,
    )

    now = utcnow()
    batch.status = BatchStatus.SETTLED
    batch.closed_at = now
    batch.settled_at = now
    db.add(batch)

    calls = db.exec(select(Call).where(Call.id.in_(plan.call_ids))).all()
    for call in calls:
        call.batch_id = batch.id
        call.status = CallStatus.SETTLED
        call.settled_at = now
        db.add(call)

    receipts = db.exec(select(Receipt).where(Receipt.call_id.in_(plan.call_ids))).all()
    for receipt in receipts:
        receipt.batch_id = batch.id
        db.add(receipt)
    db.flush()

    from app.pay.receipts import attach_batch_settlement

    attach_batch_settlement(db, batch)

    pay_session = db.get(PaySession, plan.session_pk)
    if pay_session is not None:
        pay_session.settled_atomic += plan.gross_atomic
        pay_session.status = SessionStatus.SETTLED
        pay_session.closed_at = now
        db.add(pay_session)

    db.commit()
