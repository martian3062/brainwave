"""Ledger writes for the paid gateway.

Everything the payment path knows ends up here, and the shape of what is written
is chosen so that one claim is checkable from the database alone:

    sum(Call.captured_atomic) for a session
      == Batch.gross_atomic for its batches
      == what the settlement transaction actually moved

The x402 SDK has no ledger at all -- it verifies, executes and settles, and
forgets. Per-author accounting, a platform take, receipts, and the session ->
batch -> transaction chain are what this module adds, and they are what makes
the economic argument provable rather than asserted.

Two mechanical notes:

* Every function here is SYNCHRONOUS and touches a synchronous SQLAlchemy
  engine. The payment path is async, so callers wrap these in
  `asyncio.to_thread` (see `app.gateway.paid`). Do not add `async def` to this
  module and quietly block the event loop.

* Money is integer atomic units, end to end. `app.money` is the only converter
  and no float is constructed anywhere in this file.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.config import settings
from app.db import session_scope
from app.gateway.config import gateway_settings
from app.models import (
    Author,
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
from app.money import fee_load_bps, format_atomic, split_take

log = logging.getLogger(__name__)

__all__ = [
    "ToolSpec",
    "sync_catalogue",
    "catalogue",
    "tool_row",
    "record_call",
    "receipt_body",
    "verify_receipt",
    "close_batch",
    "session_summary",
]


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


# --------------------------------------------------------------------------
# ENUM COMPARISON -- read this before writing `is` against a status.
#
# `app.models` deliberately stores every StrEnum as `String(32)` rather than
# sa.Enum, so the ledger holds the x402 WIRE value ('batch-settlement') instead
# of the member NAME ('BATCH_SETTLEMENT'). The consequence, which is not
# obvious: a value LOADED BACK from the database is a plain `str`, not the
# StrEnum member, because a String column performs no enum coercion on read.
#
#     row.status is BatchStatus.OPEN     -> False. Always. Silently.
#     row.status == BatchStatus.OPEN     -> True   (StrEnum compares by value)
#
# This bit us for real: `close_batch` returned "already closed" for every open
# batch, and `session_summary` counted one settlement event per call, making the
# batching numbers wrong in the one direction that would have been embarrassing.
# Use `same_status` (or plain `==`) for anything that came out of the database.
# --------------------------------------------------------------------------


def same_status(value: Any, member: Any) -> bool:
    """Value equality between a StrEnum member and whatever the DB returned."""
    return str(value) == str(member)


# --------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """A priced tool as declared in code.

    The DB row is authoritative at runtime -- an operator can reprice from
    /admin without a redeploy, which is the reason `Tool.price_atomic` exists at
    all. This is the seed, and (when CATALOGUE_SYNC_PRICES is on) the thing the
    row is re-synced from at boot.
    """

    name: str
    description: str
    price_atomic: int
    scheme: Scheme = Scheme.EXACT
    tags: tuple[str, ...] = ()
    max_price_atomic: int | None = None
    meter: str | None = None
    price_per_unit_atomic: int | None = None
    max_timeout_seconds: int | None = None
    #: Cost note shown in listings: why this price, in one line.
    rationale: str = ""

    @property
    def resource_url(self) -> str:
        return f"mcp://tool/{self.name}"

    @property
    def authorized_atomic(self) -> int:
        """What the agent is asked to authorize: the ceiling for `upto`."""
        return self.max_price_atomic or self.price_atomic


def _ensure_author() -> Author:
    """The catalogue's author row. Created once, then reused."""
    with session_scope() as db:
        author = db.exec(
            select(Author).where(Author.slug == gateway_settings.catalogue_author_slug)
        ).first()
        if author is None:
            author = Author(
                slug=gateway_settings.catalogue_author_slug,
                display_name=gateway_settings.catalogue_author_name,
                pay_to=settings.pay_to_address,
                is_active=True,
            )
            db.add(author)
            db.flush()
            log.info("catalogue author %r created", author.slug)
        elif author.pay_to != settings.pay_to_address and not author.pay_to:
            author.pay_to = settings.pay_to_address
            db.add(author)
        db.refresh(author)
        db.expunge(author)
        return author


