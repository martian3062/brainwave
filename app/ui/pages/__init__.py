"""Dashboard pages. Each module exposes a single `render()` that draws its page.

`render()` deliberately does NOT carry the `@ui.page` decorator: routes are
registered in `app.ui.install()`, in one place, so the route table is readable
without opening four files and so importing a page module for testing does not
have the side effect of claiming a URL.
"""

from __future__ import annotations

from app.ui.pages import economics, overview, receipts, tools

__all__ = ["overview", "tools", "receipts", "economics"]
