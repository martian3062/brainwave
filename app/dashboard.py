"""The NiceGUI dashboard entry point -- pure Python, no HTML/CSS/JS/Node anywhere.

`install(app)` is the spine's only contract with the frontend, and it now hands
off to `app.ui`, which owns the real author dashboard:

    /            overview    revenue over time, call volume, realised fee load
    /tools       tools       per-tool revenue, volume, captured vs authorized
    /receipts    receipts    receipt explorer, links out to the block explorer
    /economics   economics   fee load vs calls-per-batch -- the key visual

`render_dashboard()` below stays as the fallback: if `app.ui` cannot be imported
(a missing dependency, a syntax error mid-edit) the gateway still serves a page
at `/` describing its own configuration, rather than 500ing the root route of a
running payment gateway. That fallback is a *degraded* mode and says so on screen.
"""

from __future__ import annotations

import logging

from nicegui import ui

from app.config import settings
from app.money import fee_load_bps, format_atomic

log = logging.getLogger("brainwave.dashboard")

# Brand palette. NiceGUI is styled in Python, so these are values passed to
# ui.colors() and inline styles -- there is no stylesheet in this project.
BG = "#1d0718"
FG = "#fbf4f2"
ACCENT = "#ff6f91"
ACCENT_DEEP = "#e6416f"
CREAM = "#fff3ec"


def apply_theme() -> None:
    ui.colors(primary=ACCENT, secondary=ACCENT_DEEP, accent=ACCENT, dark=BG)
    ui.query("body").style(f"background-color:{BG}; color:{FG}")


def _stat(label: str, value: str, sub: str = "") -> None:
    with (
        ui.column()
        .classes("gap-0 p-4 rounded-lg")
        .style(
            f"background:rgba(255,111,145,0.08); border:1px solid {ACCENT_DEEP}33; min-width:190px"
        )
    ):
        ui.label(label).style(f"color:{ACCENT}; font-size:0.72rem; letter-spacing:0.12em").classes(
            "uppercase"
        )
        ui.label(value).style(f"color:{CREAM}; font-size:1.6rem; font-weight:600")
        if sub:
            ui.label(sub).style(f"color:{FG}99; font-size:0.75rem")


def render_dashboard() -> None:
    """Degraded-mode `/` page, used only when `app.ui` fails to import."""
    apply_theme()

    d = settings.x402_asset_decimals
    sym = settings.x402_asset_symbol

    # The economic argument, computed rather than asserted: one $0.001
    # settlement against a single $0.002 call, versus the same fee spread over a
    # 100-call session.
    demo_price = 2_000  # $0.002 at 6 decimals
    fee = settings.facilitator_fee_atomic
    per_call_bps = fee_load_bps(fee, demo_price)
    batched_bps = fee_load_bps(fee, demo_price * 100)

    with ui.column().classes("w-full max-w-6xl mx-auto p-8 gap-6"):
        ui.label("TRAPPIST x BRAINWAVE").style(
            f"color:{CREAM}; font-size:2.1rem; font-weight:700; letter-spacing:-0.02em"
        )
        ui.label("MCP won the tool layer. This is its payment layer.").style(
            f"color:{ACCENT}; font-size:1.05rem"
        )
        ui.label(
            "Degraded mode: the author dashboard (app.ui) could not be loaded, so this "
            "page shows the gateway's configuration only. No ledger figures are "
            "displayed here -- see the server log for the import error."
        ).style(f"color:{ACCENT_DEEP}; font-size:0.82rem; max-width:70ch")

        with ui.row().classes("gap-4 flex-wrap"):
            _stat("network", settings.x402_network, "CAIP-2")
            _stat("asset", sym, settings.asset_address[:10] + "...")
            _stat(
                "settlement",
                "batched" if settings.batching_enabled else "per call",
                f"window {settings.batch_window_seconds}s / {settings.batch_max_calls} calls",
            )
            _stat(
                "fee load",
                f"{batched_bps / 100:.2f}%",
                f"vs {per_call_bps / 100:.1f}% settling every call",
            )

        with ui.column().classes("gap-1 pt-4"):
            ui.label("Endpoints").style(
                f"color:{ACCENT}; font-size:0.72rem; letter-spacing:0.12em"
            ).classes("uppercase")
            for label, value in (
                ("paid MCP", settings.mcp_public_url),
                ("ledger admin", settings.admin_path),
                ("health", "/healthz"),
            ):
                with ui.row().classes("gap-3 items-baseline"):
                    ui.label(label).style(f"color:{FG}99; width:8rem; font-size:0.85rem")
                    ui.label(value).style(f"color:{CREAM}; font-family:monospace")

        ui.label(
            f"Facilitator {settings.facilitator_url} - settlement costs "
            f"{format_atomic(fee, d, symbol=sym)} per on-chain event."
        ).style(f"color:{FG}88; font-size:0.8rem; padding-top:1rem")


def install(app) -> None:
    """Register NiceGUI pages. Must run before `ui.run_with(app)`.

    Delegates to `app.ui`, which owns the four dashboard pages. Only an import
    failure falls back to the single degraded page above -- a bug *inside* a page
    still raises at request time, where it belongs, instead of being hidden
    behind a placeholder that looks like a working dashboard.
    """
    try:
        from app.ui import install as install_dashboard
    except Exception:  # noqa: BLE001 -- degraded mode beats a dead root route
        log.exception("app.ui failed to import -- serving the degraded / page only")

        @ui.page("/")
        def _index() -> None:
            render_dashboard()

        return

    install_dashboard(app)
