"""The x402 SDK object graph, as lazily built injectable singletons.

`decorator.py` and `batching.py` both need the same `x402ResourceServer`, the same
facilitator client and the same registered scheme instances -- the batch-settlement
scheme object in particular OWNS the channel storage, so two of them means two sets
of vouchers and a settlement that claims half of what it should. They live here so
neither module imports the other.

TWO FACTS THAT SHAPE THIS MODULE
--------------------------------
1. `x402ResourceServer.verify_payment` raises
   `RuntimeError("Server not initialized. Call initialize() first.")`, and
   `initialize()` performs a BLOCKING HTTP GET to the facilitator's `/supported`
   (`HTTPFacilitatorClient.get_supported` is sync even on the async client).
   Tool registration happens at import time and Render's health check happens
   before anyone has paid for anything, so initialization is deferred to the
   first paid call and run in a worker thread. A facilitator that is down
   therefore breaks paid calls -- correctly -- without breaking the process.

2. `initialize()` is what populates `_facilitator_clients_map`, and
   `_verify_payment_core` looks the client up by `(network, scheme)` from that
   map. So a scheme the facilitator does not advertise cannot be verified even if
   we registered it locally. `describe()` reports exactly which pairs are live,
   and `/healthz`-style surfaces should show it rather than discovering it on a
   customer's first call.

Nothing here talks to a chain and nothing here spends. `initialize()` is a GET.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.models import Scheme

log = logging.getLogger(__name__)

__all__ = [
    "Gateway",
    "get_gateway",
    "set_gateway",
    "reset_gateway",
    "enhance",
]


@dataclass
class Gateway:
    """Everything from the x402 SDK that has process-wide identity."""

    resource_server: Any
    facilitator: Any
    #: scheme value -> the registered scheme-server instance.
    schemes: dict[str, Any]
    network: str
    #: False until `initialize()` has successfully answered.
    ready: bool = False
    last_error: str | None = None

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    # ------------------------------------------------------------ startup ----

    def initialize(self) -> bool:
        """Fetch supported kinds from the facilitator. BLOCKING HTTP -- see above.

        Idempotent and safe to call from several threads; only the first caller
        does the request. Returns True when the resource server is usable.
        """
        with self._lock:
            if self.ready:
                return True
            try:
                self.resource_server.initialize()
            except Exception as exc:  # noqa: BLE001 -- reported, not swallowed
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "x402 facilitator %s not reachable (%s); paid calls will fail until it is",
                    settings.facilitator_url,
                    self.last_error,
                )
                return False
            self.ready = True
            self.last_error = None
            log.info("x402 resource server initialized against %s", settings.facilitator_url)
            return True

    async def ensure_ready(self) -> bool:
        """Async-safe `initialize()`. Runs the blocking GET off the event loop."""
        if self.ready:
            return True
        import asyncio

        return await asyncio.to_thread(self.initialize)

    # ------------------------------------------------------------ queries ----

    def scheme_server(self, scheme: Scheme | str) -> Any | None:
        return self.schemes.get(scheme.value if isinstance(scheme, Scheme) else scheme)

    def supported_kind(self, scheme: Scheme | str) -> Any | None:
        """The facilitator's advertised kind for this scheme on our network.

        None means the facilitator does not support it, which is the difference
        between "we offer upto" and "upto will actually settle".
        """
        name = scheme.value if isinstance(scheme, Scheme) else scheme
        try:
            return self.resource_server.get_supported_kind(2, self.network, name)
        except Exception:  # noqa: BLE001
            return None

    def describe(self) -> dict[str, Any]:
        """What is actually wired, for the dashboard and for operators."""
        live = {}
        for name in self.schemes:
            kind = self.supported_kind(name) if self.ready else None
            live[name] = {
                "registered": True,
                "facilitatorSupports": kind is not None,
                "extra": (kind.extra or {}) if kind is not None else {},
            }
        return {
            "network": self.network,
            "facilitator": settings.facilitator_url,
            "ready": self.ready,
            "lastError": self.last_error,
            "schemes": live,
        }


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------

_gateway: Gateway | None = None
_build_lock = threading.Lock()


def _build() -> Gateway:
    """Construct the SDK graph. No network I/O -- that is `initialize()`."""
    from x402 import x402ResourceServer
    from x402.http import FacilitatorConfig, HTTPFacilitatorClient
    from x402.mechanisms.evm.exact import ExactEvmServerScheme
    from x402.mechanisms.evm.upto import UptoEvmServerScheme

    facilitator = HTTPFacilitatorClient(
        FacilitatorConfig(
            url=settings.facilitator_url,
            timeout=30.0,
            identifier=settings.facilitator_label,
        )
    )
    server = x402ResourceServer(facilitator)

    network = settings.x402_network
    schemes: dict[str, Any] = {
        Scheme.EXACT.value: ExactEvmServerScheme(),
        Scheme.UPTO.value: UptoEvmServerScheme(),
    }

    # Batch settlement is opt-in. Exact and upto authorizations are not payment
    # channels and are always settled on the call that consumed them.
    if settings.batching_enabled:
        try:
            from x402.mechanisms.evm.batch_settlement.server.scheme import (
                BatchSettlementEvmScheme,
                BatchSettlementEvmSchemeServerConfig,
            )

            from app.channels import channel_storage

            schemes[Scheme.BATCH_SETTLEMENT.value] = BatchSettlementEvmScheme(
                receiver_address=settings.pay_to_address,
                config=BatchSettlementEvmSchemeServerConfig(
                    storage=channel_storage(),
                    # The default withdraw delay (15 min) is the SDK's floor and
                    # governs how long a payer waits to unilaterally exit.
                    withdraw_delay=max(900, settings.batch_window_seconds * 3),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "batch-settlement scheme unavailable: %s: %s",
                type(exc).__name__,
                exc,
            )

    for name, scheme in schemes.items():
        server.register(network, scheme)
        log.debug("registered x402 scheme %s on %s", name, network)

    return Gateway(
        resource_server=server,
        facilitator=facilitator,
        schemes=schemes,
        network=network,
    )


def get_gateway() -> Gateway:
    """Process-wide singleton. Built on first use, never at import."""
    global _gateway
    if _gateway is None:
        with _build_lock:
            if _gateway is None:
                _gateway = _build()
    return _gateway


def set_gateway(gateway: Gateway) -> None:
    """Inject a gateway. The seam the tests use to avoid touching the network."""
    global _gateway
    _gateway = gateway


def reset_gateway() -> None:
    global _gateway
    _gateway = None


# --------------------------------------------------------------------------
# Requirements enhancement
# --------------------------------------------------------------------------


def enhance(requirements: Any, scheme: Scheme | str, *, gateway: Gateway | None = None) -> Any:
    """Apply the SDK's own scheme-specific `extra` to our hand-built requirements.

    `upto` refuses to work without `extra.facilitatorAddress` and
    `batch-settlement` without `extra.receiverAuthorizer`; both values come from
    what the facilitator advertises, and both are filled in by the scheme's
    `enhance_payment_requirements`. We call the SDK's implementation rather than
    copying the fields, so a change upstream reaches us.

    Before the gateway is initialized there is nothing to enhance WITH, so the
    requirements are returned untouched: an `exact` tool works, and an `upto` tool
    will fail loudly at the facilitator instead of quietly settling wrong.
    """
    gw = gateway or get_gateway()
    if not gw.ready:
        return requirements
    name = scheme.value if isinstance(scheme, Scheme) else scheme
    server = gw.scheme_server(name)
    kind = gw.supported_kind(name)
    if server is None or kind is None:
        return requirements
    try:
        return server.enhance_payment_requirements(requirements, kind, [])
    except Exception as exc:  # noqa: BLE001
        log.warning("enhance_payment_requirements failed for %s: %s", name, exc)
        return requirements