def sync_catalogue(specs: list[ToolSpec]) -> None:
    """Upsert the declared tools. Idempotent; safe to run on every boot.

    Failure is logged, never raised: a gateway that will not start because the
    catalogue table is momentarily unavailable is worse than one that serves
    tools whose price row is a boot behind.
    """
    try:
        author = _ensure_author()
    except Exception:
        log.exception("catalogue author unavailable -- tool rows not synced")
        return

    try:
        with session_scope() as db:
            for spec in specs:
                row = db.exec(select(Tool).where(Tool.name == spec.name)).first()
                if row is None:
                    db.add(_new_tool_row(spec, author.id))
                    continue
                row.author_id = author.id
                row.description = spec.description[:1024]
                row.tags = ",".join(spec.tags)[:256] or None
                row.resource_url = spec.resource_url
                row.enabled = True
                row.network = settings.x402_network
                row.asset = settings.asset_address
                row.asset_decimals = settings.x402_asset_decimals
                if gateway_settings.catalogue_sync_prices:
                    row.scheme = spec.scheme
                    row.price_atomic = spec.price_atomic
                    row.max_price_atomic = spec.max_price_atomic
                    row.meter = spec.meter
                    row.price_per_unit_atomic = spec.price_per_unit_atomic
                    row.max_timeout_seconds = (
                        spec.max_timeout_seconds or settings.payment_timeout_seconds
                    )
                row.updated_at = utcnow()
                db.add(row)
        log.info("catalogue synced: %d paid tools", len(specs))
    except Exception:
        log.exception("catalogue sync failed -- serving with whatever rows exist")


def _new_tool_row(spec: ToolSpec, author_id: int | None) -> Tool:
    return Tool(
        author_id=author_id or 0,
        name=spec.name,
        resource_url=spec.resource_url,
        description=spec.description[:1024],
        tags=",".join(spec.tags)[:256] or None,
        scheme=spec.scheme,
        network=settings.x402_network,
        asset=settings.asset_address,
        asset_decimals=settings.x402_asset_decimals,
        price_atomic=spec.price_atomic,
        max_price_atomic=spec.max_price_atomic,
        meter=spec.meter,
        price_per_unit_atomic=spec.price_per_unit_atomic,
        max_timeout_seconds=spec.max_timeout_seconds or settings.payment_timeout_seconds,
        enabled=True,
    )


def tool_row(name: str) -> dict[str, Any] | None:
    """Live pricing for one tool, as a plain dict (no detached-ORM traps)."""
    try:
        with session_scope() as db:
            row = db.exec(select(Tool).where(Tool.name == name)).first()
            if row is None:
                return None
            return {
                "id": row.id,
                "name": row.name,
                "resource_url": row.resource_url,
                "description": row.description,
                "tags": [t for t in (row.tags or "").split(",") if t],
                "scheme": str(row.scheme),
                "price_atomic": row.price_atomic,
                "max_price_atomic": row.max_price_atomic,
                "meter": row.meter,
                "price_per_unit_atomic": row.price_per_unit_atomic,
                "max_timeout_seconds": row.max_timeout_seconds,
                "enabled": row.enabled,
                "total_calls": row.total_calls,
                "total_captured_atomic": row.total_captured_atomic,
                "pay_to": _pay_to_for(db, row.author_id),
            }
    except Exception:
        log.exception("tool_row(%s) failed", name)
        return None


