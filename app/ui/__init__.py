"""The author dashboard. Pure Python, NiceGUI, no HTML/CSS/JS/npm anywhere.

    /            overview    revenue over time, call volume, realised fee load
    /tools       tools       per-tool revenue, volume, captured vs authorized
    /receipts    receipts    receipt explorer, filterable, links out to Basescan
    /economics   economics   fee load vs calls-per-batch -- the key visual

Layout:

    app/ui/theme.py      palette (validator-derived), page shell, chart defaults
    app/ui/queries.py    the read layer over the ledger -- SQLModel, integers only
    app/ui/pages/*.py    one `render()` per page, no route decorators

Two rules the whole package is built around:

1. **Never fabricate a number.** Every figure is read from the ledger. Where
   there is no data the page renders an explicit empty state that says what is
   missing and what would fill it -- never a placeholder, a sample series, or a
   "typical" value. The only computed-not-recorded numbers are the two model
   curves on the economics page, which are labelled MODEL in the legend, in the
   card subtitle and in a key beneath the chart, and are drawn beside the real
   settled batches, which are labelled LEDGER.

2. **Seeded data is labelled.** A demo payment session is one whose id starts
   with `sess_demo` or whose agent label starts with `demo` (see
   `app.ui.queries.DEMO_SESSION_PREFIX`). Wherever such a row can move a number,
   a banner counts it, every table cell carrying it is prefixed `DEMO`, and a
   filter excludes it. Demo rows are never silently mixed into real revenue.

Registration order note: these pages must be registered BEFORE `ui.run_with()`
mounts NiceGUI at "/", which `app.main` already guarantees by calling
`dashboard.install(app)` at step 6 of its composition.
"""

from __future__ import annotations

import logging

from nicegui import ui

from app.ui.pages import economics, overview, receipts, tools

log = logging.getLogger("eraya.ui")

#: path -> (page module render function, browser tab title)
ROUTES = (
    ("/", overview.render, "Overview"),
    ("/tools", tools.render, "Tools"),
    ("/receipts", receipts.render, "Receipts"),
    ("/economics", economics.render, "Economics"),
)

__all__ = ["install", "ROUTES"]


def install(app=None) -> None:
    """Register every dashboard page with NiceGUI.

    `app` is accepted and unused: `@ui.page` registers against NiceGUI's own
    router, which `ui.run_with(app)` later mounts onto the FastAPI app. Keeping
    the parameter means `app.dashboard.install(app)` and this share one signature.
    """
    for path, render, title in ROUTES:
        _register(path, render, title)
    log.info("dashboard: %d pages registered", len(ROUTES))


def _register(path: str, render, title: str) -> None:
    # A default argument, not a closure over the loop variable -- late binding
    # would otherwise give every route the last page's render function.
    @ui.page(path, title=f"{title} - ERAYA x BRAINWAVE")
    def _page(_render=render) -> None:
        _render()
