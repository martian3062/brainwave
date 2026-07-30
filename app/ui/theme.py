"""Palette, page shell, and chart defaults. Pure Python -- no HTML, CSS, JS or npm.

NiceGUI is styled in Python: everything below is either a `.style()` string built
here, a Tailwind class handed to `.classes()`, or a plain dict fed to ECharts.
There is no stylesheet anywhere in this repository and there is no place to put
one.

================================================================================
COLOUR -- CHOSEN BY VALIDATOR, NOT BY EYE
================================================================================

The brand palette (bg #1d0718, fg #fbf4f2, accent #ff6f91, deep #e6416f, cream
#fff3ec) is a single warm hue family. That is right for chrome and wrong for
series identity: three pinks are three pinks under deuteranopia. So the chrome
keeps the brand values verbatim, and the *data* colours were derived by running
the palette validator against this surface (#1d0718, dark mode) and searching for
steps that clear every check, rather than by picking something that looked nice.

Categorical slots (identity: which series), assigned in FIXED order, never cycled:

    slot 1  #e6416f   the ERAYA deep accent -- kept, it passes on its own merits
    slot 2  #2299ee
    slot 3  #c08a1e

    Lightness band     PASS  all 3 inside OKLCH L 0.48-0.67 (dark band)
    Chroma floor       PASS  all 3 >= 0.10
    CVD separation     PASS  worst adjacent  #2299ee vs #e6416f  dE 20.5 (deutan)
                             worst all-pairs #c08a1e vs #e6416f  dE  9.1 (deutan)
    Normal-vision      PASS  worst adjacent dE 28.9 / worst all-pairs dE 20.4
    Contrast v surface PASS  all 3 >= 3:1

All-pairs is reported because the economics page plots a scatter, where any two
marks can end up adjacent. Three slots is also the cap: no ordering of eight
colours passes all-pairs, so a fourth series folds into "other" or gets its own
chart.

Ordinal ramp (position in a sequence -- the call-outcome funnel), one hue,
monotone lightness:

    #8a2746  #b0305a  #d13c6a  #ef5b80  #ffa6bd
    monotone PASS - adjacent dL >= 0.06 PASS - light-end 2.24:1 PASS - hue spread 5 deg

DE_EMPHASIS (#8f7f88) is deliberately grey and deliberately below the chroma
floor: it is not a categorical slot. It carries the series we are arguing
*against* (per-call settlement) using the emphasis pattern -- highlight one, grey
the rest -- and is always direct-labelled, so it never relies on hue to be read.

Status inks are text-plus-icon only, never a chart series, so a status colour can
never impersonate an identity slot. Each clears WCAG text contrast on the
surface: good 8.63:1, warn 9.80:1, bad 8.45:1.

================================================================================
NUMBERS
================================================================================

Every number a human READS is formatted from the ledger's integer atomic units by
`app.money`. Floats appear in exactly one place -- the coordinate arrays handed to
ECharts, because a chart is pixels and JSON has no integers-with-scale. Nothing is
ever read back out of a chart. `chart_value()` is the only converter and it is
one-way on purpose.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from nicegui import ui

from app.config import settings
from app.money import to_decimal

__all__ = [
    "BG",
    "SURFACE",
    "SURFACE_HI",
    "GRID",
    "FG",
    "CREAM",
    "ACCENT",
    "ACCENT_DEEP",
    "INK",
    "INK_2",
    "INK_MUTED",
    "SERIES",
    "ORDINAL",
    "DE_EMPHASIS",
    "OK_INK",
    "WARN_INK",
    "BAD_INK",
    "apply_theme",
    "page_shell",
    "card",
    "eyebrow",
    "hero",
    "stat",
    "stat_row",
    "empty_state",
    "demo_banner",
    "demo_badge",
    "note",
    "kv",
    "usd",
    "usd_exact",
    "pct_bps",
    "compact_int",
    "shorten",
    "chart_value",
    "chart_options",
    "axis_category",
    "axis_value",
    "axis_log",
    "bar_series",
    "line_series",
    "scatter_series",
    "alpha",
    "glass",
    "PEACH",
    "PEACH_SOFT",
    "table_view",
]

# --------------------------------------------------------------------------
# Brand chrome -- the ERAYA values, verbatim.
# --------------------------------------------------------------------------
BG = "#1d0718"  # page surface
FG = "#fbf4f2"  # primary ink
ACCENT = "#ff6f91"  # eyebrows, links, focus
ACCENT_DEEP = "#e6416f"  # also categorical slot 1
CREAM = "#fff3ec"  # headings and figures

# Derived chrome. One step off the surface each, so they recede.
SURFACE = "#2a1122"  # card fill                     (1.10:1 vs BG -- recessive)
SURFACE_HI = "#341829"  # hovered / nested fill
GRID = "#3a2130"  # hairline gridlines and axes   (1.31:1 vs BG -- recessive)

INK = FG  # primary text     17.6:1
INK_2 = "#b9a7b1"  # secondary text    8.4:1
INK_MUTED = "#8f7f88"  # axis ticks, captions  5.1:1

# --------------------------------------------------------------------------
# Retro-peach glass chrome -- layered OVER the chrome above, for surfaces and
# backgrounds only. Never used for SERIES/ORDINAL/status ink: those three keep
# the validator report from the module docstring untouched. This section is
# purely decorative (page background, card fills, nav), so it carries none of
# that report's constraints.
# --------------------------------------------------------------------------
PEACH = "#ffb27a"  # warm mid-tone, bridges ACCENT to CREAM
PEACH_SOFT = "#ffd9b3"  # pale peach, gradient terminus
GLASS_BORDER = "rgba(255,243,236,0.14)"  # CREAM hairline, low alpha

#: The retro-sunset backdrop: BG -> deep accent -> accent -> peach -> soft
#: peach, the classic dark-to-warm synthwave ramp. Animated in `apply_theme`.
RETRO_GRADIENT = (
    f"linear-gradient(120deg, {BG} 0%, {ACCENT_DEEP} 32%, {ACCENT} 55%, "
    f"{PEACH} 78%, {PEACH_SOFT} 100%)"
)

# --------------------------------------------------------------------------
# Data colour. See the module docstring for the validator report.
# --------------------------------------------------------------------------
#: Categorical identity, fixed order, never cycled. Three is the cap here.
SERIES = ("#e6416f", "#2299ee", "#c08a1e")

#: Ordinal, one hue, dark -> light. Position in a sequence, not identity.
ORDINAL = ("#8a2746", "#b0305a", "#d13c6a", "#ef5b80", "#ffa6bd")

#: Not a slot. The greyed-out comparison series in the emphasis pattern.
DE_EMPHASIS = "#8f7f88"

#: Status. Text + icon + label only. Never a series.
OK_INK = "#5cc27a"
WARN_INK = "#e8b04b"
BAD_INK = "#ff8a8a"

_TONES = {"default": CREAM, "ok": OK_INK, "warn": WARN_INK, "bad": BAD_INK, "accent": ACCENT}


def alpha(hex_color: str, a: float) -> str:
    """`'#e6416f', 0.10 -> 'rgba(230,65,111,0.1)'`. Area fills are washes."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{a})"