def catalogue() -> list[dict[str, Any]]:
    """Every enabled paid tool, for the free discovery tool."""
    try:
        with session_scope() as db:
            rows = db.exec(select(Tool).where(Tool.enabled == True)).all()  # noqa: E712
            return [
                {
                    "name": r.name,
                    "resource": r.resource_url,
                    "description": r.description,
                    "tags": [t for t in (r.tags or "").split(",") if t],
                    "scheme": str(r.scheme),
                    "network": r.network,
                    "asset": r.asset,
                    "priceAtomic": str(r.price_atomic),
                    "price": format_atomic(
                        r.price_atomic, r.asset_decimals, symbol=settings.x402_asset_symbol
                    ),
                    "maxPriceAtomic": (
                        str(r.max_price_atomic) if r.max_price_atomic is not None else None
                    ),
                    "meter": r.meter,
                    "pricePerUnitAtomic": (
                        str(r.price_per_unit_atomic)
                        if r.price_per_unit_atomic is not None
                        else None
                    ),
                    "totalCalls": r.total_calls,
                    "totalCapturedAtomic": str(r.total_captured_atomic),
                }
                for r in sorted(rows, key=lambda r: r.name)
            ]
    except Exception:
        log.exception("catalogue() failed")
        return []


def _pay_to_for(db: Any, author_id: int) -> str:
    author = db.get(Author, author_id)
    return (author.pay_to if author and author.pay_to else settings.pay_to_address) or ""


def _take_bps_for(db: Any, author_id: int) -> int:
    author = db.get(Author, author_id)
    if author is not None and author.platform_take_bps is not None:
        return author.platform_take_bps
    return settings.platform_take_bps


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


def _get_or_open_session(
    db: Any,
    payer: str,
    *,
    agent_label: str | None,
    agent_identity: str | None,
    scheme: Scheme,
    mode: SettlementMode,
) -> PaySession:
    """One open session per payer.

    A payment session is OUR concept, not the MCP transport's: `stateless_http`
    is on, so an agent may hit a different worker on every call and there is no
    transport session to hang this off. Keying on the payer address is what
    makes the session survive that -- and the payer is the only identifier the
    protocol guarantees us.
    """
    row = db.exec(
        select(PaySession)
        .where(PaySession.payer == payer)
        .where(PaySession.status == SessionStatus.OPEN)
        .order_by(PaySession.opened_at.desc())
    ).first()
    if row is not None:
        if agent_label and not row.agent_label:
            row.agent_label = agent_label[:128]
        if agent_identity and not row.agent_identity:
            row.agent_identity = agent_identity[:128]
        return row

    row = PaySession(
        session_id=_id("sess"),
        payer=payer[:64],
        agent_label=(agent_label or None) and agent_label[:128],
        agent_identity=(agent_identity or None) and agent_identity[:128],
        network=settings.x402_network,
        asset=settings.asset_address,
        asset_decimals=settings.x402_asset_decimals,
        scheme=scheme,
        settlement_mode=mode,
        status=SessionStatus.OPEN,
        budget_atomic=None,
    )
    db.add(row)
    db.flush()
    return row


def _get_or_open_batch(db: Any, session: PaySession, pay_to: str) -> Batch:
    """The session's open batch, created on first deferred capture.

    A batch is closed by `close_batch()`; nothing here decides *when*, because
    that is a policy the settlement worker owns.
    """
    row = db.exec(
        select(Batch)
        .where(Batch.session_id == session.id)
        .where(Batch.status == BatchStatus.OPEN)
        .order_by(Batch.opened_at.desc())
    ).first()
    if row is not None:
        return row

    row = Batch(
        batch_id=_id("batch"),
        session_id=session.id or 0,
        channel_id=session.channel_id,
        network=settings.x402_network,
        asset=settings.asset_address,
        asset_decimals=settings.x402_asset_decimals,
        pay_to=pay_to[:64],
        status=BatchStatus.OPEN,
        facilitator_fee_atomic=settings.facilitator_fee_atomic,
    )
    db.add(row)
    db.flush()
    return row


# --------------------------------------------------------------------------
# The write that matters
# --------------------------------------------------------------------------


