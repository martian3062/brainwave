"""Palette, page shell, and chart defaults. Pure Python -- no JS or npm.

The dashboard intentionally uses a quiet light system: milky-white page chrome,
white data surfaces, restrained peach accents, and dark warm-grey text. Peach is
reserved for navigation, focus, and the primary data series so it remains useful
instead of becoming visual noise.

Categorical chart colours are fixed in order and deliberately separate warm,
blue, and ochre hues. The ordinal ramp stays monotone from dark to light. Status
colours are used only with text and icons, never as categorical chart slots.

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
# Light dashboard chrome.
# --------------------------------------------------------------------------
BG = "#fbf8f4"  # milky page surface
FG = "#302a27"  # primary ink
INK = FG
INK_2 = "#625a55"  # secondary text
INK_MUTED = "#817873"  # axis ticks and captions

SURFACE = "#ffffff"  # cards and charts
SURFACE_HI = "#fff6f0"  # selected and nested surfaces
GRID = "#e8ded7"  # axes and hairlines

ACCENT = "#e88c69"  # peach focus and eyebrow
ACCENT_DEEP = "#c96849"  # primary action and chart slot 1
PEACH = "#f3b596"
PEACH_SOFT = "#fce7da"
CREAM = FG  # legacy name used by headings and figures
GLASS_BORDER = "rgba(89,71,61,0.12)"

PAGE_GRADIENT = f"linear-gradient(180deg, #fffdf9 0%, {BG} 72%, #fff3ea 100%)"

# --------------------------------------------------------------------------
# Data colour. Fixed tokens keep series identity consistent across every page.
# --------------------------------------------------------------------------
#: Categorical identity, fixed order, never cycled. Three is the cap here.
SERIES = (ACCENT_DEEP, "#2f718c", "#9a6a2f")

#: Ordinal, one hue, dark -> light. Position in a sequence, not identity.
ORDINAL = ("#9f432d", "#bc583b", "#d87555", "#e99c80", "#f3c3b0")

#: Not a slot. The greyed-out comparison series in the emphasis pattern.
DE_EMPHASIS = "#9a928d"

#: Status. Text + icon + label only. Never a series.
OK_INK = "#2f7a55"
WARN_INK = "#996515"
BAD_INK = "#b54845"

_TONES = {"default": CREAM, "ok": OK_INK, "warn": WARN_INK, "bad": BAD_INK, "accent": ACCENT}


def alpha(hex_color: str, a: float) -> str:
    """`'#c96849', 0.10 -> 'rgba(201,104,73,0.1)'`. Area fills are washes."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{a})"


def glass(tint: str = SURFACE, opacity: float = 0.08, *, blur: int = 18, glow: str = "") -> str:
    """Return the shared clean panel style.

    The name is retained for the public theme API, but panels are now opaque and
    calm instead of frosted. Non-default tints become a very light wash.
    """
    del blur
    fill = (
        SURFACE
        if tint == SURFACE
        else f"linear-gradient({alpha(tint, opacity)}, {alpha(tint, opacity)}), {SURFACE}"
    )
    shadow = "0 8px 24px rgba(73,55,45,0.07)"
    if glow:
        shadow = f"0 0 0 3px {alpha(glow, 0.06)}, {shadow}"
    return f"background:{fill};border:1px solid {GLASS_BORDER};box-shadow:{shadow};"


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


_LIGHT_CSS = f"""
<style>
.soft-page-bg {{
    background: {PAGE_GRADIENT};
    background-attachment: fixed;
}}
.q-field__native, .q-field__input, .q-field__label {{
    color: {INK};
}}
.q-field--outlined .q-field__control::before {{
    border-color: {GRID};
}}
.q-menu, .q-card, .q-dialog__inner > div {{
    color: {INK};
}}
</style>
"""


def apply_theme() -> None:
    ui.colors(primary=ACCENT_DEEP, secondary=PEACH, accent=ACCENT, dark=INK)
    ui.add_head_html(_LIGHT_CSS)
    ui.query("body").style(f"background-color:{BG}; color:{INK}")
    ui.query(".nicegui-content").style("padding:0")


def _aside(active: str):
    """Return the quiet navigation drawer used by every dashboard page."""
    drawer = (
        ui.left_drawer()
        .classes("gap-1 p-3")
        .style(f"background:{SURFACE_HI}; border-right:1px solid {GRID}; box-shadow:none")
    )
    with drawer:
        with ui.row().classes("items-center gap-2 px-1 pb-3"):
            ui.icon("bolt").style(f"color:{ACCENT}; font-size:1.3rem")
            ui.label("TRAPPIST").style(f"color:{CREAM}; font-weight:700; letter-spacing:0.02em")
        for path, label, icon in NAV:
            on = path == active
            with (
                ui.link(target=path)
                .classes("no-underline w-full px-3 py-2 rounded-lg flex items-center gap-3")
                .style(
                    f"background:{alpha(ACCENT, 0.16) if on else 'transparent'};"
                    f"color:{ACCENT_DEEP if on else INK_2};" + ("font-weight:600;" if on else "")
                )
            ):
                ui.icon(icon).style("font-size:1.1rem")
                ui.label(label).style("font-size:0.85rem; font-weight:500")
    return drawer


def _nav_bar(drawer) -> None:
    with (
        ui.row()
        .classes("w-full items-center gap-4 px-6 py-4 sticky top-0 z-10")
        .style(
            f"background:rgba(255,255,255,0.96); border-bottom:1px solid {GRID};"
            "box-shadow:0 4px 16px rgba(73,55,45,0.05)"
        )
    ):
        ui.button(icon="menu", on_click=drawer.toggle).props("flat round dense").style(
            f"color:{CREAM}"
        )
        with ui.column().classes("gap-0"):
            ui.label("TRAPPIST x BRAINWAVE").style(
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
    with ui.column().classes("w-full min-h-screen gap-0 soft-page-bg"):
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
    with ui.column().classes("w-full gap-3 p-5 rounded-xl").style(glass()):
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
        .style(
            glass(SURFACE if tone == "default" else _TONES.get(tone, SURFACE), 0.07)
            + "min-width:180px"
        )
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
        .style(f"background:{alpha(colour, 0.10)}; border:1px solid {alpha(colour, 0.25)};")
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
        axis["nameLocation"] = "middle"
        axis["nameGap"] = 36
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
    table.props("flat dense bordered")
    return table