def glass(tint: str = CREAM, opacity: float = 0.08, *, blur: int = 18, glow: str = "") -> str:
    """Frosted-glass panel: translucent tint, blurred backdrop, hairline border,
    soft shadow -- meant to sit over `RETRO_GRADIENT`, not a flat surface.

    `glow` is an optional extra colour for a soft outer glow (the retro neon
    touch); pass a brand colour for hero surfaces, leave empty for ordinary
    cards so the effect stays subtle rather than novelty.
    """
    shadow = "0 8px 32px rgba(0,0,0,0.28), inset 0 1px 0 rgba(255,255,255,0.06)"
    if glow:
        shadow = f"0 0 40px {alpha(glow, 0.18)}, {shadow}"
    return (
        f"background:{alpha(tint, opacity)};"
        f"backdrop-filter:blur({blur}px);-webkit-backdrop-filter:blur({blur}px);"
        f"border:1px solid {GLASS_BORDER};"
        f"box-shadow:{shadow};"
    )


# --------------------------------------------------------------------------
# Money and number formatting.
#
# `to_decimal` is exact. Everything below only ever *shortens* the rendering; no
# rounding that could change a value ever reaches the screen without the unit
# being visible next to it.
# --------------------------------------------------------------------------


def _decimals(decimals: int | None) -> int:
    return settings.x402_asset_decimals if decimals is None else decimals


