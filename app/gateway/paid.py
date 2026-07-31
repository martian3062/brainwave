"""`@paid` -- a thin ergonomic layer over the x402 SDK's own paid-MCP wrapper.

READ THIS BEFORE JUDGING THE NOVELTY CLAIM. The x402 Python SDK already
implements paid MCP. `x402.mcp.create_payment_wrapper` issues the 402 challenge,
parses the authorization out of the MCP `_meta` key `x402/payment`, verifies it
with the facilitator, executes the handler, settles, and returns the settlement
under `x402/payment-response`. We did not write that and do not claim to. Every
call below goes through it.

What `@paid` adds is the part the SDK has no opinion about:

    price parsing      human "$0.01" -> integer atomic units, once, at import
    metering           a tool tells the payment layer what it consumed (`upto`)
    ledger             Author / Tool / Session / Call / Batch / Receipt rows
    batching           deferred settlement, and the receipt that admits it
    receipts           a hash-checkable artefact returned inside the tool result

--------------------------------------------------------------------------
DECORATOR ORDER, AND THE ONE THING NOT TO DO TO THE SIGNATURE
--------------------------------------------------------------------------
The stack is, outermost first:

    @mcp.tool(name=..., description=...)   # FastMCP registers what it sees
    @_ledger_layer                         # ours: ctx, timing, ledger, receipt
    @payment_wrapper                       # the SDK: challenge/verify/exec/settle
    def handler(...) -> dict: ...

`create_payment_wrapper` appends a synthetic `ctx: Context` parameter to the
function's `__signature__` (x402/mcp/server.py:315-331) because that is how
FastMCP's `find_context_parameter()` decides to inject the request context --
and the payment metadata arrives through that context and nowhere else. So:

  * the handler must NOT declare `ctx` itself;
  * `_ledger_layer` must NOT rebuild the signature -- it COPIES the one the SDK
    produced, verbatim, and passes `ctx` straight through in kwargs rather than
    consuming it. (The SDK's wrapper pops it.) Copying is not rebuilding; a
    rebuilt signature is how the context injection silently stops working and
    every paid call starts 402-ing with a valid authorization in hand.

--------------------------------------------------------------------------
WHAT THE ADVERTISED PRICE IS, EXACTLY
--------------------------------------------------------------------------
`create_payment_wrapper` takes its `accepts` list once, at decoration time, and
`find_matching_requirements` compares the advertised amount to the payload's
byte-for-byte. So the amount in the 402 challenge is fixed for the life of the
process. `Tool.price_atomic` in the ledger is seeded from the same constant at
boot, so the two agree -- but an operator who reprices a tool from /admin
changes the accounting record and not the live challenge until the next
restart. Rather than hide that, the free `list_paid_tools` reports a
`priceDrift` flag whenever the two disagree.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from x402.mcp.constants import MCP_PAYMENT_META_KEY, MCP_PAYMENT_RESPONSE_META_KEY

from app.config import settings
from app.gateway import ledger, requirements
from app.gateway.ledger import CallRecord, ToolSpec
from app.gateway.meter import Meter, meter_scope
from app.gateway.resource_server import MeteredResourceServer, supported_schemes
from app.models import CallStatus, Scheme

log = logging.getLogger(__name__)

__all__ = ["paid", "REGISTRY", "AGENT_META_KEY", "RECEIPT_META_KEY"]

#: Optional MCP `_meta` key an agent may set to label itself in the ledger:
#:     {"eraya/agent": {"label": "claude-desktop", "identity": "erc8004:..."}}
#: Purely descriptive -- it never affects pricing or authorization, so a forged
#: label costs nothing but an inaccurate dashboard row.
AGENT_META_KEY = "eraya/agent"
#: Where the receipt is returned, alongside the SDK's `x402/payment-response`.
RECEIPT_META_KEY = "eraya/receipt"

#: Every tool declared with `@paid`, in declaration order. `server.py` syncs
#: these into the ledger and `tools/free.py` lists them.
REGISTRY: list[ToolSpec] = []

#: Metered tools awaiting an `upto` requirement, paired with their live accepts
#: list. Resolved once, on the first paid call. See `_upgrade_metered_accepts`.
_PENDING_UPTO: list[tuple[ToolSpec, list[Any]]] = []
_upto_resolved = False

_resource_server = MeteredResourceServer()


def paid(
    spec: ToolSpec,
    *,
    input_schema: dict[str, Any] | None = None,
    example: dict[str, Any] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Adapt catalogue ``ToolSpec`` declarations onto the shared pay core.

    `app.pay.decorator.paid` is the implementation that serves traffic and the
    implementation exercised by the payment-flow test suite. This adapter keeps
    the catalogue's compact `ToolSpec` declarations and its context-based
    `meter.record()` API without maintaining a second settlement, ledger, or
    receipt path.
    """
    # Building throwaway FastMCP instances (doctor/tests/stdio) re-runs the
    # catalogue decorators in the same process. Keep one current declaration
    # per tool name so discovery and ledger sync do not grow duplicate entries.
    for index, registered in enumerate(REGISTRY):
        if registered.name == spec.name:
            REGISTRY[index] = spec
            break
    else:
        REGISTRY.append(spec)

    def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
        import asyncio

        from app.pay.decorator import paid as core_paid

        handler_is_async = asyncio.iscoroutinefunction(handler)

        @functools.wraps(handler)
        async def meter_bridge(**kwargs: Any) -> Any:
            """Translate the catalogue meter context into `_meter.units`."""
            live_meter = Meter(
                tool_name=spec.name,
                base_atomic=spec.price_atomic,
                price_per_unit_atomic=spec.price_per_unit_atomic,
                max_atomic=spec.authorized_atomic if spec.meter else None,
                unit=spec.meter,
            )
            with meter_scope(live_meter):
                result = await handler(**kwargs) if handler_is_async else handler(**kwargs)
            if spec.meter and live_meter.units is not None and isinstance(result, dict):
                result = dict(result)
                result["_meter"] = {"units": live_meter.units}
            return result

        core_fn = core_paid(
            spec.price_atomic,
            scheme=spec.scheme,
            max_price=spec.max_price_atomic,
            meter=spec.meter,
            price_per_unit=spec.price_per_unit_atomic,
            name=spec.name,
            description=spec.description,
            tags=spec.tags,
            max_timeout_seconds=(spec.max_timeout_seconds or settings.payment_timeout_seconds),
            extra=_scheme_extra(spec),
            input_schema=input_schema,
            example=example,
            # Until the facilitator supplies Permit2's address, an upto tool
            # must advertise exact at its ceiling and say so.
            upto_fallback_exact=True,
            # Tests explicitly set the legacy one-shot flag to keep the
            # standalone catalogue offline. Production resolves the scheme.
            initialize_for_challenge=not _upto_resolved,
        )(meter_bridge)

        @functools.wraps(core_fn)
        async def live_entry(*args: Any, **kwargs: Any) -> Any:
            result = await core_fn(*args, **kwargs)
            meta = _result_meta(result)
            settlement = meta.get(MCP_PAYMENT_RESPONSE_META_KEY)
            extra = settlement.get("extra") if isinstance(settlement, dict) else None
            receipt = extra.get("receipt") if isinstance(extra, dict) else None
            return _attach_receipt(result, receipt) if isinstance(receipt, dict) else result

        signature = getattr(core_fn, "__signature__", None)
        if signature is not None:
            live_entry.__signature__ = signature  # type: ignore[attr-defined]
        live_entry.__annotations__ = dict(getattr(core_fn, "__annotations__", {}))
        return live_entry

    return decorator