@dataclass
class CallRecord:
    """Everything the payment layer learned about one invocation."""

    tool_name: str
    status: CallStatus
    payer: str | None = None
    agent_label: str | None = None
    agent_identity: str | None = None
    scheme: Scheme = Scheme.EXACT
    authorized_atomic: int = 0
    captured_atomic: int = 0
    meter: str | None = None
    meter_units: int | None = None
    meter_detail: dict[str, Any] | None = None
    nonce: str | None = None
    decline_reason: str | None = None
    error: str | None = None
    verify_ms: int | None = None
    execute_ms: int | None = None
    settle_ms: int | None = None
    tx_hash: str | None = None
    settlement: dict[str, Any] | None = None


def record_call(record: CallRecord) -> dict[str, Any] | None:
    """Write one Call (+ Receipt, when money moved) and update the rollups.

    Returns the receipt body to hand back to the agent, or None when there is
    nothing to receipt (an unpaid 402 probe has no payment artefact to attest).

    Never raises. A ledger write failing must not turn a paid, delivered tool
    call into an error the agent sees -- the work happened, the payment
    happened, and losing the row is an operations problem, loudly logged.
    """
    try:
        return _record_call(record)
    except IntegrityError as exc:
        # The UNIQUE(network, nonce) on `call` is a replay defence. Hitting it
        # means this exact authorization was already captured.
        log.warning("ledger rejected call for %s (likely nonce replay): %s", record.tool_name, exc)
        return {
            "error": "replay_detected",
            "detail": "this payment authorization has already been captured",
            "tool": record.tool_name,
        }
    except Exception:
        log.exception("ledger write failed for %s -- call served, row lost", record.tool_name)
        return None


def _record_call(record: CallRecord) -> dict[str, Any] | None:
    now = utcnow()
    body: dict[str, Any] | None = None

    with session_scope() as db:
        tool = db.exec(select(Tool).where(Tool.name == record.tool_name)).first()
        if tool is None:
            log.warning("no Tool row for %s -- not ledgered", record.tool_name)
            return None

        pay_to = _pay_to_for(db, tool.author_id)
        take_bps = _take_bps_for(db, tool.author_id)
        deferred = bool(
            record.settlement
            and str(record.settlement.get("settlement", "")).startswith("deferred")
        )
        mode = SettlementMode.BATCHED if deferred else SettlementMode.PER_CALL

        # An unpaid 402 probe has no payer. It is still worth a row -- the
        # conversion funnel is the difference between a dashboard and a
        # scoreboard -- but it opens no session and issues no receipt.
        payer = record.payer or "unknown"
        session = _get_or_open_session(
            db,
            payer,
            agent_label=record.agent_label,
            agent_identity=record.agent_identity,
            scheme=record.scheme,
            mode=mode,
        )

        captured = max(record.captured_atomic, 0)
        authorized = max(record.authorized_atomic, 0)
        if captured > authorized:
            raise ValueError(
                f"capture {captured} exceeds authorization {authorized} for "
                f"{record.tool_name}; refusing to rewrite the signed ceiling"
            )
        platform_fee, author_net = split_take(captured, take_bps)

        batch: Batch | None = None
        if captured and deferred:
            batch = _get_or_open_batch(db, session, pay_to)

        call = Call(
            call_id=_id("call"),
            session_id=session.id or 0,
            tool_id=tool.id or 0,
            batch_id=batch.id if batch else None,
            payer=payer[:64],
            pay_to=pay_to[:64],
            network=settings.x402_network,
            asset=settings.asset_address,
            scheme=record.scheme,
            authorized_atomic=authorized,
            captured_atomic=captured,
            platform_fee_atomic=platform_fee,
            author_net_atomic=author_net,
            meter=record.meter,
            meter_units=record.meter_units,
            status=record.status,
            decline_reason=record.decline_reason,
            error=(record.error or None) and record.error[:512],
            nonce=(record.nonce or None) and record.nonce[:80],
            verify_ms=record.verify_ms,
            execute_ms=record.execute_ms,
            settle_ms=record.settle_ms,
            created_at=now,
            executed_at=now if record.status != CallStatus.CHALLENGED else None,
            settled_at=now if record.status == CallStatus.SETTLED else None,
        )
        db.add(call)
        db.flush()  # surfaces the nonce UNIQUE violation here, before any rollup

        # ---- rollups ----------------------------------------------------
        session.call_count += 1
        session.last_call_at = now
        if record.status == CallStatus.DECLINED:
            session.declined_count += 1
        if captured:
            session.captured_atomic += captured
            # Under batch-settlement the voucher ceiling is monotonic, not a
            # sum; under exact/upto it is the running total of authorizations.
            session.authorized_atomic = (
                max(session.authorized_atomic, authorized)
                if same_status(record.scheme, Scheme.BATCH_SETTLEMENT)
                else session.authorized_atomic + authorized
            )
            if not deferred:
                session.settled_atomic += captured

            tool.total_calls += 1
            tool.total_captured_atomic += captured
            db.add(tool)
        db.add(session)

        if batch is not None and captured:
            batch.call_count += 1
            batch.gross_atomic += captured
            batch.platform_fee_atomic += platform_fee
            batch.author_net_atomic += author_net
            db.add(batch)

        # ---- receipt ----------------------------------------------------
        if captured or record.status in (CallStatus.SETTLED, CallStatus.CAPTURED):
            body = _build_receipt_body(
                call=call,
                session=session,
                tool=tool,
                batch=batch,
                record=record,
                pay_to=pay_to,
                issued_at=now,
            )
            from app.pay.receipts import body_digest, canonical_json

            digest = body_digest(body)
            response_body = {**body, "bodyHash": digest}

            db.add(
                Receipt(
                    receipt_id=body["receiptId"],
                    call_id=call.id or 0,
                    session_id=session.id or 0,
                    batch_id=batch.id if batch else None,
                    scheme=record.scheme,
                    network=settings.x402_network,
                    asset=settings.asset_address,
                    asset_decimals=settings.x402_asset_decimals,
                    authorized_atomic=authorized,
                    captured_atomic=captured,
                    payer=payer[:64],
                    pay_to=pay_to[:64],
                    resource_url=tool.resource_url[:512],
                    settlement=(SettlementMode.BATCHED if deferred else SettlementMode.PER_CALL),
                    tx_hash=(record.tx_hash or None) and record.tx_hash[:80],
                    explorer_url=settings.explorer_url(record.tx_hash),
                    facilitator=settings.facilitator_label[:64],
                    body_hash=digest,
                    body_json=canonical_json(body),
                    issued_at=now,
                )
            )

            body = response_body

    return body