def usd_exact(atomic: int, decimals: int | None = None, *, symbol: bool = True) -> str:
    """Full precision, every decimal place the asset has. For receipts and audit."""
    d = _decimals(decimals)
    text = f"{to_decimal(int(atomic), d):.{d}f}"
    return f"{text} {settings.x402_asset_symbol}" if symbol else text


def usd(atomic: int, decimals: int | None = None, *, symbol: bool = False) -> str:
    """Lossless short form: drops trailing zeros, keeps at least two places.

    `2000 -> '0.002'`, `1000000 -> '1.00'`. Nothing is rounded away -- if a digit
    would be lost it stays.
    """
    d = _decimals(decimals)
    text = f"{to_decimal(int(atomic), d):.{d}f}"
    if "." in text:
        text = text.rstrip("0")
        whole, _, frac = text.partition(".")
        text = f"{whole}.{frac.ljust(2, '0')}"
    return f"{text} {settings.x402_asset_symbol}" if symbol else text


def pct_bps(bps: int) -> str:
    """Basis points as a percentage. `5000 -> '50.00%'`."""
    return f"{bps / 100:.2f}%"


def compact_int(n: int) -> str:
    """Thousands-separated. Counts are counts; they are never abbreviated to 1.2K
    in a ledger view where the exact number is the point."""
    return f"{n:,}"


def shorten(text: str | None, head: int = 10, tail: int = 6) -> str:
    """Middle-elide an address or hash: `0x1234...abcdef`."""
    if not text:
        return "--"
    return text if len(text) <= head + tail + 3 else f"{text[:head]}...{text[-tail:]}"


def chart_value(atomic: int, decimals: int | None = None) -> float:
    """THE ONLY int -> float conversion in the dashboard. Charts are pixels.

    Never read a number back out of a chart; read it from `usd()`/`usd_exact()`,
    which go straight from the ledger's integers.
    """
    return float(to_decimal(int(atomic), _decimals(decimals)))


# --------------------------------------------------------------------------
# Page chrome.
# --------------------------------------------------------------------------

NAV: tuple[tuple[str, str, str], ...] = (
    ("/", "Overview", "insights"),
    ("/tools", "Tools", "handyman"),
    ("/receipts", "Receipts", "receipt_long"),
    ("/economics", "Economics", "trending_down"),
)


_RETRO_CSS = f"""
<style>
@keyframes retro-shift {{
    0%   {{ background-position: 0% 50%; }}
    50%  {{ background-position: 100% 50%; }}
    100% {{ background-position: 0% 50%; }}
}}
.retro-gradient-bg {{
    background: linear-gradient(120deg, {BG}, {ACCENT_DEEP}, {ACCENT}, {PEACH}, {PEACH_SOFT}, {ACCENT}, {ACCENT_DEEP});
    background-size: 400% 400%;
    animation: retro-shift 24s ease-in-out infinite;
}}
</style>
"""


