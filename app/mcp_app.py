"""The MCP server that gets mounted into the FastAPI spine.

Two things are configured here that are not obvious and that both bite in
production. Both were established by running the installed SDK, not by reading
its README.

1. MOUNT PATH.
   `FastMCP.streamable_http_app()` registers its endpoint at
   `settings.streamable_http_path`, which defaults to `"/mcp"`. Mounting that
   sub-app at `"/mcp"` therefore produces `/mcp/mcp`. Worse, Starlette's
   `Mount("/mcp")` compiles to `^/mcp(?P<path>/.*)$`, so the *bare* path `/mcp`
   does not match the mount at all -- with NiceGUI mounted at `"/"` it falls
   through to NiceGUI and an agent gets an HTML 404 instead of JSON-RPC.
   Fix: build FastMCP with `streamable_http_path="/"` and mount at `/mcp`, so
   the endpoint is exactly `/mcp/`. `app.main` adds an explicit redirect for the
   bare `/mcp` so both spellings work.

2. DNS-REBINDING PROTECTION.
   mcp==1.28.1 ships `TransportSecuritySettings(enable_dns_rebinding_protection=
   True, allowed_hosts=['127.0.0.1:*','localhost:*','[::1]:*'])` *by default*.
   Deployed to `something.onrender.com`, every single MCP request returns
   421 "Invalid Host header" -- verified by reproducing it. The host list below
   is derived from PUBLIC_BASE_URL so the deployed host is always allowed.

The paid catalogue is deliberately not in this file. `app.catalogue.register_tools`
is the seam: it receives the FastMCP instance and attaches `@paid()` tools to it.
The spine stands up with or without it.

--------------------------------------------------------------------------
READ THIS BEFORE WRITING THE CATALOGUE -- a real bug in x402==2.16.0
--------------------------------------------------------------------------
The SDK's own docstring tells you to write:

    from x402.mcp import create_payment_wrapper, ResourceInfo

That `ResourceInfo` resolves (via `x402/mcp/__init__.py`'s lazy `__getattr__`)
to `x402.mcp.types.ResourceInfo` -- a PLAIN class with no `model_dump()`. But
`x402/mcp/server.py::_create_payment_required_result` calls
`resource.model_dump(by_alias=True, exclude_none=True)`. So following the
documented import raises `AttributeError: 'ResourceInfo' object has no attribute
'model_dump'` on the FIRST unpaid tool call -- i.e. the 402 challenge, the one
path that must never fail. Reproduced against the installed wheel.

Use the pydantic one:

    from x402.mcp import create_payment_wrapper
    from x402.schemas.payments import ResourceInfo   # <- has model_dump()

`tests/test_spine.py::test_x402_mcp_resource_info_is_the_wrong_class` pins this,
and will fail loudly when upstream fixes it.

Two more verified facts for whoever writes `@paid()`:

  * `create_payment_wrapper` injects a synthetic `ctx: Context` parameter into
    the wrapped function's signature so FastMCP's `find_context_parameter()`
    supplies the request context. Do NOT declare `ctx` in a tool handler, and do
    NOT rebuild `__signature__` after the wrapper -- payment metadata arrives via
    that context and nowhere else.
  * Decorator order is `@mcp.tool(...)` OUTSIDE, `@wrapper` INSIDE. Reversed, the
    tool registers the unpaid function.
"""

from __future__ import annotations

import json
import logging

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.config import settings

log = logging.getLogger(__name__)

_mcp: FastMCP | None = None


def build_mcp() -> FastMCP:
    """Construct the FastMCP server and attach the tool catalogue."""
    mcp = FastMCP(
        name=settings.mcp_server_name,
        instructions=(
            "TRAPPIST x BRAINWAVE -- paid MCP tools settled with x402 on Base. "
            "Tools return HTTP-402-shaped payment requirements in their result "
            "until a signed authorization is supplied in the MCP _meta key "
            "'x402/payment'. Payment does NOT travel in an X-PAYMENT header over "
            "this transport."
        ),
        # See note 1 above.
        streamable_http_path="/",
        stateless_http=settings.mcp_stateless,
        json_response=settings.mcp_json_response,
        debug=settings.debug,
        # See note 2 above.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=settings.mcp_dns_rebinding_protection,
            allowed_hosts=settings.mcp_host_allowlist,
            allowed_origins=settings.mcp_origin_allowlist,
        ),
    )

    _register_free_tools(mcp)
    _register_catalogue(mcp)
    return mcp


def _register_free_tools(mcp: FastMCP) -> None:
    """Unpaid tools. Discovery and receipt verification are never paywalled."""

    @mcp.tool(
        name="gateway_info",
        description=(
            "Free. Describes this gateway: network, asset, facilitator, "
            "settlement mode and the MCP payment metadata key to use."
        ),
    )
    def gateway_info() -> str:
        return json.dumps(
            {
                "name": settings.app_name,
                "network": settings.x402_network,
                "asset": settings.asset_address,
                "assetSymbol": settings.x402_asset_symbol,
                "assetDecimals": settings.x402_asset_decimals,
                "facilitator": settings.facilitator_url,
                "settlement": "batched" if settings.batching_enabled else "per_call",
                "batchWindowSeconds": settings.batch_window_seconds,
                "x402Version": 2,
                # The single most common integration mistake: reaching for an
                # HTTP header that this transport does not read.
                "paymentTransport": {
                    "requestMetaKey": "x402/payment",
                    "responseMetaKey": "x402/payment-response",
                    "note": (
                        "Over MCP, payment rides in JSON-RPC _meta. X-PAYMENT / "
                        "PAYMENT-SIGNATURE headers apply only to the plain-HTTP "
                        "paywalled routes on this same server."
                    ),
                },
                "endpoint": settings.mcp_public_url,
            }
        )


def _register_catalogue(mcp: FastMCP) -> None:
    """Attach the paid tool catalogue, if it has been built yet.

    Import is deferred and failure is non-fatal on purpose: the spine must stand
    up and be inspectable even while the catalogue is still being written.
    """
    try:
        from app.catalogue import register_tools  # type: ignore[attr-defined]
    except ImportError:
        log.info("app.catalogue not present yet -- serving free tools only")
        return
    except Exception:
        log.exception("app.catalogue failed to import -- serving free tools only")
        return

    try:
        register_tools(mcp)
        log.info("paid catalogue registered")
    except Exception:
        log.exception("app.catalogue.register_tools() failed")


def get_mcp() -> FastMCP:
    """Process-wide FastMCP singleton.

    Must be a singleton: `streamable_http_app()` lazily creates the session
    manager on first call, and the lifespan that starts it is bound to that
    exact instance.
    """
    global _mcp
    if _mcp is None:
        _mcp = build_mcp()
    return _mcp
