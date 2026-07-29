"""`@paid()` -- pricing, metering, ledger and receipts around the SDK's payment wrapper.

WHAT THE SDK DOES AND WHAT WE DO
================================
`x402.mcp.create_payment_wrapper` implements paid MCP. It issues the 402 challenge,
parses the authorization out of the MCP `_meta` key `x402/payment`, matches it
against the advertised requirements, asks the facilitator to verify it, runs the
handler, asks the facilitator to settle, and puts the settlement response back in
`x402/payment-response`. We did not write any of that and `@paid()` does not
reimplement it -- it CALLS it. Every 402 body an agent sees is built by
`_create_payment_required_result` in `x402/mcp/server.py`.

`@paid()` adds four things the SDK has no opinion about:

    pricing    exact integer atomic units, and the `PaymentRequirements` they make
    metering   what the call actually consumed, and therefore what to capture
    ledger     Author / Tool / Session / Call rows, with a platform take
    receipts   a per-call artefact that reconciles session -> batch -> transaction

THE ONE PLACE WE TOUCH THE PROTOCOL, AND WHY
============================================
`create_payment_wrapper` settles the requirements it advertised. That is right for
`exact` and wrong for `upto`, whose entire purpose is to authorize a ceiling and
capture less. On the plain-HTTP side the SDK has an answer -- `Settlement-Overrides`
and `_apply_settlement_overrides`, which does
`requirements.model_copy(update={"amount": resolved})` -- but over MCP there is no
equivalent seam: `wrapped()` passes the matched requirements straight to
`settle_payment`.

`_MeteredResourceServer` below is that seam. It is a proxy around the real
`x402ResourceServer` that, at settlement time, substitutes the metered amount using
the SAME `model_copy(update={"amount": ...})` the SDK's HTTP path uses. It is not a
reimplementation of settlement; it is one field, changed the way the library changes
it, on a transport where the library forgot to expose the switch.

Mutating the requirements object in place -- the obvious shortcut, since
`on_after_execution` hands you the live object just before settlement -- is a bug,
and it is worth recording why: `x402ResourceServerBase.find_matching_requirements`
returns the element FROM the `accepts` list, so that object is shared by every call
to the tool. Mutating it would change the advertised price for everyone and then
break matching, because matching compares `amount`.

HOW PER-CALL STATE IS CARRIED
=============================
By `contextvars`, not by `id(payload)`.

The SDK's own batch-settlement scheme correlates its per-request state with
`self._request_contexts[id(payload)]`. That works, but it needs a strong reference
to keep the id from being recycled and a sweeper for the paths where the final hook
never fires. A `ContextVar` avoids both: `verify_payment`, the handler and
`settle_payment` are all awaited from the same coroutine inside `wrapped()`, so they
share one context, while every concurrent request is a separate task with a copied
context. If the SDK ever dispatches the resource server through `asyncio.to_thread`
-- it does exactly that when `settle_payment` is not a coroutine function -- the
context would be lost, so both proxy methods are `async def` deliberately, and
`_current()` returning None is logged rather than papered over.

WHAT IS NOT HERE
================
No buyer-side spend Guardian. `_admit()` below is the SELLER's admission check
(tool enabled, session open, budget, replay) and is not the same thing as a buyer
deciding whether to sign. `app.guardian` is that module; if it exists, `_admit`
defers to it, and if it does not, the seller-side checks still run.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from x402.mcp.constants import MCP_PAYMENT_META_KEY

from app.config import settings
from app.models import (
    Author,
    Call,
    CallStatus,
    PaySession,
    Scheme,
    SessionStatus,
    SettlementMode,
    Tool,
    utcnow,
)
from app.money import PriceError
from app.pay import batching, gateway
from app.pay import receipts as receipts_mod
from app.pay.meters import (
    ExecutionOutcome,
    MeterReading,
    capture,
    declared_units,
    strip_meter_declaration,
)
from app.pay.pricing import PriceSpec, payment_requirements, resource_info, wire_amount

log = logging.getLogger(__name__)

__all__ = ["paid", "PaidTool", "registry", "sync_catalogue", "HOUSE_AUTHOR_SLUG"]

#: Author a tool is filed under when the decorator is not told otherwise.
HOUSE_AUTHOR_SLUG = "eraya"


# --------------------------------------------------------------------------
# Per-call state
# --------------------------------------------------------------------------


@dataclass
class _InFlight:
    """One tool call, from verification to settlement."""

    spec: PriceSpec
    tool_id: int
    payer: str
    authorized_atomic: int

    session_pk: int | None = None
    session_id: str | None = None
    call_pk: int | None = None
    call_id: str | None = None

    started_ns: int = 0
    verify_ms: int | None = None
    execute_ms: int | None = None
    reading: MeterReading | None = None
    declined: str | None = None
    receipt_body: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


#: Set in `verify_payment`, read by the handler wrapper and by `settle_payment`.
#: All three run in the same task, so they share this; separate requests are
#: separate tasks and get a copied context.
_CURRENT: contextvars.ContextVar[_InFlight | None] = contextvars.ContextVar(
    "brainwave_inflight", default=None
)


def _current() -> _InFlight | None:
    return _CURRENT.get()


# --------------------------------------------------------------------------
# Ledger writes.
#
# Sync functions, always called via `asyncio.to_thread` so a DB round trip never
# blocks the event loop, and always taking and returning PLAIN VALUES -- primary
# keys and dicts, never ORM instances, which would be detached the moment their
# session closed and would then lazy-load from the wrong thread.
#
# All of them go through `batching.ledger_write()`, which holds a process-wide
# reentrant lock. That is load-bearing, not defensive: `open_or_reuse_session` is
# a SELECT-then-INSERT with no uniqueness constraint under it, so without the lock
# two concurrent first calls from one payer open two sessions and split the
# agent's budget. See the comment on `batching.LEDGER_LOCK`.
# --------------------------------------------------------------------------


def _call_id() -> str:
    return f"call_{uuid.uuid4().hex[:24]}"


def _ensure_author(db: Any, slug: str, display_name: str, pay_to: str) -> Author:
    author = db.exec(select(Author).where(Author.slug == slug)).first()
    if author is None:
        author = Author(slug=slug, display_name=display_name, pay_to=pay_to)
        db.add(author)
        db.flush()
    return author


def _upsert_tool(spec: PriceSpec, author_slug: str, author_name: str) -> int:
    """Create or reprice the `tool` row for a spec. Returns its primary key.

    The declaration in code seeds the row; after that the ROW is authoritative, so
    an operator can reprice from /admin without a redeploy. Only the fields that
    describe the tool's shape (scheme, meter, network) are re-synced from code --
    the price is not overwritten, because doing so would silently revert every
    manual repricing on the next deploy.
    """
    with batching.ledger_write() as db:
        author = _ensure_author(db, author_slug, author_name, spec.pay_to)
        tool = db.exec(select(Tool).where(Tool.name == spec.name)).first()
        if tool is None:
            tool = Tool(
                author_id=author.id,
                name=spec.name,
                resource_url=spec.resource_url,
                description=spec.description,
                tags=",".join(spec.tags) or None,
                scheme=spec.scheme,
                network=spec.network,
                asset=spec.asset,
                asset_decimals=spec.asset_decimals,
                price_atomic=spec.price_atomic,
                max_price_atomic=spec.max_price_atomic,
                meter=spec.meter_name,
                price_per_unit_atomic=spec.price_per_unit_atomic,
                max_timeout_seconds=spec.max_timeout_seconds,
            )
            db.add(tool)
            db.flush()
            log.info("catalogued tool %s at %s atomic units", spec.name, spec.price_atomic)
        else:
            tool.resource_url = spec.resource_url
            tool.scheme = spec.scheme
            tool.network = spec.network
            tool.asset = spec.asset
            tool.asset_decimals = spec.asset_decimals
            tool.meter = spec.meter_name
            tool.updated_at = utcnow()
            db.add(tool)
            db.flush()
        return int(tool.id)


def _nonce_of(payload: Any) -> str | None:
    """The replay-defence key for an authorization.

    Shape depends on the scheme, so this reads the two that exist rather than
    assuming one:

      exact / upto        `payload.authorization.nonce` -- an EIP-3009 or Permit2
                          nonce, single-use by construction.
      batch-settlement    there is no per-call nonce; the voucher is cumulative.
                          `channelId:maxClaimableAmount` is the equivalent, because
                          the ceiling is strictly monotonic, so the same pair can
                          legitimately appear only once.

    None means "no replay key available", and the unique index tolerates it --
    several NULLs are allowed. Better an honest gap than a fabricated key that
    would make distinct calls collide.
    """
    raw = _payload_data(payload)
    if not isinstance(raw, dict):
        return None
    auth = raw.get("authorization")
    if isinstance(auth, dict) and auth.get("nonce") is not None:
        return str(auth["nonce"])[:80]
    voucher = raw.get("voucher")
    if isinstance(voucher, dict) and voucher.get("channelId"):
        return f"{voucher['channelId']}:{voucher.get('maxClaimableAmount', '')}"[:80]
    permit = raw.get("permit2Authorization") or raw.get("permit2") or raw.get("permit")
    if isinstance(permit, dict) and permit.get("nonce") is not None:
        return str(permit["nonce"])[:80]
    return None


def _begin_call(
    spec: PriceSpec,
    tool_id: int,
    payer: str,
    authorized_atomic: int,
    nonce: str | None,
    agent_label: str | None,
) -> dict[str, Any]:
    """Open (or reuse) the session and write the `call` row in VERIFIED state.

    Returns a plain dict; nothing ORM-shaped leaves the thread.
    """
    with batching.ledger_write() as db:
        session = batching.open_or_reuse_session(
            db,
            payer=payer,
            network=spec.network,
            asset=spec.asset,
            scheme=spec.scheme,
            asset_decimals=spec.asset_decimals,
            agent_label=agent_label,
        )
        tool = db.get(Tool, tool_id)

        decline = _admit(session, tool, authorized_atomic)

        call = Call(
            call_id=_call_id(),
            session_id=session.id,
            tool_id=tool_id,
            payer=payer,
            # The address the agent actually signed to. NOT the author's payout
            # address, which may differ: an author is paid from the platform's
            # ledger, and recording a payee here that the buyer never authorized
            # would make the receipt unverifiable against the chain.
            pay_to=spec.pay_to,
            network=spec.network,
            asset=spec.asset,
            scheme=spec.scheme,
            authorized_atomic=authorized_atomic,
            captured_atomic=0,
            platform_fee_atomic=0,
            author_net_atomic=0,
            meter=spec.meter_name,
            status=CallStatus.DECLINED if decline else CallStatus.VERIFIED,
            decline_reason=decline,
            nonce=nonce,
        )
        db.add(call)
        try:
            db.flush()
        except IntegrityError:
            # The (network, nonce) unique index fired: this authorization has been
            # presented before. Roll back and re-record as a decline, with no
            # nonce, so the replay is visible in the ledger instead of vanishing.
            db.rollback()
            session = batching.open_or_reuse_session(
                db,
                payer=payer,
                network=spec.network,
                asset=spec.asset,
                scheme=spec.scheme,
                asset_decimals=spec.asset_decimals,
                agent_label=agent_label,
            )
            call = Call(
                call_id=_call_id(),
                session_id=session.id,
                tool_id=tool_id,
                payer=payer,
                pay_to=spec.pay_to,
                network=spec.network,
                asset=spec.asset,
                scheme=spec.scheme,
                authorized_atomic=authorized_atomic,
                status=CallStatus.DECLINED,
                decline_reason="replayed_nonce",
                nonce=None,
            )
            db.add(call)
            db.flush()
            decline = "replayed_nonce"

        if decline:
            session.declined_count += 1
            db.add(session)
            db.flush()

        return {
            "session_pk": int(session.id),
            "session_id": session.session_id,
            "call_pk": int(call.id),
            "call_id": call.call_id,
            "declined": decline,
        }


def _admit(session: PaySession, tool: Tool | None, authorized_atomic: int) -> str | None:
    """Seller-side admission. Returns a `decline_reason`, or None to proceed.

    NOT a buyer-side spend Guardian: by the time this runs the agent has already
    signed. These are the seller's own reasons to refuse -- a disabled tool, a
    frozen session, a session that has already spent its budget. A buyer deciding
    whether to sign at all is `app.guardian`, deferred to below when present.
    """
    if tool is None:
        return "unknown_tool"
    if not tool.enabled:
        return "tool_disabled"
    # `==`, never `is`. The status columns are String(32) (see app/models.py), and
    # SQLModel hands the raw string back on read -- so `session.status is
    # SessionStatus.OPEN` is False for every row that came from the database and
    # every single call would be declined. StrEnum makes `==` work both ways.
    # Pinned by test_pay_flow.py::test_enum_columns_come_back_as_plain_strings.
    if session.status != SessionStatus.OPEN:
        return "session_not_open"
    if session.budget_atomic is not None:
        if session.captured_atomic + authorized_atomic > session.budget_atomic:
            return "over_session_budget"

    guardian_gate = _guardian_gate()
    if guardian_gate is not None:
        try:
            verdict = guardian_gate(session=session, tool=tool, amount_atomic=authorized_atomic)
        except Exception:  # noqa: BLE001
            log.exception("guardian gate raised; failing closed")
            return "guardian_error"
        if verdict:
            return str(verdict)[:64]
    return None


_guardian_looked_up = False
_guardian_fn: Callable[..., Any] | None = None


def _guardian_gate() -> Callable[..., Any] | None:
    """Optional seam for `app.guardian`. Looked up once, absence is normal."""
    global _guardian_looked_up, _guardian_fn
    if not _guardian_looked_up:
        _guardian_looked_up = True
        try:
            from app.guardian import seller_gate  # type: ignore[attr-defined]

            _guardian_fn = seller_gate
            log.info("app.guardian.seller_gate wired into the admission check")
        except Exception:  # noqa: BLE001
            _guardian_fn = None
    return _guardian_fn


def _decline(call_pk: int, reason: str) -> None:
    with batching.ledger_write() as db:
        call = db.get(Call, call_pk)
        if call is None:
            return
        call.status = CallStatus.DECLINED
        call.decline_reason = reason[:64]
        db.add(call)


def _fail(call_pk: int, error: str) -> None:
    with batching.ledger_write() as db:
        call = db.get(Call, call_pk)
        if call is None:
            return
        call.status = CallStatus.FAILED
        call.error = error[:512]
        db.add(call)


def _capture_call(
    call_pk: int,
    reading: MeterReading,
    *,
    verify_ms: int | None,
    execute_ms: int | None,
    settle_ms: int | None,
    tx_hash: str | None,
    attestation: str | None,
    settlement: SettlementMode,
) -> dict[str, Any]:
    """Record the capture, split the revenue, issue the receipt.

    Runs after the facilitator has confirmed settlement. `tx_hash` is empty for a
    batched call -- see `app/pay/batching.py`: under `batch-settlement` the SDK's
    own `handle_before_settle` returns `SkipSettleResult` with `transaction=""`,
    because the voucher has been raised and nothing has gone on-chain yet.
    """
    with batching.ledger_write() as db:
        call = db.get(Call, call_pk)
        if call is None:
            raise LookupError(f"call {call_pk} vanished before capture")
        session = db.get(PaySession, call.session_id)
        tool = db.get(Tool, call.tool_id)
        author = db.get(Author, tool.author_id) if tool is not None else None

        platform, author_net = batching.apply_take(reading.captured_atomic, tool, author)

        call.captured_atomic = reading.captured_atomic
        call.platform_fee_atomic = platform
        call.author_net_atomic = author_net
        call.meter = reading.meter
        call.meter_units = reading.units
        call.verify_ms = verify_ms
        call.execute_ms = execute_ms
        call.settle_ms = settle_ms
        call.executed_at = utcnow()
        # SETTLED only when a transaction actually exists. A batched call is
        # CAPTURED: real money is owed against a signed voucher, and no chain has
        # seen it yet. Conflating the two would be the whole submission's lie.
        if tx_hash:
            call.status = CallStatus.SETTLED
            call.settled_at = utcnow()
        else:
            call.status = CallStatus.CAPTURED
        db.add(call)

        session.captured_atomic += reading.captured_atomic
        session.call_count += 1
        session.last_call_at = utcnow()
        if tx_hash:
            session.settled_atomic += reading.captured_atomic
        if session.budget_atomic is not None and session.captured_atomic >= session.budget_atomic:
            # Budget spent. Freeze rather than close: what was consumed still
            # settles honestly, but nothing more is admitted.
            session.status = SessionStatus.FROZEN
        db.add(session)

        if tool is not None:
            tool.total_calls += 1
            tool.total_captured_atomic += reading.captured_atomic
            db.add(tool)

        db.flush()
        receipt = receipts_mod.issue(
            db,
            call=call,
            session=session,
            tool=tool,
            settlement=settlement,
            tx_hash=tx_hash or None,
            attestation=attestation,
            meter_reading=reading.as_dict(),
        )
        body = json.loads(receipt.body_json)
        body["bodyHash"] = receipt.body_hash
        return body


# --------------------------------------------------------------------------
# The settlement-amount seam
# --------------------------------------------------------------------------


class _MeteredResourceServer:
    """Proxy around `x402ResourceServer` that settles the METERED amount.

    Implements only the three attributes `create_payment_wrapper` uses --
    `find_matching_requirements`, `verify_payment`, `settle_payment` -- and
    forwards all three. The single behavioural difference is that
    `settle_payment` swaps in the amount the meter arrived at, exactly as the
    SDK's HTTP server does for `Settlement-Overrides`.

    One instance per decorated tool, so the spec is unambiguous without inspecting
    the payload.
    """

    def __init__(self, spec: PriceSpec) -> None:
        self._spec = spec

    # -- plumbing ----------------------------------------------------------

    @property
    def _inner(self) -> Any:
        return gateway.get_gateway().resource_server

    def find_matching_requirements(self, available: list[Any], payload: Any) -> Any:
        return self._inner.find_matching_requirements(available, payload)

    # -- verify ------------------------------------------------------------

    async def verify_payment(self, payload: Any, requirements: Any, *args: Any, **kw: Any) -> Any:
        """Verify with the facilitator, then open the ledger entry for this call.

        `async def` on purpose: `create_payment_wrapper` checks
        `asyncio.iscoroutinefunction` and falls back to `asyncio.to_thread` for a
        sync one, which would run in a different context and lose `_CURRENT`.
        """
        gw = gateway.get_gateway()
        if not await gw.ensure_ready():
            from x402.schemas import ResourceVerifyResponse, VerifyResponse

            return ResourceVerifyResponse(
                verify=VerifyResponse(
                    is_valid=False,
                    invalid_reason="facilitator_unavailable",
                    invalid_message=(
                        f"cannot reach the x402 facilitator at {settings.facilitator_url}: "
                        f"{gw.last_error}"
                    ),
                )
            )

        started = time.monotonic_ns()
        result = await self._inner.verify_payment(payload, requirements, *args, **kw)
        verify_ms = (time.monotonic_ns() - started) // 1_000_000

        if not result.is_valid:
            return result

        payer = result.payer or _payer_of(payload)
        if not payer and self._spec.scheme == Scheme.UPTO:
            from x402.schemas import ResourceVerifyResponse, VerifyResponse

            return ResourceVerifyResponse(
                verify=VerifyResponse(
                    is_valid=False,
                    invalid_reason="payer_identity_missing",
                    invalid_message=(
                        "verified upto payload has no payer in the facilitator "
                        "response or permit2Authorization.from; refusing to merge "
                        "callers into an unauditable shared session"
                    ),
                )
            )
        payer = payer or "0x?"
        authorized = int(requirements.amount)
        record = _InFlight(
            spec=self._spec,
            tool_id=await asyncio.to_thread(_tool_id_for, self._spec),
            payer=payer,
            authorized_atomic=authorized,
            verify_ms=int(verify_ms),
        )

        opened = await asyncio.to_thread(
            _begin_call,
            self._spec,
            record.tool_id,
            payer,
            authorized,
            _nonce_of(payload),
            _agent_label_of(payload),
        )
        record.session_pk = opened["session_pk"]
        record.session_id = opened["session_id"]
        record.call_pk = opened["call_pk"]
        record.call_id = opened["call_id"]
        record.declined = opened["declined"]
        _CURRENT.set(record)

        if record.declined:
            # Refuse before execution AND before settlement. A declined call is
            # never settled, so the buyer is never charged for a refusal.
            from x402.schemas import ResourceVerifyResponse, VerifyResponse

            return ResourceVerifyResponse(
                verify=VerifyResponse(
                    is_valid=False,
                    invalid_reason=record.declined,
                    invalid_message=f"declined by the gateway: {record.declined}",
                    payer=payer,
                )
            )
        return result

    # -- settle ------------------------------------------------------------

    async def settle_payment(self, payload: Any, requirements: Any, *args: Any, **kw: Any) -> Any:
        """Settle the metered amount, then write the capture and the receipt."""
        record = _current()
        effective = requirements

        if record is None or record.reading is None:
            # No metering happened: either the context was lost or the handler
            # never ran. Settle exactly what was advertised -- under-billing on a
            # bug is recoverable, over-billing is not -- and say so loudly.
            log.warning(
                "settling %s at the advertised amount: no meter reading in context",
                self._spec.name,
            )
        else:
            captured = record.reading.captured_atomic
            supports_partial_capture = str(requirements.scheme) in {
                str(Scheme.UPTO),
                str(Scheme.BATCH_SETTLEMENT),
            }
            if supports_partial_capture and captured != int(requirements.amount):
                # The SDK's own settlement-override move, on the MCP transport.
                effective = requirements.model_copy(update={"amount": wire_amount(captured)})

        started = time.monotonic_ns()
        result = await self._inner.settle_payment(payload, effective, *args, **kw)
        settle_ms = (time.monotonic_ns() - started) // 1_000_000

        if record is None:
            return result
        if not getattr(result, "success", False):
            if record.call_pk is not None:
                await asyncio.to_thread(
                    _fail, record.call_pk, f"settlement failed: {result.error_reason}"
                )
            return result

        reading = record.reading or capture(
            self._spec, ExecutionOutcome(result=None, result_text="", elapsed_ms=0)
        )
        # An upto tool may deliberately fall back to an exact offer while the
        # facilitator is unavailable. EIP-3009 exact cannot capture less than
        # the signed amount, so the receipt must report the full exact amount
        # rather than the lower metered reading.
        if str(requirements.scheme) not in {
            str(Scheme.UPTO),
            str(Scheme.BATCH_SETTLEMENT),
        }:
            exact_amount = int(requirements.amount)
            if reading.captured_atomic != exact_amount:
                reading = MeterReading(
                    meter=reading.meter,
                    units=reading.units,
                    computed_atomic=reading.computed_atomic,
                    captured_atomic=exact_amount,
                    authorized_atomic=exact_amount,
                    capped=reading.computed_atomic > exact_amount,
                )

        reported_amount = getattr(result, "amount", None)
        if reported_amount not in (None, ""):
            try:
                settled_amount = int(reported_amount)
            except (TypeError, ValueError):
                settled_amount = -1
            if settled_amount != reading.captured_atomic:
                detail = (
                    f"facilitator reported capture {reported_amount!r}; gateway "
                    f"authorized {record.authorized_atomic} and requested "
                    f"{reading.captured_atomic}"
                )
                if record.call_pk is not None:
                    await asyncio.to_thread(_fail, record.call_pk, detail)
                log.critical("settlement amount mismatch for %s: %s", record.call_id, detail)
                try:
                    return result.model_copy(
                        update={
                            "success": False,
                            "error_reason": "settlement_amount_mismatch",
                            "error_message": detail[:400],
                        }
                    )
                except Exception:  # pragma: no cover - defensive SDK fallback
                    return result

        tx_hash = (getattr(result, "transaction", "") or "").strip()
        # An empty `transaction` is the batch-settlement tell: the voucher was
        # raised, nothing went on-chain. See handle_before_settle in the SDK.
        settlement = SettlementMode.PER_CALL if tx_hash else SettlementMode.BATCHED

        try:
            body = await asyncio.to_thread(
                _capture_call,
                record.call_pk,
                reading,
                verify_ms=record.verify_ms,
                execute_ms=record.execute_ms,
                settle_ms=int(settle_ms),
                tx_hash=tx_hash or None,
                attestation=_attestation_of(result),
                settlement=settlement,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "capture failed for call %s after a successful settlement", record.call_id
            )
            return result

        record.receipt_body = body
        # The receipt rides back to the agent inside the settlement response, which
        # `create_payment_wrapper` puts in the MCP `_meta` key
        # `x402/payment-response`. `extra` is the protocol's own extension point,
        # so no private channel is invented for it.
        extra = dict(getattr(result, "extra", None) or {})
        extra["receipt"] = body
        extra["brainwave"] = {
            "sessionId": record.session_id,
            "callId": record.call_id,
            "settlement": str(settlement),
            "meter": reading.as_dict(),
        }
        try:
            result.extra = extra
        except Exception:  # noqa: BLE001 -- never lose a settlement over metadata
            log.warning("could not attach the receipt to the settlement response")
        return result


def _payer_of(payload: Any) -> str | None:
    raw = _payload_data(payload)
    if not isinstance(raw, dict):
        return None
    auth = raw.get("authorization")
    if isinstance(auth, dict):
        return auth.get("from") or auth.get("owner")
    permit = raw.get("permit2Authorization") or raw.get("permit2") or raw.get("permit")
    if isinstance(permit, dict):
        payer = permit.get("from") or permit.get("owner")
        if isinstance(payer, str) and payer:
            return payer
    config = raw.get("channelConfig")
    if isinstance(config, dict):
        return config.get("payer")
    return None


def _payload_data(payload: Any) -> dict[str, Any]:
    """Scheme-specific inner payload from a model, dict, or JSON string."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return {}
    if isinstance(payload, dict):
        inner = payload.get("payload", payload)
        return inner if isinstance(inner, dict) else {}
    raw = getattr(payload, "payload", None)
    return raw if isinstance(raw, dict) else {}