def apply_theme() -> None:
    ui.colors(primary=ACCENT, secondary=ACCENT_DEEP, accent=ACCENT, dark=BG)
    # The only <style> tag in the project: a keyframe animation, which NiceGUI's
    # inline .style()/.classes() API has no way to express. Still pure Python --
    # generated from the palette constants above, not a separate stylesheet file.
    ui.add_head_html(_RETRO_CSS)
    ui.query("body").style(f"background-color:{BG}; color:{INK}")
    # Quasar paints its own surface behind the page; match it or cards float on grey.
    ui.query(".nicegui-content").style("padding:0")


def _aside(active: str):
    """The nav 'aside' -- a transparent glass drawer over the retro gradient.

    Returns the drawer so `_nav_bar` can wire a toggle button to it.
    """
    drawer = ui.left_drawer(value=True).classes("gap-1 p-3").style(glass(CREAM, 0.05, blur=22))
    with drawer:
        with ui.row().classes("items-center gap-2 px-1 pb-3"):
            ui.icon("bolt").style(f"color:{ACCENT}; font-size:1.3rem")
            ui.label("ERAYA").style(f"color:{CREAM}; font-weight:700; letter-spacing:0.02em")
        for path, label, icon in NAV:
            on = path == active
            with (
                ui.link(target=path)
                .classes("no-underline w-full px-3 py-2 rounded-lg flex items-center gap-3")
                .style(
                    f"background:{alpha(ACCENT_DEEP, 0.22) if on else 'transparent'};"
                    f"color:{CREAM if on else INK_2};"
                    + (f"box-shadow:0 0 20px {alpha(ACCENT, 0.15)};" if on else "")
                )
            ):
                ui.icon(icon).style("font-size:1.1rem")
                ui.label(label).style("font-size:0.85rem; font-weight:500")
    return drawer


def _nav_bar(drawer) -> None:
    with (
        ui.row()
        .classes("w-full items-center gap-4 px-6 py-4 sticky top-0 z-10")
        .style(glass(CREAM, 0.06, blur=20, glow=ACCENT))
    ):
        ui.button(icon="menu", on_click=drawer.toggle).props("flat round dense").style(
            f"color:{CREAM}"
        )
        with ui.column().classes("gap-0"):
            ui.label("ERAYA x BRAINWAVE").style(
                f"color:{CREAM}; font-size:1rem; font-weight:700; letter-spacing:-0.01em"
            )
            ui.label("MCP won the tool layer. This is its payment layer.").style(
                f"color:{ACCENT}; font-size:0.72rem"
            )
        ui.space()
        with ui.row().classes("items-center gap-3"):
            _pill(settings.x402_network, "accent" if settings.is_mainnet else "default")
            _pill("batched" if settings.batching_enabled else "per-call settlement")


def _pill(text: str, tone: str = "default") -> None:
    ui.label(text).classes("px-2 py-1 rounded").style(
        f"background:{alpha(_TONES.get(tone, CREAM), 0.12)};"
        f"color:{_TONES.get(tone, INK_2)}; font-size:0.7rem; font-family:monospace"
    )


@contextmanager
def page_shell(active: str, title: str, subtitle: str = "") -> Iterator[None]:
    """Aside, nav bar, page heading, and a max-width body column. Used by every page."""
    apply_theme()
    drawer = _aside(active)
    with ui.column().classes("w-full min-h-screen gap-0 retro-gradient-bg"):
        _nav_bar(drawer)
        with ui.column().classes("w-full max-w-[1400px] mx-auto px-6 py-6 gap-5"):
            with ui.column().classes("gap-1"):
                ui.label(title).style(
                    f"color:{CREAM}; font-size:1.7rem; font-weight:700; letter-spacing:-0.02em"
                )
                if subtitle:
                    ui.label(subtitle).style(f"color:{INK_2}; font-size:0.9rem; max-width:70ch")
            yield
            _footer()