def _build_receipt_body(
    *,
    call: Call,
    session: PaySession,
    tool: Tool,
    batch: Batch | None,
    record: CallRecord,
    pay_to: str,
    issued_at: datetime,
) -> dict[str, Any]:
    """The receipt handed back inside the tool response.

    Deliberately shows `authorized` AND `captured` separately: under `upto` they
    differ, and showing only the charge is the difference between a payment
    system and a black box.
    """
    decimals = settings.x402_asset_decimals
    symbol = settings.x402_asset_symbol
    body: dict[str, Any] = {
        "receiptId": _id("rcpt"),
        "x402Version": 2,
        "callId": call.call_id,
        "sessionId": session.session_id,
        "batchId": batch.batch_id if batch else None,
        "tool": tool.name,
        "resource": tool.resource_url,
        "scheme": str(record.scheme),
        "network": settings.x402_network,
        "asset": settings.asset_address,
        "assetSymbol": symbol,
        "assetDecimals": decimals,
        "payer": call.payer,
        "payTo": pay_to,
        "authorizedAtomic": str(call.authorized_atomic),
        "capturedAtomic": str(call.captured_atomic),
        "captured": format_atomic(call.captured_atomic, decimals, symbol=symbol),
        "platformFeeAtomic": str(call.platform_fee_atomic),
        "authorNetAtomic": str(call.author_net_atomic),
        "facilitator": settings.facilitator_label,
        "settlement": dict(record.settlement or {"settlement": "immediate"}),
        "transaction": record.tx_hash or None,
        "explorerUrl": settings.explorer_url(record.tx_hash),
        "issuedAt": issued_at.astimezone(UTC).isoformat(),
        "verify": "call the free tool `verify_receipt` with this receiptId",
    }
    if record.meter:
        body["metering"] = {
            "unit": record.meter,
            "units": record.meter_units,
            "detail": record.meter_detail or {},
        }
    return body


