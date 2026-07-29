"""Async clients for everything outside this process.

Three rules hold for every function here, and they are why the app boots on an
empty `.env`:

1. **Nothing is constructed at import.** No client, no key check, no connection.
   A missing key is a call-time condition, not an import-time crash.
2. **Nothing raises.** Failures come back as a dict with `ok: false` and a
   readable reason, because a paid tool that raises gets swallowed by the SDK
   into "Tool execution error: ..." and the agent learns nothing useful.
3. **Nothing is written.** These are reads and simulations. The gateway never
   signs a Casper transaction, never mutates upstream state, and never touches
   the ERAYA backend with anything but GET or an explicitly simulated POST.

The Casper JSON-RPC shapes below are adapted from the ERAYA backend's
`core/casper/sdk.py` (read-only; copied rather than imported, so this project
has no runtime dependency on that codebase). The one substantive change is
`urllib.request` -> `httpx.AsyncClient`: the original is synchronous, and
blocking the event loop inside a paid MCP call would stall every other agent on
the server.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.gateway.config import gateway_settings as gw

log = logging.getLogger(__name__)

__all__ = ["eraya_get", "eraya_post", "casper_rpc", "MOTES_PER_CSPR"]

#: Casper's smallest unit. Integer division everywhere -- a balance is money and
#: `app.money`'s rule (no floats) applies to CSPR as much as to USDC.
MOTES_PER_CSPR = 1_000_000_000


def _unconfigured(what: str, env_var: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": f"{what} is not configured",
        "hint": f"set {env_var} in the environment to enable this tool",
        "engine": "unconfigured",
    }


# --------------------------------------------------------------------------
# ERAYA REST API
# --------------------------------------------------------------------------


async def eraya_get(path: str) -> dict[str, Any]:
    """GET against a running ERAYA backend."""
    if not gw.eraya_api_configured:
        return _unconfigured("ERAYA_API_BASE", "ERAYA_API_BASE")
    return await _eraya_request("GET", path)


async def eraya_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST against a running ERAYA backend (simulation endpoints only)."""
    if not gw.eraya_api_configured:
        return _unconfigured("ERAYA_API_BASE", "ERAYA_API_BASE")
    return await _eraya_request("POST", path, body)


async def _eraya_request(
    method: str, path: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    url = f"{gw.eraya_api_base.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=gw.eraya_api_timeout, follow_redirects=True) as client:
            response = await client.request(method, url, json=body)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        return {
            "ok": False,
            "error": f"upstream returned {exc.response.status_code}",
            "detail": exc.response.text[:400],
            "url": url,
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "error": str(exc)[:400], "url": url}

    return data if isinstance(data, dict) else {"ok": True, "result": data}


# --------------------------------------------------------------------------
# Casper 2.0 JSON-RPC
# --------------------------------------------------------------------------


async def casper_rpc(
    method: str, params: dict[str, Any] | list[Any] | None = None
) -> dict[str, Any]:
    """One JSON-RPC round trip against the configured Casper node.

    pycspr is deliberately not used. Its 0.12 `NodeConnection` builds
    `http://<host>:<port>/rpc`, so it cannot talk to a TLS Casper 2.0 endpoint
    like `node.testnet.casper.network`, and its RPC surface is still Casper
    1.x-shaped (`info_get_deploy` rather than `info_get_transaction`). The
    backend this is adapted from made the same call, for the same reason -- and
    since the gateway only reads, none of pycspr's crypto is needed here.
    """
    node = gw.casper_node_rpc_url.strip()
    if not node:
        return _unconfigured("Casper node", "CASPER_NODE_RPC_URL")

    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    try:
        async with httpx.AsyncClient(
            timeout=gw.casper_rpc_timeout, follow_redirects=True
        ) as client:
            response = await client.post(node, json=body)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"error": {"message": str(exc)[:400], "method": method, "node": node}}

    if not isinstance(data, dict):
        return {"error": {"message": "node returned a non-object response", "method": method}}
    return data


def rpc_error(response: dict[str, Any], fallback: str = "rpc failed") -> str:
    """Readable text out of a JSON-RPC error envelope."""
    error = response.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or fallback)
    return fallback