def _footer() -> None:
    with (
        ui.row()
        .classes("w-full items-center gap-4 pt-6 mt-2")
        .style(f"border-top:1px solid {GRID}")
    ):
        ui.label(
            f"Ledger: {settings.x402_asset_symbol} on {settings.x402_network} - "
            f"facilitator {settings.facilitator_label}"
        ).style(f"color:{INK_MUTED}; font-size:0.75rem")
        ui.space()
        ui.link("MCP endpoint", settings.mcp_public_url).style(
            f"color:{INK_MUTED}; font-size:0.75rem"
        )
        ui.link("raw ledger", f"{settings.admin_path}/").style(
            f"color:{INK_MUTED}; font-size:0.75rem"
        )
        ui.link("health", "/healthz").style(f"color:{INK_MUTED}; font-size:0.75rem")


@contextmanager
def card(title: str = "", subtitle: str = "") -> Iterator[None]:
    with ui.column().classes("w-full gap-3 p-5 rounded-xl").style(glass(CREAM, 0.05)):
        if title:
            with ui.column().classes("gap-0"):
                ui.label(title).style(f"color:{CREAM}; font-size:1rem; font-weight:600")
                if subtitle:
                    ui.label(subtitle).style(f"color:{INK_2}; font-size:0.8rem; max-width:80ch")
        yield


def eyebrow(text: str) -> None:
    ui.label(text).classes("uppercase").style(
        f"color:{ACCENT}; font-size:0.68rem; letter-spacing:0.14em; font-weight:600"
    )


def hero(label: str, value: str, sub: str = "") -> None:
    """The one number a view leads with. Same sans as everything else,
    proportional figures -- tabular-nums makes a big number look loose."""
    with ui.column().classes("gap-1"):
        eyebrow(label)
        ui.label(value).style(
            f"color:{CREAM}; font-size:3rem; line-height:1.05; font-weight:650;"
            "letter-spacing:-0.03em"
        )
        if sub:
            ui.label(sub).style(f"color:{INK_2}; font-size:0.85rem")


def stat(label: str, value: str, sub: str = "", tone: str = "default", icon: str = "") -> None:
    with (
        ui.column()
        .classes("gap-1 p-4 rounded-xl flex-1")
        .style(glass(_TONES.get(tone, CREAM), 0.06, blur=14) + "min-width:180px")
    ):
        ui.label(label).classes("uppercase").style(
            f"color:{INK_MUTED}; font-size:0.66rem; letter-spacing:0.12em"
        )
        with ui.row().classes("items-center gap-2 no-wrap"):
            if icon:
                ui.icon(icon).style(f"color:{_TONES.get(tone, CREAM)}; font-size:1.1rem")
            ui.label(value).style(
                f"color:{_TONES.get(tone, CREAM)}; font-size:1.55rem; font-weight:600;"
                "letter-spacing:-0.02em"
            )
        if sub:
            ui.label(sub).style(f"color:{INK_2}; font-size:0.75rem")


@contextmanager
def stat_row() -> Iterator[None]:
    with ui.row().classes("w-full gap-4 flex-wrap items-stretch"):
        yield


def kv(label: str, value: str, *, mono: bool = False, tone: str = "default") -> None:
    with ui.row().classes("w-full items-baseline gap-3 no-wrap"):
        ui.label(label).style(f"color:{INK_MUTED}; font-size:0.78rem; min-width:9rem")
        ui.label(value).style(
            f"color:{_TONES.get(tone, INK)}; font-size:0.85rem;"
            + ("font-family:monospace; word-break:break-all" if mono else "")
        )


def note(text: str, tone: str = "default", icon: str = "info") -> None:
    colour = _TONES.get(tone, INK_2)
    with (
        ui.row()
        .classes("items-start gap-2 p-3 rounded-lg w-full")
        .style(
            f"background:{alpha(colour, 0.10)}; border:1px solid {alpha(colour, 0.25)};"
            "backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);"
        )
    ):
        ui.icon(icon).style(f"color:{colour}; font-size:1rem; margin-top:1px")
        ui.label(text).style(f"color:{INK}; font-size:0.8rem; max-width:90ch")


