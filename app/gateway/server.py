"""The gateway's entry point into the spine.

`app.main` mounts ONE FastMCP instance -- the one `app.mcp_app.get_mcp()` builds
and caches. That is not negotiable: `streamable_http_app()` lazily creates the
StreamableHTTP session manager on first call, and the lifespan that starts it is
bound to that exact instance. A second FastMCP object here would produce a
second session manager that nothing ever starts, and every tools/call against it
would 500.

So `get_gateway_mcp()` below RETURNS THE SPINE'S SINGLETON. It exists because
the gateway should be describable on its own terms ("here is the MCP server this
package produces") without that description being a lie about how many servers
there are.

Wiring, end to end:

    app/main.py            mcp = get_mcp(); app.mount("/mcp", mcp.streamable_http_app())
      app/mcp_app.py       build_mcp() -> _register_free_tools -> _register_catalogue
        app/catalogue.py   register_tools(mcp)  <- the seam the spine left
          THIS MODULE      register_tools(mcp)  -> attach tools
          app.main         sync_registered_catalogue() after the schema exists

Nothing in `app/main.py` or `app/mcp_app.py` needed to change for the catalogue
to appear; the seam was already there and this fills it.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "register_tools",
    "sync_registered_catalogue",
    "get_gateway_mcp",
    "build_standalone_mcp",
]


def register_tools(mcp: Any, *, sync_ledger: bool = True) -> list[str]:
    """Attach the paid catalogue to a FastMCP instance and sync the ledger.

    Called by `app.catalogue.register_tools`, which is what `app.mcp_app`
    imports. Returns the tool modules that registered, for logging and tests.

    The ledger sync happens AFTER registration and is best-effort: a database
    that is not migrated yet must not prevent the MCP server from starting and
    serving its free discovery tools. The paid tools will 402 correctly either
    way -- what a missing Tool row costs is the accounting entry, and that is
    logged loudly rather than fatally.
    """
    from app.gateway.paid import REGISTRY
    from app.gateway.tools import register_all

    registered = register_all(mcp)
    log.info(
        "gateway: %d tool module(s) registered (%s); %d paid tools",
        len(registered),
        ", ".join(registered) or "none",
        len(REGISTRY),
    )

    if sync_ledger:
        sync_registered_catalogue()
    return registered


def sync_registered_catalogue() -> None:
    """Upsert the registered paid tools after the application schema exists."""
    from app.gateway.paid import REGISTRY

    if REGISTRY:
        from app.gateway.ledger import sync_catalogue

        sync_catalogue(list(REGISTRY))


def get_gateway_mcp() -> Any:
    """The FastMCP instance this gateway's tools live on.

    Deliberately delegates to the spine's singleton rather than constructing
    one. See the module docstring: two instances means two session managers and
    a 500 on every paid call.
    """
    from app.mcp_app import get_mcp

    return get_mcp()


def build_standalone_mcp(name: str = "eraya-brainwave-gateway") -> Any:
    """A fresh FastMCP carrying only this gateway's tools.

    For tests and for running the catalogue over stdio against a desktop MCP
    client. NEVER use this for the mounted HTTP server -- `app.main` must mount
    the spine's singleton, for the session-manager reason above.
    """
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(name=name)
    register_tools(mcp)
    return mcp