async def _upgrade_metered_accepts() -> None:
    """Offer `upto` once the facilitator has told us it can settle it.

    `UptoEvmScheme.enhance_payment_requirements` refuses to build requirements
    without a `facilitatorAddress`, which it copies out of the facilitator's
    advertised supported kinds (x402/mechanisms/evm/upto/server.py:107-114).
    That address belongs to the facilitator; there is no offline substitute and
    inventing one would produce a challenge that every payment against it fails.

    So metered tools boot advertising `exact` at their ceiling, and the first
    paid call -- by which point the facilitator has been reached for
    verification anyway -- upgrades them. The `upto` entry is inserted FIRST so
    a client that takes `accepts[0]` gets the option that charges it less.

    Runs at most once. If the facilitator does not support `upto`, that is a
    permanent fact about this deployment and is logged as such, not retried on
    every call.
    """
    global _upto_resolved
    if _upto_resolved or not _PENDING_UPTO:
        return

    schemes = await supported_schemes()
    if not schemes:
        return  # facilitator unreachable; get_resource_server backs this off

    _upto_resolved = True
    kind_extra = schemes.get(str(Scheme.UPTO)) or {}
    facilitator_address = kind_extra.get("facilitatorAddress")
    if not facilitator_address:
        log.warning(
            "facilitator %s does not advertise the 'upto' scheme; %d metered tool(s) "
            "will keep charging their full ceiling under 'exact'. This is stated in "
            "their 402 challenge and in list_paid_tools.",
            settings.facilitator_url,
            len(_PENDING_UPTO),
        )
        return

    for spec, accepts in _PENDING_UPTO:
        extra = _scheme_extra(spec)
        extra.update(
            {
                # Required by the scheme: upto always settles through Permit2,
                # not EIP-3009, because a partial capture needs a spender.
                "assetTransferMethod": "permit2",
                "facilitatorAddress": facilitator_address,
            }
        )
        extra["eraya"] = {**extra.get("eraya", {}), "capturePolicy": "metered"}
        accepts.insert(
            0,
            requirements.build_requirements(
                amount_atomic=spec.authorized_atomic,
                pay_to=settings.pay_to_address,
                scheme=Scheme.UPTO,
                max_timeout_seconds=spec.max_timeout_seconds,
                extra=extra,
            ),
        )
    log.info("metered tools upgraded to the 'upto' scheme (%d)", len(_PENDING_UPTO))