def empty_state(title: str, detail: str, icon: str = "inbox") -> None:
    """The honest alternative to inventing numbers.

    Nothing on this dashboard renders a placeholder value, a sample series or a
    "typical" figure. If the ledger has no rows, the view says so in words and
    says what would put rows there.
    """
    with (
        ui.column()
        .classes("w-full items-center justify-center gap-2 py-10")
        .style(f"border:1px dashed {GRID}; border-radius:0.75rem")
    ):
        ui.icon(icon).style(f"color:{INK_MUTED}; font-size:2rem")
        ui.label(title).style(f"color:{INK}; font-size:0.95rem; font-weight:600")
        ui.label(detail).classes("text-center").style(
            f"color:{INK_MUTED}; font-size:0.8rem; max-width:60ch"
        )


def demo_badge(text: str = "DEMO DATA") -> None:
    ui.label(text).classes("px-2 py-1 rounded").style(
        f"background:{alpha(WARN_INK, 0.15)}; color:{WARN_INK}; font-size:0.7rem;"
        "font-weight:700; letter-spacing:0.08em"
    )


def demo_banner(summary, *, excluded: bool = False) -> None:
    """Seeded rows are labelled, loudly, wherever they can affect a number.

    The wording is `app.demo.BANNER` -- that module owns it, and it names the
    command that produced the rows and states that the transaction hashes are
    synthetic, which is the specific thing a viewer would otherwise assume.

    Three states, because they carry different risks:

      only_demo  everything on screen is fabricated -- say so at the top.
      mixed      real and seeded revenue in one figure. The worst case to show
                 silently, so the per-row DEMO prefix does the work and this
                 banner explains why the totals move when the filter flips.
      excluded   the filter is on; the banner becomes a reassurance, not a
                 warning, and says exactly what is hidden.
    """
    from app.demo import BANNER

    if excluded:
        note(
            f"Demo data is being EXCLUDED. {compact_int(summary.counts.get('call', 0))} seeded "
            f"call(s) worth {usd(summary.demo_captured_atomic, symbol=True)} are hidden from "
            "every figure on this page. Turn the filter off to see them, labelled.",
            tone="ok",
            icon="filter_alt_off",
        )
        return

    detail = (
        "Every figure on this page is fabricated."
        if summary.only_demo
        else (
            f"Figures below MIX real and seeded rows: "
            f"{usd(summary.demo_captured_atomic, symbol=True)} of the captured total is demo, "
            f"{usd(summary.real_captured_atomic, symbol=True)} is real. Seeded rows are "
            "prefixed DEMO in every table; use 'exclude demo data' for real revenue only."
        )
    )
    note(f"{BANNER} {detail}", tone="warn", icon="science")


# --------------------------------------------------------------------------
# ECharts defaults -- the mark specs, in one place.
#
# Thin marks, hairline SOLID gridlines one step off the surface, no dashes, a
# tooltip on every chart by default, and a legend whenever there are two or more
# series (a single series takes none -- the card title already names it).
#
# Formatter strings below are ECharts' own template syntax ('{value}%'), not
# JavaScript. There is no JS anywhere in this project.
# --------------------------------------------------------------------------

_TOOLTIP = {
    "backgroundColor": SURFACE_HI,
    "borderColor": GRID,
    "borderWidth": 1,
    "padding": [8, 12],
    "textStyle": {"color": INK, "fontSize": 12},
}


