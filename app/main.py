"""THE SPINE.

One ASGI app serves four things:

    /mcp/    paid MCP tools          (FastMCP streamable HTTP, mounted)
    /admin   the raw ledger          (SQLAdmin)
    /        the author dashboard    (NiceGUI, pure Python)
    /healthz free operational routes (plus the plain-HTTP paywalled routes)

NiceGUI *is* FastAPI underneath, which is the only reason all four can share one
process, one lifespan and one deploy. That single fact is what bought us the
official x402 FastAPI middleware, a one-line Starlette mount for the MCP app, and
a reactive frontend with no HTML, CSS, JS, Node or npm in the repository.

================================================================================
COMPOSITION ORDER -- THIS IS NOT ARBITRARY
================================================================================

    1. combined lifespan is defined
    2. app = FastAPI(lifespan=combined)
    3. app.mount("/mcp", mcp_asgi)          + explicit bare-/mcp redirect
    4. free HTTP routes, then the x402 HTTP paywall middleware
    5. SQLAdmin at /admin                   + explicit bare-/admin redirect
    6. dashboard pages registered
    7. ui.run_with(app)                     <- LAST, ALWAYS

Step 7 must be last because `ui.run_with()` does `app.mount("/", nicegui_app)`.
Starlette resolves routes in registration order and `Mount("/")` matches every
path, so anything registered after it is unreachable. Verified by reproducing it:
with NiceGUI mounted first, a POST to /mcp/ returns NiceGUI's HTML 404 page.

================================================================================
LIFESPAN -- WHAT I ACTUALLY FOUND (this is the one integration point that
silently half-works)
================================================================================

Probed empirically against the installed starlette 1.3.1 / fastapi 0.140.2 /
mcp 1.28.1, not read off a README:

  * A MOUNTED SUB-APP'S LIFESPAN NEVER RUNS. Starlette dispatches the "lifespan"
    scope in `Router.lifespan()`; `Mount` routes only match "http" and
    "websocket". Probe: parent lifespan fired, mounted sub-app lifespan did not,
    in either direction (no startup, no shutdown).

  * THAT MATTERS BECAUSE `FastMCP.streamable_http_app()` RETURNS
    `Starlette(..., lifespan=lambda app: self.session_manager.run())`.
    The StreamableHTTP session manager is started by that lifespan and nothing
    else. Mount it naively and the app *looks* wired -- routes resolve, no
    warning is logged -- and then every single tools/call returns
    **500 Internal Server Error**. Reproduced both ways: unwired => 500; wired
    => the request reaches the MCP transport.

  * FIX: enter the sub-app's own lifespan context from the parent's.
    `async with mcp_asgi.router.lifespan_context(mcp_asgi): yield` in
    `combined_lifespan` below. Do not call `session_manager.run()` directly --
    going through `router.lifespan_context` means any future FastMCP startup work
    comes along for free.

  * NICEGUI DOES NOT CLOBBER IT. `ui.run_with()` reads
    `app.router.lifespan_context`, wraps it, and puts the wrapper back:
    `_startup()` -> `async with main_app_lifespan(app)` -> `_shutdown()`. So a
    lifespan passed to the `FastAPI(...)` constructor still runs, and its yielded
    state still propagates. Confirmed by probe: SUB_STARTUP, PARENT_STARTUP,
    PARENT_SHUTDOWN, SUB_SHUTDOWN, in that order, with NiceGUI installed.

  * The session manager also refuses to be run twice, so `get_mcp()` and the ASGI
    app must both be singletons. They are.

`/healthz` reports `mcp_session_manager: "started"` only if the lifespan actually
ran, so a broken wiring fails loudly instead of at the first paid call.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from nicegui import ui

from app import dashboard
from app.admin import mount_admin
from app.config import settings
from app.db import DATABASE_URL, create_all, describe
from app.mcp_app import get_mcp

logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO))
log = logging.getLogger("brainwave.spine")

# --------------------------------------------------------------------------
# 0. Singletons. `streamable_http_app()` lazily builds the session manager on
#    first call, and the lifespan below is bound to THIS instance -- so it is
#    built exactly once, here.
# --------------------------------------------------------------------------
mcp = get_mcp()
mcp_asgi = mcp.streamable_http_app()  # -> starlette.applications.Starlette


# --------------------------------------------------------------------------
# 1. The combined lifespan.
# --------------------------------------------------------------------------
@contextlib.asynccontextmanager
async def combined_lifespan(app: FastAPI) -> AsyncIterator[dict]:
    _preflight()

    # Local convenience only. Alembic owns the schema in every real deployment
    # (build.sh runs `alembic upgrade head`); create_all() is a no-op once the
    # tables exist.
    if DATABASE_URL.startswith("sqlite"):
        create_all()
    log.info("database: %s", describe())

    # Tool functions are registered while composing the MCP singleton, before
    # the lifespan runs. Sync their ledger rows only now, after SQLite create_all
    # or the production Alembic build step has guaranteed that tables exist.
    from app.gateway.server import sync_registered_catalogue

    sync_registered_catalogue()

    # THE LOAD-BEARING LINE. Without it the MCP session manager is never
    # started and every tools/call 500s. See the module docstring.
    async with mcp_asgi.router.lifespan_context(mcp_asgi):
        app.state.mcp_started = True
        log.info("MCP streamable-http session manager started at %s", settings.mcp_public_url)
        try:
            yield {"mcp_started": True}
        finally:
            app.state.mcp_started = False
            log.info("MCP session manager stopped")


ZERO_ADDRESS = "0x" + "0" * 40


def _preflight() -> None:
    """Refuse the configurations that deploy broken but report healthy."""
    problems: list[str] = []
    warnings: list[str] = []

    if settings.app_env == "production":
        if settings.storage_secret == "dev-only-change-me":
            problems.append("STORAGE_SECRET is still the development default")
        if settings.admin_enabled and not settings.admin_password:
            problems.append("ADMIN_PASSWORD is empty -- /admin would be unauthenticated")
        if settings.pay_to_address.lower() == ZERO_ADDRESS:
            problems.append("PAY_TO_ADDRESS is the zero address -- revenue would be burned")
        if DATABASE_URL.startswith("sqlite"):
            problems.append(
                "DATABASE_URL is SQLite in production -- the ledger would not survive a redeploy"
            )
        if not settings.asset_address:
            problems.append(f"no asset address for network {settings.x402_network}")

    if settings.app_env != "production" and settings.is_mainnet:
        warnings.append("mainnet configured outside production -- real USDC is at risk")
    if settings.public_base_url.startswith("http://localhost") and settings.app_env != "local":
        warnings.append(
            "PUBLIC_BASE_URL is still localhost -- MCP will 421 'Invalid Host header' "
            "for every request to the real hostname"
        )

    for warning in warnings:
        log.warning("preflight: %s", warning)
    if problems:
        raise RuntimeError("refusing to start:\n  - " + "\n  - ".join(problems))


# --------------------------------------------------------------------------
# 2. The app.
# --------------------------------------------------------------------------
app = FastAPI(
    title=settings.app_name,
    description="Paid MCP. Every agent tool call metered and settled with x402 on Base.",
    version="0.1.0",
    debug=settings.debug,
    lifespan=combined_lifespan,
    # NiceGUI serves /, so the interactive API docs move out of its way.
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)


# --------------------------------------------------------------------------
# 3. The paid MCP endpoint.
#
#    FastMCP is built with streamable_http_path="/" (see app/mcp_app.py), so
#    mounting here puts the JSON-RPC endpoint at exactly `/mcp/`.
#
#    Starlette's Mount("/mcp") compiles to ^/mcp(?P<path>/.*)$ -- the BARE path
#    "/mcp" does not match it. Normally FastAPI's redirect_slashes would rescue
#    that, but NiceGUI's Mount("/") swallows it first and an agent gets an HTML
#    404 instead of JSON-RPC. Hence the explicit redirect, registered here so it
#    resolves before NiceGUI.
# --------------------------------------------------------------------------
app.mount(settings.mcp_mount_path, mcp_asgi, name="mcp")


@app.api_route(
    settings.mcp_mount_path,
    methods=["GET", "POST", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def _mcp_slash_redirect() -> RedirectResponse:
    # 307 preserves the method and the JSON-RPC body.
    return RedirectResponse(url=f"{settings.mcp_mount_path}/", status_code=307)


# --------------------------------------------------------------------------
# 4. Free HTTP routes, then the plain-HTTP paywall.
# --------------------------------------------------------------------------
@app.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    started = bool(getattr(app.state, "mcp_started", False))
    return JSONResponse(
        {
            "status": "ok" if started else "degraded",
            "app": settings.app_name,
            "env": settings.app_env,
            "network": settings.x402_network,
            "asset": settings.asset_address,
            "settlement": "batched" if settings.batching_enabled else "per_call",
            # If this is not "started", the mounted MCP lifespan did not run and
            # every paid tool call will 500. Fail loudly here instead.
            "mcp_session_manager": "started" if started else "NOT STARTED",
            "mcp_endpoint": settings.mcp_public_url,
            "database": describe(),
        },
        status_code=200 if started else 503,
    )


@app.get("/api/config", include_in_schema=True, tags=["public"])
async def public_config() -> dict:
    """Everything an agent operator needs to point a client at this gateway."""
    return {
        "x402Version": 2,
        "mcpEndpoint": settings.mcp_public_url,
        "network": settings.x402_network,
        "asset": settings.asset_address,
        "assetSymbol": settings.x402_asset_symbol,
        "assetDecimals": settings.x402_asset_decimals,
        "facilitator": settings.facilitator_url,
        "settlementMode": "batched" if settings.batching_enabled else "per_call",
        "batchWindowSeconds": settings.batch_window_seconds,
        # Being precise about this everywhere is the point: two transports, two
        # different carriers for the same protocol.
        "paymentTransport": {
            "mcp": {"requestMeta": "x402/payment", "responseMeta": "x402/payment-response"},
            "http": {
                "request": "PAYMENT-SIGNATURE (X-PAYMENT legacy)",
                "response": "PAYMENT-RESPONSE (X-PAYMENT-RESPONSE legacy)",
            },
        },
    }


def _install_http_paywall() -> None:
    """Wire x402's OFFICIAL FastAPI middleware for plain-HTTP paid routes.

    `app.paywall.install(app)` is the seam. Contract for whoever writes it:

      * Use `x402.http.middleware.fastapi.payment_middleware(routes, server)`.
        Do not hand-roll a 402 -- there is first-party middleware and using it is
        the whole reason this app is FastAPI-shaped.
      * It MUST short-circuit for `/mcp*`, `/` and `/_nicegui*`. Payment over MCP
        rides in JSON-RPC `_meta`, not in a header; running the HTTP paywall over
        the MCP mount would look for a header that is correctly never there.
      * It is a BaseHTTPMiddleware, which buffers response bodies. Keep it off
        any streaming route.

    Absent, the app serves the free routes and the MCP endpoint normally.
    """
    try:
        from app.paywall import install  # type: ignore[attr-defined]
    except ImportError:
        log.info("app.paywall not present yet -- no plain-HTTP paywalled routes")
        return
    try:
        install(app)
        log.info("x402 HTTP paywall middleware installed")
    except Exception:
        log.exception("app.paywall.install() failed")


_install_http_paywall()


# --------------------------------------------------------------------------
# 5. The ledger admin. Mounted before NiceGUI, same bare-path caveat.
# --------------------------------------------------------------------------
admin = mount_admin(app)

if admin is not None:

    @app.get(settings.admin_path, include_in_schema=False)
    async def _admin_slash_redirect() -> RedirectResponse:
        return RedirectResponse(url=f"{settings.admin_path}/", status_code=307)


# --------------------------------------------------------------------------
# 6. Dashboard pages.
# --------------------------------------------------------------------------
dashboard.install(app)


# --------------------------------------------------------------------------
# 7. NiceGUI LAST. This does app.mount("/", nicegui_app) and wraps -- but does
#    not replace -- the lifespan set above.
# --------------------------------------------------------------------------
ui.run_with(
    app,
    title=settings.app_name,
    favicon="\N{ELECTRIC PLUG}",
    dark=False,
    storage_secret=settings.storage_secret,
    # Render supports websockets; NiceGUI needs them for its reactive updates.
    reconnect_timeout=5.0,
    show_welcome_message=False,
)


__all__ = ["app", "mcp", "mcp_asgi", "combined_lifespan"]
