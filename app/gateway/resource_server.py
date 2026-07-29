"""The x402 resource server, plus the two things the SDK's own server cannot do.

`create_payment_wrapper` takes an object it calls `resource_server` and uses
exactly three members of it (x402/mcp/server.py):

    find_matching_requirements(accepts, payload)   -> line 149
    verify_payment(payload, requirements)          -> line 158-163
    settle_payment(payload, requirements)          -> line 247-254

`MeteredResourceServer` implements those three and forwards everything else to a
real `x402ResourceServer`. It exists for two reasons, and neither is a
reimplementation of the protocol:

1. METERED CAPTURE (`upto`). The wrapper settles with the SAME requirements it
   verified, so the advertised ceiling is what would move. The SDK's permit2
   settle path reads the capture amount straight off `requirements.amount`
   (upto/permit2_utils.py:388) and rejects anything above the signed permitted
   amount (:542). So we settle with a COPY of the requirements whose `amount`
   is what the tool actually consumed, read from `app.gateway.meter`. The
   payload is untouched; the ceiling the payer signed is untouched.

2. DEFERRED SETTLEMENT (session batching). Under `batch-settlement` the per-call
   step is a cumulative voucher and the on-chain claim is a separate server-side
   action, so "don't call settle now" is the scheme working as designed, not a
   trick. See the honesty note on `_deferred` below for what we do -- and say --
   when batching is switched on with a scheme that does not work that way.

--------------------------------------------------------------------------
WHY THE REAL SERVER IS BUILT LAZILY
--------------------------------------------------------------------------
`x402ResourceServer.verify_payment` refuses to run before `initialize()`
(server_base.py:1110), and `initialize()` performs a BLOCKING HTTP GET to the
facilitator's /supported endpoint (server_base.py:397). That cannot happen at
import (the app must boot with no network) and must not happen on the event loop
(it would block every other request). So it happens once, lazily, on the first
paid call, inside `asyncio.to_thread`, under a lock. A failure is cached briefly
and converted into a readable `VerifyResponse(is_valid=False, ...)` rather than
an exception -- an agent gets a 402 explaining that the facilitator is
unreachable, which is actionable, instead of an opaque tool error.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from x402.schemas.payments import PaymentPayload, PaymentRequirements
from x402.schemas.responses import SettleResponse, VerifyResponse

from app.config import settings
from app.gateway.meter import current_meter
from app.models import Scheme, SettlementMode

log = logging.getLogger(__name__)

__all__ = [
    "MeteredResourceServer",
    "get_resource_server",
    "facilitator_status",
    "reset_resource_server",
]

#: How long a failed facilitator initialize() is remembered before we retry.
#: Long enough that a dead facilitator does not turn every call into a 30-second
#: blocking probe; short enough that recovery is automatic.
_INIT_BACKOFF_SECONDS = 30.0

_lock = asyncio.Lock()
_server: Any | None = None
_supported: dict[str, Any] = {}
_last_error: str | None = None
_last_error_at: float = 0.0


def reset_resource_server() -> None:
    """Drop the cached server. Tests only."""
    global _server, _last_error, _last_error_at, _supported
    _server = None
    _supported = {}
    _last_error = None
    _last_error_at = 0.0


def facilitator_status() -> dict[str, Any]:
    """Readable state for /healthz and the free `gateway_info` tool."""
    return {
        "url": settings.facilitator_url,
        "label": settings.facilitator_label,
        "initialized": _server is not None,
        "authenticated": bool(settings.cdp_api_key_id and settings.cdp_api_key_secret),
        "supportedSchemes": sorted(_supported.keys()),
        "lastError": _last_error,
    }


def _build_facilitator() -> Any:
    """An async facilitator client, with CDP credentials when configured.

    The public x402.org facilitator needs no auth on testnet; Coinbase's hosted
    one does. `create_headers` is the SDK's documented hook for that, and it is
    called per request so a rotated key is picked up without a restart.
    """
    from x402.http import FacilitatorConfig, HTTPFacilitatorClient

    key_id = settings.cdp_api_key_id.strip()
    key_secret = settings.cdp_api_key_secret.strip()

    config = FacilitatorConfig(
        url=settings.facilitator_url,
        timeout=30.0,
        identifier=settings.facilitator_label or settings.facilitator_url,
    )
    if key_id and key_secret:
        from x402.http import CreateHeadersAuthProvider

        def _headers() -> dict[str, dict[str, str]]:
            auth = {"X-CDP-API-KEY-ID": key_id, "X-CDP-API-KEY-SECRET": key_secret}
            return {"verify": auth, "settle": auth, "supported": auth}

        config.auth_provider = CreateHeadersAuthProvider(_headers)

    return HTTPFacilitatorClient(config)


def _build_and_initialize() -> tuple[Any, dict[str, Any]]:
    """Blocking. Runs in a worker thread -- never call this from the loop."""
    from x402 import x402ResourceServer
    from x402.mechanisms.evm.exact import ExactEvmServerScheme

    server = x402ResourceServer(_build_facilitator())
    server.register(settings.x402_network, ExactEvmServerScheme())

    # `upto` is registered opportunistically: its requirements need a
    # facilitatorAddress that only the facilitator can supply, so a facilitator
    # that does not advertise the scheme simply means no upto tools are offered.
    try:
        from x402.mechanisms.evm.upto import UptoEvmServerScheme

        server.register(settings.x402_network, UptoEvmServerScheme())
    except Exception:
        log.debug("upto scheme unavailable in this x402 build", exc_info=True)

    server.initialize()  # <- the blocking GET /supported

    supported: dict[str, Any] = {}
    for network, by_scheme in getattr(server, "_supported_responses", {}).items():
        if network != settings.x402_network:
            continue
        for scheme, response in by_scheme.items():
            for kind in getattr(response, "kinds", []):
                if kind.scheme == scheme and kind.network == network:
                    supported[scheme] = kind.extra or {}
                    break
            supported.setdefault(scheme, {})
    return server, supported


async def get_resource_server() -> Any:
    """The initialized `x402ResourceServer`, building it on first use.

    Raises `RuntimeError` with the facilitator's own error text when it cannot
    be reached. Callers in the payment path catch this and turn it into a 402.
    """
    global _server, _supported, _last_error, _last_error_at

    if _server is not None:
        return _server

    async with _lock:
        if _server is not None:
            return _server
        if _last_error and (time.monotonic() - _last_error_at) < _INIT_BACKOFF_SECONDS:
            raise RuntimeError(_last_error)
        try:
            server, supported = await asyncio.to_thread(_build_and_initialize)
        except Exception as exc:
            _last_error = f"facilitator {settings.facilitator_url} unreachable: {exc}"
            _last_error_at = time.monotonic()
            log.warning("x402 facilitator initialize failed: %s", exc)
            raise RuntimeError(_last_error) from exc

        _server, _supported, _last_error = server, supported, None
        log.info(
            "x402 facilitator ready: %s schemes=%s",
            settings.facilitator_url,
            sorted(supported.keys()),
        )
        return _server


async def supported_schemes() -> dict[str, Any]:
    """Scheme -> supported-kind `extra`, as advertised by the facilitator.

    Empty when the facilitator has not been reached yet. Used to decide whether
    a tool may advertise `upto` (which needs `facilitatorAddress`) or
    `batch-settlement`.
    """
    try:
        await get_resource_server()
    except RuntimeError:
        return {}
    return dict(_supported)


class MeteredResourceServer:
    """Thin proxy around `x402ResourceServer`. Owns capture and deferral.

    Everything not overridden is forwarded, so this stays a decorator over the
    SDK's server rather than a fork of it.
    """

    def __init__(self, settlement_mode: SettlementMode | None = None) -> None:
        self.settlement_mode = settlement_mode or (
            SettlementMode.BATCHED if settings.batching_enabled else SettlementMode.PER_CALL
        )

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - passthrough
        # Only reached for attributes we do not define. Deliberately raises if
        # the server has not been built, since every such use is a programming
        # error rather than a runtime condition.
        if _server is None:
            raise AttributeError(f"resource server not initialized (wanted {name!r})")
        return getattr(_server, name)

    # ------------------------------------------------------------- matching --
    def find_matching_requirements(
        self, available: list[PaymentRequirements], payload: PaymentPayload
    ) -> PaymentRequirements | None:
        """Match a payload to one of the advertised requirements.

        Reimplemented rather than delegated purely so it works before the
        facilitator has been reached -- it is pure comparison (server_base.py:927)
        and needs nothing from the network. Same five-field equality the SDK uses,
        so behaviour is identical.
        """
        accepted = payload.accepted
        for req in available:
            if (
                accepted.scheme == req.scheme
                and accepted.network == req.network
                and accepted.amount == req.amount
                and accepted.asset == req.asset
                and accepted.pay_to == req.pay_to
            ):
                return req
        return None

    # --------------------------------------------------------------- verify --
    async def verify_payment(
        self,
        payload: PaymentPayload,
        requirements: PaymentRequirements,
        *args: Any,
        **kwargs: Any,
    ) -> VerifyResponse:
        """Always a real facilitator round trip. This is the risk control.

        Verification is what proves the authorization is well-formed, unexpired
        and funded. It is never skipped, never deferred and never faked -- even
        when settlement is deferred, because a deferred settlement of an invalid
        authorization is just an unpaid call.
        """
        try:
            server = await get_resource_server()
        except RuntimeError as exc:
            return VerifyResponse(
                is_valid=False,
                invalid_reason="facilitator_unavailable",
                invalid_message=str(exc),
            )

        try:
            result = await server.verify_payment(payload, requirements, *args, **kwargs)
        except Exception as exc:
            log.exception("verify_payment raised")
            return VerifyResponse(
                is_valid=False,
                invalid_reason="verify_error",
                invalid_message=str(exc)[:400],
            )

        # x402ResourceServer.verify_payment returns a ResourceVerifyResponse
        # wrapper (schemas/hooks.py:185) whose `.verify` is the VerifyResponse.
        # The MCP wrapper only reads `.is_valid` / `.invalid_reason`, which the
        # wrapper proxies -- but the ledger wants the real thing, so unwrap.
        return getattr(result, "verify", result)

    # --------------------------------------------------------------- settle --
    async def settle_payment(
        self,
        payload: PaymentPayload,
        requirements: PaymentRequirements,
        *args: Any,
        **kwargs: Any,
    ) -> SettleResponse:
        """Capture what was consumed, now or at batch close."""
        capture = self._capture_requirements(requirements)

        if self.settlement_mode is SettlementMode.BATCHED:
            return self._deferred(payload, capture)

        try:
            server = await get_resource_server()
        except RuntimeError as exc:
            return SettleResponse(
                success=False,
                error_reason="facilitator_unavailable",
                error_message=str(exc),
                transaction="",
                network=capture.network,
            )

        try:
            return await server.settle_payment(payload, capture, *args, **kwargs)
        except Exception as exc:
            log.exception("settle_payment raised")
            return SettleResponse(
                success=False,
                error_reason="settle_error",
                error_message=str(exc)[:400],
                transaction="",
                network=capture.network,
            )

    # -------------------------------------------------------------- helpers --
    #: Schemes where the server may settle for LESS than the advertised amount.
    #: `exact` is not one of them and never can be: EIP-3009
    #: `transferWithAuthorization` moves precisely the value that was signed, so
    #: "capture less" is not a thing the scheme can express. Applying metered
    #: capture there would produce a requirements/payload mismatch and a
    #: facilitator rejection -- and, worse, a receipt claiming a partial charge
    #: that the chain would contradict.
    _PARTIAL_CAPTURE_SCHEMES = frozenset({str(Scheme.UPTO), str(Scheme.BATCH_SETTLEMENT)})

    @classmethod
    def _capture_requirements(cls, requirements: PaymentRequirements) -> PaymentRequirements:
        """Requirements with `amount` replaced by metered capture.

        Returns the original object untouched when there is nothing to meter, or
        when the matched scheme cannot express a partial capture -- so `exact`
        tools take exactly the SDK's own path, byte for byte.
        """
        meter = current_meter()
        if meter is None or meter.price_per_unit_atomic is None:
            return requirements
        if requirements.scheme not in cls._PARTIAL_CAPTURE_SCHEMES:
            return requirements

        captured = meter.capture_atomic()
        ceiling = int(requirements.amount)
        if captured > ceiling:
            # Cannot happen -- Meter.capture_atomic clamps -- but the invariant
            # is worth more than the branch. Charging above the authorization is
            # the single outcome this system exists to make impossible.
            log.error(
                "metered capture %s exceeded authorized %s for %s; clamping",
                captured,
                ceiling,
                meter.tool_name,
            )
            captured = ceiling

        return requirements.model_copy(update={"amount": str(captured)})

    def _deferred(self, payload: PaymentPayload, capture: PaymentRequirements) -> SettleResponse:
        """A settlement that has not happened yet, said out loud.

        `transaction` is the empty string, never a placeholder hash: a receipt
        that names a transaction which does not exist is worse than one that
        admits it is pending. `extra` carries the mode so a client can tell a
        deferred capture from a landed one without parsing prose.

        HONESTY NOTE, and it belongs in the artefact rather than the pitch:
        deferring is only *sound* under `batch-settlement`, where the per-call
        artefact is a cumulative voucher against a funded channel and the
        on-chain claim is a separate server-side step by design. Under `exact`
        or `upto` the authorization is settleable immediately, so deferring it
        means this gateway serves the response before the money moves -- it is
        extending credit for the length of the batch window. That is a real
        counterparty risk and it is labelled `deferred_unsecured` in the
        receipt, the ledger and the dashboard rather than being smoothed over.
        The configuration exists because measuring per-call fee load against
        batched fee load on the same tools is the whole argument; it is not a
        recommendation.
        """
        scheme = capture.scheme
        secured = scheme == str(Scheme.BATCH_SETTLEMENT)
        return SettleResponse(
            success=True,
            transaction="",
            network=capture.network,
            payer=_payer_of(payload),
            amount=capture.amount,
            extra={
                "settlement": "deferred" if secured else "deferred_unsecured",
                "scheme": scheme,
                "settlesAt": "batch_close",
                "batchWindowSeconds": settings.batch_window_seconds,
                "note": (
                    "Cumulative voucher recorded; the on-chain claim happens at "
                    "batch close, as the batch-settlement scheme specifies."
                    if secured
                    else "Authorization verified and captured but NOT yet settled "
                    "on-chain. Under the 'exact'/'upto' schemes this gateway is "
                    "extending credit until batch close. Set "
                    "BATCHING_ENABLED=false for settle-per-call."
                ),
            },
        )


def _payer_of(payload: PaymentPayload) -> str | None:
    """Best-effort payer address out of a scheme-specific payload.

    Every EVM scheme puts it somewhere different, and none of them are worth a
    hard dependency: the payer is metadata on the receipt, not a control.
    """
    data = payload.payload if isinstance(payload.payload, dict) else {}
    for path in (
        ("authorization", "from"),
        ("permit2Authorization", "from"),
        ("permit", "owner"),
        ("owner",),
        ("from",),
        ("payer",),
    ):
        cursor: Any = data
        for key in path:
            if not isinstance(cursor, dict):
                cursor = None
                break
            cursor = cursor.get(key)
        if isinstance(cursor, str) and cursor:
            return cursor
    return None