def chart_options(
    *,
    series: list[dict],
    x_axis: dict,
    y_axis: dict,
    legend: bool = False,
    tooltip_trigger: str = "axis",
    tooltip_formatter: str | None = None,
    grid: dict | None = None,
) -> dict:
    """The house style. Pages supply data; nothing else."""
    pointer = {
        "type": "line" if tooltip_trigger == "axis" else "none",
        "lineStyle": {"color": alpha(ACCENT, 0.5), "width": 1, "type": "solid"},
        "label": {"backgroundColor": SURFACE_HI, "color": INK, "borderColor": GRID},
    }
    tooltip = {**_TOOLTIP, "trigger": tooltip_trigger, "axisPointer": pointer}
    if tooltip_formatter:
        tooltip["formatter"] = tooltip_formatter

    options: dict = {
        "backgroundColor": "transparent",
        "textStyle": {"color": INK_2, "fontFamily": "inherit"},
        "animationDuration": 260,
        # Bottom leaves room for the x-axis band; a fixed height that clips the
        # axis is the classic way to get a tiny nested scrollbar inside a card.
        "grid": grid
        or {"left": 8, "right": 24, "top": 34 if legend else 12, "bottom": 8, "containLabel": True},
        "tooltip": tooltip,
        "xAxis": x_axis,
        "yAxis": y_axis,
        "series": series,
    }
    if legend:
        options["legend"] = {
            "top": 0,
            "left": 0,
            "icon": "roundRect",
            "itemWidth": 10,
            "itemHeight": 10,
            "itemGap": 18,
            # Legend text wears an ink token, never the series colour.
            "textStyle": {"color": INK_2, "fontSize": 12},
            "inactiveColor": GRID,
        }
    return options


def _axis_common() -> dict:
    return {
        "axisLine": {"show": True, "lineStyle": {"color": GRID, "width": 1}},
        "axisTick": {"show": False},
        "axisLabel": {"color": INK_MUTED, "fontSize": 11},
        "nameTextStyle": {"color": INK_MUTED, "fontSize": 11, "padding": [0, 0, 0, 0]},
        "splitLine": {"show": False},
    }


def axis_category(data: list[str], *, name: str = "", rotate: int = 0) -> dict:
    axis = {**_axis_common(), "type": "category", "data": data, "boundaryGap": True}
    if name:
        axis["name"] = name
        axis["nameLocation"] = "middle"
        axis["nameGap"] = 30
    if rotate:
        axis["axisLabel"] = {**axis["axisLabel"], "rotate": rotate}
    return axis


def axis_value(
    *, name: str = "", formatter: str | None = None, split: bool = True, max_: float | None = None
) -> dict:
    axis = {**_axis_common(), "type": "value"}
    if split:
        # Solid hairline, one step off the surface. Never dashed: a dashed grid
        # reads as "threshold" when it is just a grid.
        axis["splitLine"] = {
            "show": True,
            "lineStyle": {"color": GRID, "width": 1, "type": "solid"},
        }
    if formatter:
        axis["axisLabel"] = {**axis["axisLabel"], "formatter": formatter}
    if name:
        axis["name"] = name
        axis["nameLocation"] = "end"
        axis["nameGap"] = 12
    if max_ is not None:
        axis["max"] = max_
    return axis


def axis_log(*, name: str = "", formatter: str | None = None) -> dict:
    """Log x. Note: this is an X axis. A second *Y* scale is never allowed."""
    axis = {**_axis_common(), "type": "log", "logBase": 10, "min": 1}
    if formatter:
        axis["axisLabel"] = {**axis["axisLabel"], "formatter": formatter}
    if name:
        axis["name"] = name
        axis["nameLocation"] = "middle"
        axis["nameGap"] = 30
    return axis


