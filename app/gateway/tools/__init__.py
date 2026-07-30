"""The tool catalogue, module by module.

Registration is per-module and fails soft. One tool module that cannot import --
a missing optional dependency, a typo shipped on a Friday -- must not take the
whole gateway down with it: the other tools still sell, the failure is logged
with a traceback, and `list_paid_tools` shows a catalogue that is genuinely
short rather than a server that is genuinely down.

Order matters only in that `free` is registered first, so discovery and receipt
verification exist even if every paid module fails.
"""

from __future__ import annotations

import importlib
import logging

log = logging.getLogger(__name__)

__all__ = ["MODULES", "register_all"]

#: Module name (relative to this package) -> what it contributes.
MODULES: tuple[tuple[str, str], ...] = (
    ("free", "discovery + receipt verification (never paywalled)"),
    ("swarm", "prompt-injection and identity-spoof simulations"),
    ("casper", "Casper chain reads"),
    ("base_chain", "Base/EVM chain reads (Quicknode-backed, public RPC fallback)"),
    ("analysis", "LLM contract review (metered) and deterministic triage"),
    ("webtools", "Firecrawl-backed URL scraping"),
)


def register_all(mcp: object) -> list[str]:
    """Attach every tool module. Returns the names that registered cleanly."""
    registered: list[str] = []
    for name, purpose in MODULES:
        try:
            module = importlib.import_module(f"{__name__}.{name}")
            module.register(mcp)  # type: ignore[attr-defined]
        except Exception:
            log.exception("tool module %r (%s) failed to register", name, purpose)
            continue
        registered.append(name)
    return registered