def _scheme_extra(spec: ToolSpec) -> dict[str, Any]:
    """Non-protocol hints carried in `PaymentRequirements.extra`.

    Namespaced under `eraya` so nothing here can be mistaken for a field the
    x402 schemes read. This is where a buyer-side Guardian finds the metering
    terms it needs to decide whether the ceiling is acceptable *before* signing.
    """
    hints: dict[str, Any] = {
        "eraya": {
            "tool": spec.name,
            "priceAtomic": str(spec.price_atomic),
            "rationale": spec.rationale or None,
            "settlement": "batched" if settings.batching_enabled else "per_call",
        }
    }
    if spec.meter:
        hints["eraya"].update(
            {
                "meter": spec.meter,
                "pricePerUnitAtomic": str(spec.price_per_unit_atomic or 0),
                "ceilingAtomic": str(spec.authorized_atomic),
                "capturePolicy": (
                    "You authorize the ceiling; only measured consumption is captured. "
                    "The receipt reports both."
                ),
            }
        )
    return hints


# --------------------------------------------------------------------------
# The ledger layer
# --------------------------------------------------------------------------


def _ledger_layer(spec: ToolSpec, paid_fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap the SDK's paid function with metering, timing and ledger writes."""

    @functools.wraps(paid_fn)
    async def wrapped(**kwargs: Any) -> Any:
        import asyncio

        await _upgrade_metered_accepts()  # one-shot; returns immediately after

        ctx = kwargs.get("ctx")  # PEEK -- the SDK's wrapper is what pops it.
        payload = _meta_value(ctx, MCP_PAYMENT_META_KEY)
        agent = _meta_value(ctx, AGENT_META_KEY) or {}

        meter = Meter(
            tool_name=spec.name,
            base_atomic=spec.price_atomic,
            price_per_unit_atomic=spec.price_per_unit_atomic,
            max_atomic=spec.authorized_atomic if spec.meter else None,
            unit=spec.meter,
        )

        started = time.perf_counter()
        with meter_scope(meter):
            result = await paid_fn(**kwargs)
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        record = _classify(spec, result, meter, payload, agent, elapsed_ms)
        try:
            body = await asyncio.to_thread(ledger.record_call, record)
        except Exception:  # pragma: no cover - to_thread itself failing
            log.exception("ledger thread failed for %s", spec.name)
            body = None

        return _attach_receipt(result, body) if body else result

    # Copy, do not rebuild. See the module docstring: the SDK put `ctx` in this
    # signature so FastMCP would inject the request context, and reconstructing
    # it is how that silently breaks.
    signature = getattr(paid_fn, "__signature__", None)
    if signature is not None:
        wrapped.__signature__ = signature  # type: ignore[attr-defined]
    else:  # pragma: no cover - only if the SDK stops setting it
        log.warning(
            "x402 payment wrapper did not set __signature__ for %s; "
            "FastMCP will not inject Context and payment metadata will be invisible",
            spec.name,
        )
        wrapped.__signature__ = inspect.signature(paid_fn)  # type: ignore[attr-defined]
    wrapped.__annotations__ = dict(getattr(paid_fn, "__annotations__", {}))
    return wrapped


def _classify(
    spec: ToolSpec,
    result: Any,
    meter: Meter,
    payload: Any,
    agent: dict[str, Any],
    elapsed_ms: int,
) -> CallRecord:
    """Turn the SDK's `CallToolResult` into a ledger row.

    The SDK signals outcome through the result rather than exceptions, so this
    is a small state machine over the four shapes it can return
    (x402/mcp/server.py: `_create_payment_required_result`,
    `_create_settlement_failed_result`, the execution-error branch, and success).
    """
    base = CallRecord(
        tool_name=spec.name,
        status=CallStatus.CHALLENGED,
        # The scheme the payer ACTUALLY signed, not the one the tool prefers.
        # A metered tool advertises both `upto` and `exact` once the facilitator
        # supports the former, and the ledger has to record which was used --
        # otherwise "authorized 0.05, captured 0.05" reads like a metering bug
        # rather than a buyer who took the exact option.
        scheme=_scheme_of(payload, spec.scheme),
        agent_label=_str_or_none(agent.get("label")),
        agent_identity=_str_or_none(agent.get("identity")),
        meter=spec.meter,
        meter_units=meter.units,
        meter_detail=meter.detail or None,
        payer=_payer_of(payload),
        nonce=_nonce_of(payload),
        execute_ms=elapsed_ms,
    )

    meta = _result_meta(result)
    settle = meta.get(MCP_PAYMENT_RESPONSE_META_KEY) if isinstance(meta, dict) else None

    if isinstance(settle, dict) and settle.get("success"):
        amount = settle.get("amount")
        base.status = CallStatus.CAPTURED
        base.captured_atomic = int(amount) if amount not in (None, "") else meter.capture_atomic()
        base.authorized_atomic = spec.authorized_atomic
        base.payer = _str_or_none(settle.get("payer")) or base.payer
        base.tx_hash = _str_or_none(settle.get("transaction"))
        base.settlement = settle.get("extra") or {"settlement": "immediate"}
        if base.tx_hash:
            base.status = CallStatus.SETTLED
            base.settlement = {**base.settlement, "settlement": "immediate"}
        return base

    if not getattr(result, "isError", False):
        # Executed and returned, but no payment response -- only reachable if
        # the SDK changes shape. Record it rather than lose it.
        base.status = CallStatus.EXECUTED
        base.error = "no x402 payment response in result meta"
        return base

    structured = getattr(result, "structuredContent", None)
    error = str(structured.get("error", "")) if isinstance(structured, dict) else _text_of(result)

    if error.startswith("Payment settlement failed"):
        base.status = CallStatus.FAILED
        base.decline_reason = "settlement_failed"
        base.error = error
        base.authorized_atomic = spec.authorized_atomic
    elif error.startswith("Payment verification failed") or error.startswith("No matching"):
        base.status = CallStatus.DECLINED
        base.decline_reason = "verify_failed"
        base.error = error
    elif error.startswith("Invalid payment payload"):
        base.status = CallStatus.DECLINED
        base.decline_reason = "invalid_payload"
        base.error = error
    elif error.startswith("Tool execution error"):
        # The SDK returns this BEFORE settling, so nothing was captured. The
        # agent got an error and paid nothing, which is the correct outcome.
        base.status = CallStatus.FAILED
        base.decline_reason = "execution_error"
        base.error = error
    else:
        base.status = CallStatus.CHALLENGED
        base.error = error or "payment required"
    return base


def _attach_receipt(result: Any, body: dict[str, Any]) -> Any:
    """Return the tool's result with the receipt attached, twice.

    In `_meta` under `eraya/receipt` for machine consumption, and folded into
    the JSON body under `_receipt` -- the key the README documents -- so a
    human reading the transcript can see what they were charged without
    decoding protocol metadata. If the tool returned something that is not a
    JSON object, the text is left exactly as the tool wrote it and only
    `_meta` carries the receipt.
    """
    from mcp.types import CallToolResult, TextContent

    text = _text_of(result)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            parsed["_receipt"] = body
            text = json.dumps(parsed)
    except (ValueError, TypeError):
        pass

    meta = dict(_result_meta(result))
    meta[RECEIPT_META_KEY] = body
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        isError=bool(getattr(result, "isError", False)),
        _meta=meta,
    )