def bar_series(
    name: str,
    data: list,
    colour: str,
    *,
    stack: str | None = None,
    horizontal: bool = False,
    label: str | None = None,
) -> dict:
    """<=24px thick, 4px rounded data-end, square at the baseline.

    Stacked segments are separated by a 2px gap IN THE SURFACE COLOUR (1px of
    border on each of the two touching segments) -- never by a stroke drawn round
    the mark, which would add ink that is not data.

    `data` may be plain numbers, or `{'value': n, 'itemStyle': {...}}` items when
    an ORDINAL ramp is colouring the bars by their position in a sequence.
    """
    # Round only the growing end; the baseline end stays square.
    radius = [0, 4, 4, 0] if horizontal else [4, 4, 0, 0]
    item: dict = {"color": colour, "borderRadius": radius}
    if stack:
        item["borderColor"] = SURFACE
        item["borderWidth"] = 1
    out: dict = {
        "name": name,
        "type": "bar",
        "data": data,
        "barMaxWidth": 24,
        "itemStyle": item,
        "emphasis": {"focus": "series"},
    }
    if stack:
        out["stack"] = stack
    if label:
        # Value at the tip (bars) / on the cap (columns). Text wears an ink
        # token; the coloured bar beside it carries identity.
        out["label"] = {
            "show": True,
            "position": "right" if horizontal else "top",
            "formatter": label,
            "color": INK_2,
            "fontSize": 11,
        }
    return out


def line_series(
    name: str,
    data: list,
    colour: str,
    *,
    area: bool = False,
    symbols: bool = False,
    end_label: str | None = None,
    width: int = 2,
) -> dict:
    """2px line, round caps, >=8px markers with a 2px surface ring."""
    out: dict = {
        "name": name,
        "type": "line",
        "data": data,
        "smooth": False,
        "showSymbol": symbols,
        "symbol": "circle",
        "symbolSize": 8,
        "lineStyle": {"color": colour, "width": width, "cap": "round", "join": "round"},
        "itemStyle": {"color": colour, "borderColor": SURFACE, "borderWidth": 2},
        "emphasis": {"focus": "series"},
        # Bigger hit area than the mark, so a 8px dot is not a pinpoint target.
        "triggerLineEvent": True,
    }
    if area:
        out["areaStyle"] = {"color": alpha(colour, 0.10)}
    if end_label:
        # Selective direct label: the endpoint only. Text wears an ink token; the
        # coloured line beside it carries identity.
        out["endLabel"] = {
            "show": True,
            "formatter": end_label,
            "color": INK_2,
            "fontSize": 11,
            "distance": 8,
        }
    return out


def scatter_series(
    name: str,
    data: list,
    colour: str,
    *,
    size: int = 11,
    symbol: str = "circle",
    hollow: bool = False,
) -> dict:
    """Markers carry a 2px surface ring so overlapping points stay legible.

    `hollow` + a different `symbol` is the SECONDARY ENCODING used to separate
    seeded observations from real ones. Deliberately not a fourth hue: the
    categorical palette is capped at three slots under the all-pairs CVD check,
    which is the check that applies to scatter, and "this data is fake" is far
    too important to encode in colour alone.
    """
    item = (
        {"color": "transparent", "borderColor": colour, "borderWidth": 2}
        if hollow
        else {"color": colour, "borderColor": SURFACE, "borderWidth": 2}
    )
    return {
        "name": name,
        "type": "scatter",
        "data": data,
        "symbol": symbol,
        "symbolSize": size,
        "itemStyle": item,
        "emphasis": {"scale": 1.4},
    }


@contextmanager
def table_view(label: str = "Table view") -> Iterator[None]:
    """Every chart ships a table twin -- the WCAG-clean way to read the same
    values without relying on colour or on a hover tooltip."""
    with (
        ui.expansion(label, icon="table_rows")
        .classes("w-full")
        .style(f"color:{INK_2}; font-size:0.8rem; border-top:1px solid {GRID}")
    ):
        yield


def data_table(
    columns: list[dict],
    rows: list[dict],
    *,
    row_key: str = "id",
    pagination: int | dict | None = None,
):
    """A `ui.table` in house style. `tabular-nums` here and only here: columns of
    numbers must align vertically."""
    table = ui.table(columns=columns, rows=rows, row_key=row_key, pagination=pagination)
    table.classes("w-full").style(
        f"background:{SURFACE}; color:{INK}; font-variant-numeric:tabular-nums;"
        f"border:1px solid {GRID}"
    )
    table.props("flat dense bordered dark")
    return table