def _agent_label_of(payload: Any) -> str | None:
    """An agent may name itself in `PaymentPayload.extensions`. Cosmetic, unverified."""
    ext = getattr(payload, "extensions", None)
    if isinstance(ext, dict):
        label = ext.get("agentLabel") or ext.get("agent_label")
        if isinstance(label, str):
            return label[:128]
    return None


def _attestation_of(settle_result: Any) -> str | None:
    """The facilitator's own words about this settlement, verbatim and truncated.

    Not a signature -- the public facilitator does not return one -- so `verify()`
    grades it as "facilitator" strength, below "chain" and above "local".
    """
    try:
        dumped = settle_result.model_dump(by_alias=True, exclude_none=True)
    except Exception:  # noqa: BLE001
        return None
    keep = {k: v for k, v in dumped.items() if k in ("transaction", "network", "payer", "amount")}
    return json.dumps(keep, sort_keys=True)[:256] if keep else None


# --------------------------------------------------------------------------
# Tool registration
# --------------------------------------------------------------------------


@dataclass
class PaidTool:
    """A registered priced tool: the declaration, and the row it maps to."""

    spec: PriceSpec
    author_slug: str
    author_name: str
    handler_name: str
    tool_id: int | None = None


#: Everything `@paid()` has decorated in this process. The catalogue and the
#: dashboard read it; `sync_catalogue()` pushes it into the database.
registry: dict[str, PaidTool] = {}


