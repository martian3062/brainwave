"""Building the 402 challenge -- offline, on purpose.

The obvious way to build `PaymentRequirements` is
`x402ResourceServer.build_payment_requirements(ResourceConfig(...))`. We do not
use it, and the reason is a hard constraint rather than a preference:

    x402/server_base.py:470
        if not self._initialized:
            raise RuntimeError("Server not initialized. Call initialize() first.")

    x402/server_base.py:394 -- initialize()
        for client in self._facilitator_clients:
            supported = client.get_supported()      # <- SYNCHRONOUS HTTP CALL

So `build_payment_requirements` cannot run until a blocking network round trip
to the facilitator has succeeded. Doing that at import time would mean the app
refuses to boot without network, and doing it inside the event loop would block
it. Both are unacceptable for a service whose stated property is "boots on an
empty .env".

`PaymentRequirements` is a plain pydantic model, so we construct it directly and
fill `extra` with the EIP-712 domain fields from x402's own `NETWORK_CONFIGS`
table -- the same source `ExactEvmScheme.enhance_payment_requirements` reads
(x402/mechanisms/evm/exact/server.py:148-156). The facilitator round trip still
happens, lazily, before the first verify/settle: see
`app.gateway.resource_server`.

--------------------------------------------------------------------------
A NOTE ON `upto`, WHICH WE CANNOT ADVERTISE OFFLINE
--------------------------------------------------------------------------
`UptoEvmScheme.enhance_payment_requirements` (upto/server.py:107-114) *requires*
a `facilitatorAddress` taken from the facilitator's advertised supported kinds,
and raises without it. There is no offline substitute -- the address belongs to
the facilitator, not to us. So a tool priced `upto` advertises `upto` only once
the facilitator has been reached and has published that address; until then it
falls back to advertising its ceiling as `exact` and captures the full ceiling.
That fallback is worse for the payer, so it is stated in the tool's own
description and in the challenge's `extra.eraya` block rather than hidden.
"""

from __future__ import annotations

import logging
from typing import Any

from x402.schemas.payments import PaymentRequirements, ResourceInfo

from app.config import settings
from app.models import Scheme

log = logging.getLogger(__name__)

__all__ = [
    "asset_extra",
    "build_requirements",
    "resource_info",
    "resource_url",
    "discovery_extension",
]


def resource_url(tool_name: str) -> str:
    """The x402 resource identifier for an MCP tool.

    `mcp://tool/<name>` is the convention `create_payment_wrapper` itself
    defaults to (x402/mcp/server.py:103), so receipts issued here line up with
    receipts issued by anything else built on the SDK.
    """
    return f"mcp://tool/{tool_name}"


def resource_info(tool_name: str, description: str, tags: list[str] | None = None) -> ResourceInfo:
    """Build the `ResourceInfo` that rides in the 402 challenge.

    NOTE THE IMPORT AT THE TOP OF THIS MODULE. `x402.mcp.ResourceInfo` -- the
    class the SDK's own docstring tells you to use -- resolves to
    `x402.mcp.types.ResourceInfo`, a plain class with no `model_dump()`, while
    `x402/mcp/server.py::_create_payment_required_result` calls
    `resource.model_dump(by_alias=True, exclude_none=True)` on it. Following the
    documented import raises AttributeError on the FIRST unpaid call -- i.e. the
    402 challenge itself. The pydantic one lives in `x402.schemas.payments`.
    `tests/test_spine.py::test_x402_mcp_resource_info_is_the_wrong_class` pins
    this and will fail loudly when upstream fixes it.
    """
    return ResourceInfo(
        url=resource_url(tool_name),
        description=description[:512],
        mime_type="application/json",
        service_name="TRAPPIST x BRAINWAVE"[:32],
        tags=(tags or [])[:5],
    )


def asset_extra() -> dict[str, Any]:
    """EIP-712 domain fields for the configured asset.

    Read from x402's `NETWORK_CONFIGS` when the asset is the network's canonical
    stablecoin, so the `name`/`version` a buyer signs over are byte-identical to
    what the facilitator expects. Falls back to the operator-configured values
    for a custom token, which is the only case where we have to be told.
    """
    try:
        from x402.mechanisms.evm.utils import get_asset_info

        info = get_asset_info(settings.x402_network, settings.asset_address)
        extra: dict[str, Any] = {}
        transfer_method = info.get("asset_transfer_method")
        if not transfer_method or info.get("supports_eip2612", False):
            extra["name"] = info["name"]
            extra["version"] = info["version"]
        if transfer_method:
            extra["assetTransferMethod"] = transfer_method
        return extra
    except Exception:
        # Custom asset, or an x402 version that moved the table. The configured
        # EIP-712 domain is the honest fallback -- signing needs *something*,
        # and guessing is worse than using what the operator declared.
        log.debug(
            "asset %s is not in x402's NETWORK_CONFIGS for %s -- using configured EIP-712 domain",
            settings.asset_address,
            settings.x402_network,
        )
        return {"name": settings.x402_asset_name, "version": settings.x402_asset_version}


def build_requirements(
    *,
    amount_atomic: int,
    pay_to: str,
    scheme: Scheme = Scheme.EXACT,
    max_timeout_seconds: int | None = None,
    extra: dict[str, Any] | None = None,
) -> PaymentRequirements:
    """One `PaymentRequirements`, built without touching the network.

    `amount` is a STRING of atomic units -- x402's own representation, and the
    reason `app.money` never produces a float.
    """
    if amount_atomic < 0:
        raise ValueError(f"negative amount: {amount_atomic}")

    merged = asset_extra()
    if extra:
        merged.update(extra)

    return PaymentRequirements(
        scheme=str(scheme),
        network=settings.x402_network,
        asset=settings.asset_address,
        amount=str(amount_atomic),
        pay_to=pay_to,
        max_timeout_seconds=max_timeout_seconds or settings.payment_timeout_seconds,
        extra=merged,
    )


def discovery_extension(
    *,
    tool_name: str,
    description: str,
    input_schema: dict[str, Any],
    example: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Bazaar discovery metadata, so a facilitator can index this tool.

    Uses the SDK's own `declare_mcp_discovery_extension` -- we are not inventing
    a discovery format. Returns None (rather than raising) if the extension is
    unavailable in the installed x402; an unindexed tool still sells.
    """
    try:
        from x402.extensions.bazaar import (  # type: ignore[attr-defined]
            DeclareMcpDiscoveryConfig,
            declare_mcp_discovery_extension,
        )

        return declare_mcp_discovery_extension(
            DeclareMcpDiscoveryConfig(
                tool_name=tool_name,
                description=description[:512],
                input_schema=input_schema,
                transport="streamable-http",
                example=example,
            )
        )
    except Exception:
        log.debug("bazaar discovery extension unavailable for %s", tool_name, exc_info=True)
        return None