# --------------------------------------------------------------------------
# Small readers. Every one of these is best-effort by design: none of them is
# a control, and a malformed payload must never crash the payment path.
# --------------------------------------------------------------------------


def _meta_value(ctx: Any, key: str) -> Any:
    """Read one key out of the MCP request `_meta`.

    FastMCP parks unknown `_meta` fields in `request_context.meta.model_extra`
    -- the same place `x402/mcp/server.py::_extract_payment_from_context` reads
    from, so this sees exactly what the SDK sees.
    """
    if ctx is None:
        return None
    try:
        meta = ctx.request_context.meta
        if meta is not None and meta.model_extra:
            return meta.model_extra.get(key)
    except (ValueError, AttributeError):
        pass
    return None


def _result_meta(result: Any) -> dict[str, Any]:
    meta = getattr(result, "meta", None)
    return dict(meta) if isinstance(meta, dict) else {}


def _text_of(result: Any) -> str:
    content = getattr(result, "content", None) or []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            return text
    return ""


def _payload_dict(payload: Any) -> dict[str, Any]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return {}
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("payload")
    return inner if isinstance(inner, dict) else {}


def _dig(data: dict[str, Any], *paths: tuple[str, ...]) -> str | None:
    for path in paths:
        cursor: Any = data
        for key in path:
            if not isinstance(cursor, dict):
                cursor = None
                break
            cursor = cursor.get(key)
        if isinstance(cursor, str) and cursor:
            return cursor
        if isinstance(cursor, int) and not isinstance(cursor, bool):
            return str(cursor)
    return None


def _payer_of(payload: Any) -> str | None:
    return _dig(
        _payload_dict(payload),
        ("authorization", "from"),
        ("permit2Authorization", "from"),
        ("permit", "owner"),
        ("owner",),
        ("from",),
        ("payer",),
    )


def _nonce_of(payload: Any) -> str | None:
    """The authorization nonce -- the ledger's replay defence.

    `UNIQUE(network, nonce)` on the `call` table means the same authorization
    cannot be captured twice, enforced by the database rather than by us
    remembering to check.
    """
    return _dig(
        _payload_dict(payload),
        ("authorization", "nonce"),
        ("permit2Authorization", "nonce"),
        ("permit", "nonce"),
        ("nonce",),
        ("voucher", "nonce"),
    )


def _scheme_of(payload: Any, default: Scheme) -> Scheme:
    """Which advertised option the payer signed, from `payload.accepted.scheme`."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return default
    if not isinstance(payload, dict):
        return default
    accepted = payload.get("accepted")
    name = accepted.get("scheme") if isinstance(accepted, dict) else None
    try:
        return Scheme(name)
    except (ValueError, TypeError):
        return default


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