def _tool_id_for(spec: PriceSpec) -> int:
    """Primary key of the tool row, creating it on first use.

    Lazy because decoration happens at import time, when Alembic may not have run
    yet and the tables may not exist. The first paid call is late enough that the
    schema is certainly there.
    """
    entry = registry.get(spec.name)
    if entry is None:
        raise LookupError(f"{spec.name} is not in the paid registry")
    if entry.tool_id is None:
        entry.tool_id = _upsert_tool(spec, entry.author_slug, entry.author_name)
    return entry.tool_id


def sync_catalogue() -> int:
    """Write every declared tool into the ledger. Safe to call at startup."""
    count = 0
    for entry in registry.values():
        try:
            entry.tool_id = _upsert_tool(entry.spec, entry.author_slug, entry.author_name)
            count += 1
        except Exception:  # noqa: BLE001
            log.exception("could not catalogue %s", entry.spec.name)
    return count


# --------------------------------------------------------------------------
# @paid
# --------------------------------------------------------------------------


def paid(
    price: str | int,
    *,
    scheme: Scheme | str = Scheme.EXACT,
    max_price: str | int | None = None,
    meter: str | None = None,
    price_per_unit: str | int | None = None,
    network: str | None = None,
    asset: str | None = None,
    asset_decimals: int | None = None,
    pay_to: str | None = None,
    name: str | None = None,
    description: str | None = None,
    tags: tuple[str, ...] | list[str] = (),
    author: str = HOUSE_AUTHOR_SLUG,
    author_name: str | None = None,
    max_timeout_seconds: int | None = None,
    extra: dict[str, Any] | None = None,
    input_schema: dict[str, Any] | None = None,
    example: dict[str, Any] | None = None,
    upto_fallback_exact: bool = False,
    initialize_for_challenge: bool = True,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Price, meter and settle an MCP tool.

    Usage -- note the order, which is not negotiable::

        @mcp.tool(name="run_injection_attack_sim")
        @paid("$0.05", scheme="upto", max_price="$0.50",
              meter="seconds", price_per_unit="$0.01")
        async def run_injection_attack_sim(target: str) -> dict: ...

    `@mcp.tool` OUTSIDE, `@paid` INSIDE. Reversed, FastMCP registers the unpaid
    function and every call is free.

    Do NOT declare a `ctx: Context` parameter. `create_payment_wrapper` appends a
    synthetic one to the signature so FastMCP's `find_context_parameter()` injects
    the request context -- that is the only route the `x402/payment` metadata takes
    -- and a hand-declared one collides with it.

    A tool may report its own consumption by returning a dict containing
    ``{"_meter": {"units": N}}``. The key is stripped before the result reaches the
    agent. Without it a `tokens` meter bills zero units rather than an estimate.
    """
    spec_scheme = Scheme(scheme) if not isinstance(scheme, Scheme) else scheme

    def decorate(handler: Callable[..., Any]) -> Callable[..., Any]:
        tool_name = name or handler.__name__
        spec = PriceSpec.declare(
            tool_name,
            price=price,
            scheme=spec_scheme,
            max_price=max_price,
            meter=meter,
            price_per_unit=price_per_unit,
            network=network or settings.x402_network,
            asset=asset or settings.asset_address,
            asset_decimals=(
                asset_decimals if asset_decimals is not None else settings.x402_asset_decimals
            ),
            pay_to=pay_to or settings.pay_to_address,
            description=description
            or (inspect.getdoc(handler) or "").strip().split("\n")[0]
            or None,
            tags=tags,
            max_timeout_seconds=max_timeout_seconds or settings.payment_timeout_seconds,
            extra=extra,
        )
        if not spec.asset:
            raise PriceError(
                f"{tool_name}: no asset address for network {spec.network}. "
                "Set X402_ASSET, or use a network x402 has a default stablecoin for."
            )

        registry[tool_name] = PaidTool(
            spec=spec,
            author_slug=author,
            author_name=author_name or author.replace("-", " ").title(),
            handler_name=handler.__name__,
        )

        # ONE requirements object, built once and shared, because
        # `find_matching_requirements` returns the element from this list and the
        # client echoes it back for matching. Enhanced in place on first use, when
        # the facilitator has told us what `upto` and `batch-settlement` need.
        preferred = payment_requirements(spec)
        if upto_fallback_exact and spec.scheme == Scheme.UPTO:
            accepts = [
                preferred.model_copy(
                    update={
                        "scheme": str(Scheme.EXACT),
                        "amount": wire_amount(spec.authorized_atomic),
                    }
                )
            ]
        else:
            accepts = [preferred]
        enhanced = False

        metered_server = _MeteredResourceServer(spec)

        # --- our handler wrapper: timing, `_meter`, and the meter reading -----
        is_async = asyncio.iscoroutinefunction(handler)

        @functools.wraps(handler)
        async def metered_handler(**kwargs: Any) -> Any:
            record = _current()
            started = time.monotonic_ns()
            try:
                result = await handler(**kwargs) if is_async else handler(**kwargs)
            except Exception as exc:
                if record is not None and record.call_pk is not None:
                    await asyncio.to_thread(_fail, record.call_pk, f"{type(exc).__name__}: {exc}")
                # Re-raise: `create_payment_wrapper` turns this into an isError
                # result and, crucially, never calls settle. A tool that threw is
                # not billed.
                raise
            elapsed_ms = (time.monotonic_ns() - started) // 1_000_000

            units = declared_units(result)
            clean = strip_meter_declaration(result)
            text = clean if isinstance(clean, str) else json.dumps(clean, default=str)

            if record is not None:
                record.execute_ms = int(elapsed_ms)
                record.reading = capture(
                    spec,
                    ExecutionOutcome(
                        result=clean,
                        result_text=text,
                        elapsed_ms=int(elapsed_ms),
                        declared=units,
                    ),
                )
            return clean

        # The SDK inspects the handler's signature to build the tool schema, so the
        # synthetic ctx parameter it adds must be appended to the ORIGINAL
        # signature -- `functools.wraps` already copied it, and nothing here may
        # rebuild `__signature__` afterwards.
        wrapper_factory = _make_wrapper(
            metered_server,
            accepts,
            spec,
            input_schema=input_schema,
            example=example,
        )
        wrapped = wrapper_factory(metered_handler)

        @functools.wraps(wrapped)
        async def entry(*args: Any, **kwargs: Any) -> Any:
            nonlocal enhanced
            ctx = kwargs.get("ctx")
            should_initialize = initialize_for_challenge or _payment_from_context(ctx) is not None
            if not enhanced and should_initialize:
                gw = gateway.get_gateway()
                if await gw.ensure_ready():
                    if upto_fallback_exact and spec.scheme == Scheme.UPTO:
                        metered = payment_requirements(spec)
                        metered = gateway.enhance(metered, spec.scheme, gateway=gw)
                        if gw.supported_kind(spec.scheme) is not None:
                            accepts.insert(0, metered)
                    else:
                        accepts[0] = gateway.enhance(accepts[0], spec.scheme, gateway=gw)
                    enhanced = True
            return await wrapped(*args, **kwargs)

        # `entry` must present the signature `wrapped` was given -- the synthetic
        # `ctx: Context` included -- or FastMCP will not inject the context and no
        # payment metadata will ever arrive. functools.wraps copies
        # `__wrapped__`, and inspect.signature follows it, but being explicit here
        # is cheap and the failure mode is silent.
        entry.__signature__ = inspect.signature(wrapped)  # type: ignore[attr-defined]
        entry.__annotations__ = dict(getattr(wrapped, "__annotations__", {}))
        entry.__doc__ = handler.__doc__
        return entry

    return decorate


def _make_wrapper(
    resource_server: Any,
    accepts: list[Any],
    spec: PriceSpec,
    *,
    input_schema: dict[str, Any] | None = None,
    example: dict[str, Any] | None = None,
) -> Callable:
    """`create_payment_wrapper`, with our hooks. The protocol work is all the SDK's."""
    from x402.mcp import PaymentWrapperHooks, create_payment_wrapper

    async def on_before_execution(_ctx: Any) -> bool:
        """Last chance to refuse, after verification and before the tool runs.

        Almost always True: a decline is decided in `verify_payment`, which is
        earlier and therefore cheaper, and returning False here would still have
        cost a facilitator round trip. Kept because it is the only hook that can
        stop execution once the payload is known-good, which is where a buyer-side
        Guardian veto belongs.
        """
        record = _current()
        if record is None:
            return True
        if record.declined:
            return False
        return True

    async def on_after_execution(_ctx: Any) -> None:
        """Metering already happened in `metered_handler`; this is the audit line."""
        record = _current()
        if record is None or record.reading is None:
            log.debug("no meter reading after executing %s", spec.name)
            return
        if record.reading.capped:
            log.info(
                "%s hit its ceiling: computed %s, captured %s (agent authorized %s)",
                spec.name,
                record.reading.computed_atomic,
                record.reading.captured_atomic,
                record.reading.authorized_atomic,
            )

    extensions = None
    if input_schema is not None:
        from app.gateway.requirements import discovery_extension

        extensions = discovery_extension(
            tool_name=spec.name,
            description=spec.description or f"Tool: {spec.name}",
            input_schema=input_schema,
            example=example,
        )

    return create_payment_wrapper(
        resource_server,
        accepts=accepts,
        resource=resource_info(spec),
        extensions=extensions,
        hooks=PaymentWrapperHooks(
            on_before_execution=on_before_execution,
            on_after_execution=on_after_execution,
        ),
    )


def _payment_from_context(ctx: Any) -> Any:
    """The exact metadata slot the SDK reads, without consuming it."""
    if ctx is None:
        return None
    try:
        meta = ctx.request_context.meta
        extra = meta.model_extra if meta is not None else None
        return extra.get(MCP_PAYMENT_META_KEY) if isinstance(extra, dict) else None
    except (AttributeError, ValueError):
        return None
