"""Paid page scraping, via Firecrawl.

Priced `exact`: Firecrawl bills per page fetched regardless of page size, so
the cost is known before the tool runs -- same reasoning as
`summarize_bug_report`, not the metered `upto` reasoning `analyze_contract`
needs.

Without FIRECRAWL_API_KEY the tool reports itself unavailable and captures
nothing -- same contract as `analyze_contract` without ANTHROPIC_API_KEY: a
metered-looking tool must never charge for an answer it could not produce, and
neither may this one.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from app.gateway.config import gateway_settings as gw
from app.gateway.ledger import ToolSpec
from app.gateway.paid import paid
from app.gateway.tools._upstream import firecrawl_scrape
from app.models import Scheme

__all__ = ["register"]

SCRAPE = ToolSpec(
    name="scrape_url",
    description=(
        "Fetch a URL and return its cleaned content as markdown, via Firecrawl. Priced "
        "`exact` -- one page fetched is one charge, regardless of page size. Requires "
        "FIRECRAWL_API_KEY on the server; without it the tool reports itself unavailable "
        "and captures nothing."
    ),
    price_atomic=gw.scrape_url_atomic,
    scheme=Scheme.EXACT,
    tags=("scrape", "web", "content"),
    rationale="One Firecrawl page fetch; Firecrawl bills flat per page regardless of size.",
)


def _unavailable(reason: str, hint: str) -> dict[str, Any]:
    return {
        "ok": False,
        "engine": "unavailable",
        "error": reason,
        "hint": hint,
        "charged": "base price only; no upstream call was made",
    }


async def _scrape(url: str) -> dict[str, Any]:
    """Module-level so it is unit-testable without the paid/MCP machinery --
    same split as `app.gateway.tools.analysis._analyze`."""
    if not gw.firecrawl_configured:
        return _unavailable(
            "no Firecrawl credentials configured on this gateway",
            "set FIRECRAWL_API_KEY in the server environment",
        )

    result = await firecrawl_scrape(url)
    if not result.get("success"):
        return {
            "ok": False,
            "engine": "firecrawl",
            "url": url,
            "error": result.get("error") or "scrape failed",
        }

    data = result.get("data") or {}
    metadata = data.get("metadata") or {}
    markdown = data.get("markdown") or ""
    return {
        "ok": True,
        "engine": "firecrawl",
        "url": url,
        "title": metadata.get("title"),
        "statusCode": metadata.get("statusCode"),
        "markdown": markdown[:50_000],
        "charCount": len(markdown),
    }


def register(mcp: FastMCP) -> None:
    @mcp.tool(name=SCRAPE.name, description=SCRAPE.description)
    @paid(
        SCRAPE,
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to fetch and clean."}
            },
            "required": ["url"],
        },
        example={"url": "https://example.com"},
    )
    async def scrape_url(url: str) -> dict:
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "error": "url must be an absolute http(s) URL"}
        return await _scrape(url)
