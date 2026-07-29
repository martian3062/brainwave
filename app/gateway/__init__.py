"""The paid MCP gateway.

    app.gateway.server            register_tools(mcp) -- the spine's seam
    app.gateway.paid              @paid, over x402's create_payment_wrapper
    app.gateway.meter             how a tool reports what it consumed
    app.gateway.requirements      the 402 challenge, built without a network
    app.gateway.resource_server   metered capture + deferred settlement
    app.gateway.ledger            Author / Tool / Session / Call / Batch / Receipt
    app.gateway.config            what the tools may reach (never the payment spine)
    app.gateway.tools             the catalogue itself

Scope, stated once and honestly: the x402 Python SDK already implements paid MCP.
`x402.mcp.create_payment_wrapper` owns the protocol -- 402 challenge, facilitator
verify, execute, settle, and payment metadata in the MCP `_meta` keys
`x402/payment` and `x402/payment-response`. Every paid call in this package goes
through it and we claim none of it.

What this package adds is what the SDK has no ledger and no opinion about:
metered capture for `upto`, per-author revenue accounting with a platform take,
session batching with receipts that admit when settlement has not happened yet,
and free, unpaywalled verification of every artefact it issues.
"""

from __future__ import annotations

__all__ = ["register_tools", "get_gateway_mcp"]


def register_tools(mcp: object) -> list[str]:
    """Attach the paid catalogue. Re-exported so `app.catalogue` stays a one-liner."""
    from app.gateway.server import register_tools as _register

    return _register(mcp)


def get_gateway_mcp() -> object:
    """The FastMCP instance the catalogue is attached to (the spine's singleton)."""
    from app.gateway.server import get_gateway_mcp as _get

    return _get()