# --------------------------------------------------------------------------
# Verification (free, always)
# --------------------------------------------------------------------------


def receipt_body(receipt_id: str) -> dict[str, Any] | None:
    try:
        with session_scope() as db:
            row = db.exec(select(Receipt).where(Receipt.receipt_id == receipt_id)).first()
            if row is None:
                return None
            return {**json.loads(row.body_json), "bodyHash": row.body_hash}
    except Exception:
        log.exception("receipt_body(%s) failed", receipt_id)
        return None


def verify_receipt(receipt_id: str, presented_body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Check a receipt against the ledger. Never paywalled.

    Two independent checks, and the distinction matters:

      * `bodyHashMatches` -- the stored body still hashes to its recorded digest,
        so nothing tampered with the ledger row.
      * `presentedMatches` -- an agent's copy of the receipt matches the stored
        one byte for byte, so nothing tampered with the receipt in transit.

    Neither is a claim about the chain. `onChain` reports whether a settlement
    transaction exists yet and says so plainly when it does not.
    """
    try:
        with session_scope() as db:
            row = db.exec(select(Receipt).where(Receipt.receipt_id == receipt_id)).first()
            if row is None:
                return {"receiptId": receipt_id, "found": False, "verified": False}

            stored = json.loads(row.body_json)
            from app.pay.receipts import body_digest

            recomputed = dict(stored)
            recomputed.pop("bodyHash", None)
            digest = body_digest(recomputed)
            body_ok = digest == row.body_hash

            presented_ok: bool | None = None
            if presented_body is not None:
                presented = dict(presented_body)
                presented.pop("bodyHash", None)
                presented_ok = json.dumps(presented, sort_keys=True) == json.dumps(
                    recomputed, sort_keys=True
                )

            batch = db.get(Batch, row.batch_id) if row.batch_id else None
            row.verified_at = utcnow()
            row.verify_status = "ok" if body_ok else "tampered"
            db.add(row)

            return {
                "receiptId": receipt_id,
                "found": True,
                "verified": bool(body_ok and (presented_ok is not False)),
                "bodyHashMatches": body_ok,
                "presentedMatches": presented_ok,
                "capturedAtomic": str(row.captured_atomic),
                "authorizedAtomic": str(row.authorized_atomic),
                "payer": row.payer,
                "payTo": row.pay_to,
                "onChain": {
                    "settled": bool(row.tx_hash),
                    "txHash": row.tx_hash,
                    "explorerUrl": row.explorer_url,
                    "batchId": batch.batch_id if batch else None,
                    "batchStatus": str(batch.status) if batch else None,
                    "note": (
                        "settled on-chain"
                        if row.tx_hash
                        else "captured in the ledger; not yet settled on-chain"
                    ),
                },
                "attestation": row.attestation,
                "facilitator": row.facilitator,
                "body": {**stored, "bodyHash": row.body_hash},
            }
    except Exception:
        log.exception("verify_receipt(%s) failed", receipt_id)
        return {
            "receiptId": receipt_id,
            "found": False,
            "verified": False,
            "error": "lookup failed",
        }


# --------------------------------------------------------------------------
# Batch close -- the seam the settlement worker plugs into
# --------------------------------------------------------------------------


def close_batch(batch_id: str) -> dict[str, Any]:
    """Freeze an open batch and report what settling it would cost.

    THIS PERFORMS NO ON-CHAIN ACTION and deliberately does not pretend to. It
    moves the batch OPEN -> CLAIMING and returns the reconciliation figures; a
    settlement worker with a funded signer is what turns that into a claim and a
    sweep, and it is what fills in `claim_tx_hash` / `settle_tx_hash`.

    The returned `feeLoadBps` is the whole argument in one integer: the
    facilitator's per-settlement fee as a fraction of the gross this one batch
    covers. Compare it against the same figure for a single call.
    """
    try:
        with session_scope() as db:
            batch = db.exec(select(Batch).where(Batch.batch_id == batch_id)).first()
            if batch is None:
                return {"batchId": batch_id, "error": "not found", "closed": False}
            # `==`, not `is` -- see the note on `same_status` above.
            if not same_status(batch.status, BatchStatus.OPEN):
                return {"batchId": batch_id, "status": str(batch.status), "closed": False}

            batch.status = BatchStatus.CLAIMING
            batch.closed_at = utcnow()
            batch.facilitator_fee_atomic = settings.facilitator_fee_atomic
            db.add(batch)

            per_call_cost = settings.facilitator_fee_atomic * max(batch.call_count, 1)
            return {
                "batchId": batch.batch_id,
                "closed": True,
                "status": str(batch.status),
                "callCount": batch.call_count,
                "grossAtomic": str(batch.gross_atomic),
                "platformFeeAtomic": str(batch.platform_fee_atomic),
                "authorNetAtomic": str(batch.author_net_atomic),
                "facilitatorFeeAtomic": str(batch.facilitator_fee_atomic),
                "feeLoadBps": fee_load_bps(batch.facilitator_fee_atomic, batch.gross_atomic),
                "feeLoadBpsIfSettledPerCall": fee_load_bps(per_call_cost, batch.gross_atomic),
                "note": (
                    "Ledger state only. No on-chain claim or sweep has been "
                    "submitted; claim_tx_hash and settle_tx_hash are still null."
                ),
            }
    except Exception:
        log.exception("close_batch(%s) failed", batch_id)
        return {"batchId": batch_id, "error": "close failed"}


def session_summary(session_id: str) -> dict[str, Any] | None:
    """Everything an agent (or the dashboard) needs to audit one session."""
    try:
        with session_scope() as db:
            row = db.exec(select(PaySession).where(PaySession.session_id == session_id)).first()
            if row is None:
                return None
            batches = db.exec(select(Batch).where(Batch.session_id == row.id)).all()
            decimals = row.asset_decimals
            # `==`, not `is` -- see the note on `same_status` above. Getting
            # this wrong counted one settlement per call and made the batching
            # figures wrong in the flattering direction.
            settle_events = (
                len(batches)
                if same_status(row.settlement_mode, SettlementMode.BATCHED)
                else row.call_count
            )
            cost = settings.facilitator_fee_atomic * max(settle_events, 1)
            return {
                "sessionId": row.session_id,
                "payer": row.payer,
                "agentLabel": row.agent_label,
                "status": str(row.status),
                "settlementMode": str(row.settlement_mode),
                "callCount": row.call_count,
                "declinedCount": row.declined_count,
                "capturedAtomic": str(row.captured_atomic),
                "captured": format_atomic(
                    row.captured_atomic, decimals, symbol=settings.x402_asset_symbol
                ),
                "settledAtomic": str(row.settled_atomic),
                "outstandingAtomic": str(row.captured_atomic - row.settled_atomic),
                "settlementEvents": settle_events,
                "facilitatorCostAtomic": str(cost),
                "feeLoadBps": fee_load_bps(cost, row.captured_atomic),
                "feeLoadBpsIfSettledPerCall": fee_load_bps(
                    settings.facilitator_fee_atomic * max(row.call_count, 1), row.captured_atomic
                ),
                "batches": [
                    {
                        "batchId": b.batch_id,
                        "status": str(b.status),
                        "callCount": b.call_count,
                        "grossAtomic": str(b.gross_atomic),
                        "claimTxHash": b.claim_tx_hash,
                        "settleTxHash": b.settle_tx_hash,
                    }
                    for b in batches
                ],
            }
    except Exception:
        log.exception("session_summary(%s) failed", session_id)
        return None
