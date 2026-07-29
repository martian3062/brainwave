"""The seam `app.mcp_app` looks for.

`app/mcp_app.py::_register_catalogue` imports `app.catalogue.register_tools` and
calls it with the FastMCP instance, tolerating both ImportError (not written
yet) and any exception (written, but broken). Tool registration happens during
module composition, before a local SQLite schema necessarily exists. Ledger
catalogue synchronisation therefore runs from `app.main`'s lifespan after
`create_all()` / Alembic, avoiding a first-boot race while keeping all payment
logic in `app.gateway`.
"""

from __future__ import annotations

from typing import Any

from app.gateway.server import register_tools as _register_tools


def register_tools(mcp: Any) -> list[str]:
    """Attach tools now; the application lifespan syncs their ledger rows."""
    return _register_tools(mcp, sync_ledger=False)


__all__ = ["register_tools"]
