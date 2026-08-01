"""Economics -- THE argument, plotted.

The claim the whole submission rests on:

    A $0.002 tool call settled on its own, against a $0.001 facilitator fee,
    burns 50% of the revenue. The same fee spread across a 100-call session
    is 0.5%.

That is not an opinion and it is not a projection. It is one division, and this
page draws it as a function so the shape is visible rather than asserted:

    settle every call     fee_load(n) = fee*n / (price*n)  = fee/price     (flat)
    one settlement per N   fee_load(n) = fee   / (price*n)  = (fee/price)/n (1/n)

The flat line is the trap: per-call settlement's fee load does not improve with
volume, because every call brings its own fee. The hyperbola is the product.

Two things keep this page honest:

* Both curves are labelled MODEL. They are computed from the price and fee in the
  controls, with `app.money.fee_load_bps` -- the same integer function the ledger
  uses -- so what you see is the arithmetic, not a fit.
* The dots are labelled LEDGER. They are real settled batches: x is that batch's
  actual call count, y is its actual facilitator fee over its actual gross. If no
  batch has settled, there are no dots and the page says so, rather than
  scattering plausible-looking points onto a chart.

One y-scale, always. Fee load is the only quantity plotted; the x axis is log
because the interesting range spans 1 to 500 calls.
"""

from __future__ import annotations

from nicegui import ui

from app.config import settings
from app.money import fee_load_bps, parse_price
from app.ui import queries as q
from app.ui import theme as t

SYM = settings.x402_asset_symbol
D = settings.x402_asset_decimals

#: Price points an author might plausibly charge per call, in atomic units.
PRICE_PRESETS = ("$0.0005", "$0.001", "$0.002", "$0.005", "$0.01", "$0.05", "$0.10")
#: What one on-chain settlement costs at the facilitator.
FEE_PRESETS = ("$0.0002", "$0.0005", "$0.001", "$0.002", "$0.005")

#: Breakpoints for the table twin -- the values a reader is most likely to quote.
BREAKPOINTS = (1, 2, 5, 10, 25, 50, 100, 250, 500)


def _atomic(prices: tuple[str, ...]) -> dict[int, str]:
    out: dict[int, str] = {}
    for text in prices:
        try:
            out[parse_price(text, D)] = text
        except Exception:  # a preset finer than the asset's decimals -- skip it
            continue
    return out


def render() -> None:
    price_options = _atomic(PRICE_PRESETS)
    fee_options = _atomic(FEE_PRESETS)
    # Whatever this deployment is actually configured for must be selectable.
    fee_options.setdefault(settings.facilitator_fee_atomic, settings.facilitator_fee_price)

    ledger_avg = q.average_captured_atomic()
    if ledger_avg:
        price_options[ledger_avg] = f"ledger average ({t.usd(ledger_avg)} {SYM})"

    default_price = parse_price("$0.002", D)
    if default_price not in price_options:
        default_price = sorted(price_options)[0]

    state = {
        "price": default_price,
        "fee": settings.facilitator_fee_atomic,
        "max_n": max(50, min(500, settings.batch_max_calls)),
        "exclude_demo": False,
    }
    demo = q.demo_summary()

    with t.page_shell(
        "/economics",
        "Economics",
        "Settlement cost as a share of revenue, as a function of how many calls share "
        "one on-chain settlement. This is the reason session batching exists.",
    ):
        _controls(state, price_options, fee_options, demo, lambda: model.refresh())

        @ui.refreshable
        def model() -> None:
            _model(state, demo)

        model()


# --------------------------------------------------------------------------
# Controls. One row, above everything they scope.
# --------------------------------------------------------------------------


def _controls(state: dict, prices: dict[int, str], fees: dict[int, str], demo, refresh) -> None:
    with (
        ui.row()
        .classes("w-full items-center gap-5 p-3 rounded-xl flex-wrap")
        .style(f"background:{t.SURFACE}; border:1px solid {t.GRID}")
    ):
        ui.icon("tune").style(f"color:{t.INK_MUTED}")
        ui.select(
            options=prices,
            value=state["price"],
            label="price per call",
            on_change=lambda e: (state.update(price=int(e.value)), refresh()),
        ).props("dense outlined").style("min-width:16rem")

        ui.select(
            options=fees,
            value=state["fee"],
            label="facilitator fee per settlement",
            on_change=lambda e: (state.update(fee=int(e.value)), refresh()),
        ).props("dense outlined").style("min-width:16rem")

        with ui.column().classes("gap-0").style("min-width:16rem"):
            ui.label("calls per batch (x-axis maximum)").style(
                f"color:{t.INK_MUTED}; font-size:0.7rem"
            )
            ui.slider(
                min=10,
                max=500,
                step=10,
                value=state["max_n"],
                on_change=lambda e: (state.update(max_n=int(e.value)), refresh()),
            ).props("label-always").style(f"color:{t.ACCENT_DEEP}")

        if demo.present:
            ui.switch(
                "exclude demo data",
                value=state["exclude_demo"],
                on_change=lambda e: (state.update(exclude_demo=bool(e.value)), refresh()),
            ).props("dense").style(f"color:{t.WARN_INK}").tooltip(
                "Scopes the ledger overlay only. The model curves are computed from "
                "the price and fee above and are unaffected."
            )

        ui.space()
        ui.label(
            f"configured: batch cap {settings.batch_max_calls} calls / "
            f"{settings.batch_window_seconds}s window"
        ).style(f"color:{t.INK_MUTED}; font-size:0.75rem")


# --------------------------------------------------------------------------


def _model(state: dict, demo) -> None:
    price, fee, max_n = int(state["price"]), int(state["fee"]), int(state["max_n"])
    exclude = bool(state["exclude_demo"])

    per_call_bps = fee_load_bps(fee, price)  # flat: independent of n
    cap = max(1, min(max_n, settings.batch_max_calls))
    at_cap_bps = fee_load_bps(fee, price * cap)
    at_hundred_bps = fee_load_bps(fee, price * 100)

    # Seeded batches would otherwise be plotted as LEDGER observations -- real
    # dots on a chart whose whole job is to be checkable. Say so first.
    if demo.present:
        t.demo_banner(demo, excluded=exclude)

    _headline(price, fee, per_call_bps, at_cap_bps, at_hundred_bps, cap)
    points = q.batch_points(exclude_demo=exclude)
    _chart(price, fee, max_n, points)
    _breakpoint_table(price, fee)
    _ledger_card(points)
    _derivation(price, fee)


def _headline(
    price: int, fee: int, per_call_bps: int, at_cap_bps: int, at_hundred_bps: int, cap: int
) -> None:
    with ui.row().classes("w-full gap-5 items-stretch flex-wrap"):
        with (
            ui.column()
            .classes("p-5 rounded-xl gap-3")
            .style(f"background:{t.SURFACE}; border:1px solid {t.GRID}; min-width:min(100%,360px)")
        ):
            t.hero(
                "fee load, settling every call",
                t.pct_bps(per_call_bps),
                f"{t.usd(fee, symbol=True)} of fee on every {t.usd(price, symbol=True)} call",
            )
            ui.separator().style(f"background:{t.GRID}")
            with ui.row().classes("items-baseline gap-3"):
                ui.label(t.pct_bps(at_hundred_bps)).style(
                    f"color:{t.SERIES[0]}; font-size:1.9rem; font-weight:650;letter-spacing:-0.02em"
                )
                ui.label("with 100 calls sharing one settlement").style(
                    f"color:{t.INK_2}; font-size:0.85rem"
                )

        with ui.column().classes("flex-1 gap-4").style("min-width:min(100%,420px)"):
            with t.stat_row():
                t.stat(
                    "at the configured cap",
                    t.pct_bps(at_cap_bps),
                    f"{cap} calls per settlement",
                    tone="ok",
                    icon="tune",
                )
                t.stat(
                    "revenue kept, per call",
                    t.pct_bps(max(0, 10_000 - per_call_bps)),
                    "settling every call",
                    tone="bad" if per_call_bps >= 1_000 else "default",
                    icon="trending_down",
                )
                t.stat(
                    "revenue kept, batched",
                    t.pct_bps(max(0, 10_000 - at_cap_bps)),
                    f"at {cap} calls per settlement",
                    tone="ok",
                    icon="trending_up",
                )
            t.note(
                "Per-call settlement does not get cheaper with volume. Every call brings its "
                "own fee, so its fee load is a horizontal line -- the flat grey series below. "
                "Batching divides ONE fee across N calls, which is the 1/n curve. That is the "
                "entire product, and it is one division.",
                tone="default",
                icon="calculate",
            )


def _sample_points(max_n: int) -> list[int]:
    """Dense at the left where the curve moves, sparse at the right where it does not."""
    xs = set(range(1, min(21, max_n + 1)))
    n = 25
    while n <= max_n:
        xs.add(n)
        n = int(n * 1.25) + 1
    xs.add(max_n)
    return sorted(x for x in xs if 1 <= x <= max_n)


def _chart(price: int, fee: int, max_n: int, points: list[q.BatchPoint]) -> None:
    xs = _sample_points(max_n)
    per_call = [[x, round(fee_load_bps(fee, price) / 100, 2)] for x in xs]
    batched = [[x, round(fee_load_bps(fee, price * x) / 100, 2)] for x in xs]

    series = [
        # The emphasis pattern: the series we are arguing AGAINST is greyed and
        # directly labelled, so it never depends on hue to be identified.
        t.line_series(
            "MODEL - settle every call",
            per_call,
            t.DE_EMPHASIS,
            end_label="settle every call",
        ),
        t.line_series(
            "MODEL - one settlement per batch",
            batched,
            t.SERIES[0],
            area=True,
            end_label="batched",
        ),
    ]

    # A vertical rule at the operating point this deployment is configured for.
    cap = min(max(settings.batch_max_calls, 1), max_n)
    series[1] = {
        **series[1],
        "markLine": {
            "symbol": "none",
            "silent": True,
            "lineStyle": {"color": t.INK_MUTED, "width": 1, "type": "solid"},
            "label": {
                "formatter": f"configured cap: {settings.batch_max_calls}",
                "color": t.INK_MUTED,
                "fontSize": 10,
                "position": "insideEndTop",
            },
            "data": [{"xAxis": cap}],
        },
    }

    plotted = [p for p in points if p.gross_atomic > 0 and p.call_count >= 1]
    real = [p for p in plotted if not p.is_demo]
    seeded = [p for p in plotted if p.is_demo]

    def _coords(group: list[q.BatchPoint]) -> list[list[float]]:
        return [[p.call_count, round(p.fee_load_bps / 100, 2)] for p in group]

    if real:
        series.append(t.scatter_series("LEDGER - settled batches", _coords(real), t.SERIES[1]))
    if seeded:
        # Same hue -- both are "an observation" -- but a hollow diamond, its own
        # legend entry, and the word SEEDED. A fabricated dot must never be able
        # to pass for a real settlement on the chart that makes the argument.
        series.append(
            t.scatter_series(
                "SEEDED - demo batches, not real settlements",
                _coords(seeded),
                t.SERIES[1],
                symbol="diamond",
                hollow=True,
                size=13,
            )
        )

    if real and seeded:
        overlay = (
            f"{t.compact_int(len(real))} real settled batch(es) are overlaid as filled dots, "
            f"and {t.compact_int(len(seeded))} SEEDED demo batch(es) as hollow diamonds."
        )
    elif real:
        overlay = f"{t.compact_int(len(real))} real settled batch(es) are overlaid as dots."
    elif seeded:
        overlay = (
            f"No real batch has settled. The {t.compact_int(len(seeded))} hollow diamonds are "
            "SEEDED demo rows, not settlements -- nothing on this chart is a real observation."
        )
    else:
        overlay = (
            "No batch has settled on-chain yet, so there are no ledger observations "
            "to overlay -- only the model."
        )

    with t.card(
        "Fee load as a function of calls per batch",
        "Lower is better: it is the share of revenue the facilitator takes. " + overlay,
    ):
        ui.echart(
            t.chart_options(
                series=series,
                x_axis=t.axis_log(name="calls sharing one settlement"),
                y_axis=t.axis_value(name="fee load", formatter="{value}%"),
                legend=True,
                tooltip_trigger="axis",
                grid={"left": 8, "right": 96, "top": 34, "bottom": 40, "containLabel": True},
            )
        ).classes("w-full").style("height:400px")

        with ui.row().classes("w-full items-center gap-4 flex-wrap"):
            _key(t.DE_EMPHASIS, "MODEL", "computed from the controls above")
            if real:
                _key(t.SERIES[1], "LEDGER", "one filled dot = one settled batch, as recorded")
            if seeded:
                _key(
                    t.WARN_INK,
                    "SEEDED",
                    "hollow diamonds are demo rows -- no USDC moved, hashes are synthetic",
                )


def _key(colour: str, label: str, detail: str) -> None:
    with ui.row().classes("items-center gap-2 no-wrap"):
        ui.label("").style(
            f"background:{colour}; width:12px; height:12px; border-radius:3px; display:block"
        )
        ui.label(label).style(
            f"color:{t.INK}; font-size:0.7rem; font-weight:700; letter-spacing:0.08em"
        )
        ui.label(detail).style(f"color:{t.INK_MUTED}; font-size:0.72rem")


def _breakpoint_table(price: int, fee: int) -> None:
    with t.card(
        "The same numbers, as numbers",
        "The table twin of the chart above -- every value reachable without colour or hover.",
    ):
        flat = fee_load_bps(fee, price)
        t.data_table(
            columns=[
                {"name": "n", "label": "Calls per settlement", "field": "n", "align": "right"},
                {"name": "gross", "label": f"Gross ({SYM})", "field": "gross", "align": "right"},
                {
                    "name": "percall",
                    "label": "Fee load, per-call",
                    "field": "percall",
                    "align": "right",
                },
                {
                    "name": "batched",
                    "label": "Fee load, batched",
                    "field": "batched",
                    "align": "right",
                },
                {
                    "name": "saved",
                    "label": f"Fees avoided ({SYM})",
                    "field": "saved",
                    "align": "right",
                },
            ],
            rows=[
                {
                    "id": n,
                    "n": n,
                    "gross": t.usd(price * n),
                    "percall": t.pct_bps(flat),
                    "batched": t.pct_bps(fee_load_bps(fee, price * n)),
                    "saved": t.usd(fee * n - fee),
                }
                for n in BREAKPOINTS
            ],
            pagination=0,
        )
        ui.label(
            f"Computed with app.money.fee_load_bps at {t.usd(price, symbol=True)} per call "
            f"and {t.usd(fee, symbol=True)} per settlement -- integer basis points, the same "
            "function the ledger uses. No floats, no rounding into the argument."
        ).style(f"color:{t.INK_MUTED}; font-size:0.72rem")


def _ledger_card(points: list[q.BatchPoint]) -> None:
    with t.card(
        "Settled batches, as recorded",
        "The observations behind the dots. Only SETTLED batches with a positive gross "
        "appear: an open batch has not been charged a facilitator fee, so its fee load "
        "is not yet a fact.",
    ):
        if not points:
            t.empty_state(
                "No batch has settled on-chain",
                "The curves above are the model of the configured fee. Nothing is plotted "
                "as a realised figure because nothing has been realised yet -- close a "
                "batch and its true fee load appears here and on the chart.",
                icon="hourglass_empty",
            )
            return

        # The tiles report REAL settlements only. A seeded batch has a synthetic
        # transaction hash and paid no facilitator anything, so folding it into
        # "realised fee load" would be inventing the one number this page exists
        # to prove.
        real = [p for p in points if not p.is_demo]
        seeded = [p for p in points if p.is_demo]

        if not real:
            t.note(
                f"All {t.compact_int(len(seeded))} batch(es) below are SEEDED demo rows. "
                "There is no realised fee load to report, so none is shown -- the figures "
                "that would go here are left empty rather than filled with demo arithmetic.",
                tone="warn",
                icon="science",
            )
        else:
            realised_fee = sum(p.facilitator_fee_atomic for p in real)
            realised_gross = sum(p.gross_atomic for p in real)
            realised_calls = sum(p.call_count for p in real)
            counterfactual = settings.facilitator_fee_atomic * realised_calls

            with t.stat_row():
                t.stat(
                    "settled batches",
                    t.compact_int(len(real)),
                    f"covering {t.compact_int(realised_calls)} calls"
                    + (f", {t.compact_int(len(seeded))} seeded excluded" if seeded else ""),
                    icon="layers",
                )
                t.stat(
                    "realised fee load",
                    t.pct_bps((realised_fee * 10_000) // realised_gross)
                    if realised_gross
                    else "no data",
                    f"{t.usd(realised_fee, symbol=True)} on "
                    f"{t.usd(realised_gross, symbol=True)} gross",
                    tone="ok",
                    icon="percent",
                )
                t.stat(
                    "had each call settled alone",
                    t.pct_bps((counterfactual * 10_000) // realised_gross)
                    if realised_gross
                    else "no data",
                    f"{t.usd(counterfactual, symbol=True)} of fees instead",
                    tone="bad",
                    icon="call_split",
                )
                t.stat(
                    "fees avoided",
                    t.usd(max(0, counterfactual - realised_fee), symbol=True),
                    "kept by batching, on real settlements only",
                    tone="ok",
                    icon="savings",
                )

        t.data_table(
            columns=[
                {"name": "batch", "label": "Batch", "field": "batch", "align": "left"},
                {
                    "name": "calls",
                    "label": "Calls",
                    "field": "calls",
                    "align": "right",
                    "sortable": True,
                },
                {"name": "gross", "label": f"Gross ({SYM})", "field": "gross", "align": "right"},
                {
                    "name": "fee",
                    "label": f"Facilitator fee ({SYM})",
                    "field": "fee",
                    "align": "right",
                },
                {
                    "name": "load",
                    "label": "Fee load",
                    "field": "load",
                    "align": "right",
                    "sortable": True,
                },
                {"name": "settled", "label": "Settled (UTC)", "field": "settled", "align": "left"},
            ],
            rows=[
                {
                    "id": p.batch_public_id,
                    "batch": ("DEMO  " if p.is_demo else "") + t.shorten(p.batch_public_id, 14, 4),
                    "calls": p.call_count,
                    "gross": t.usd(p.gross_atomic, p.decimals),
                    "fee": t.usd(p.facilitator_fee_atomic, p.decimals),
                    "load": t.pct_bps(p.fee_load_bps),
                    "settled": p.settled_at.strftime("%Y-%m-%d %H:%M") if p.settled_at else "--",
                }
                for p in points
            ],
            pagination={"rowsPerPage": 12, "sortBy": "calls", "descending": True},
        )


def _derivation(price: int, fee: int) -> None:
    with t.card("Why this is a division and not a claim"):
        ui.markdown(
            f"""
Settlement is charged **per on-chain event**, not per call. So for `n` calls at
`{t.usd(price)} {SYM}` each, with a `{t.usd(fee)} {SYM}` facilitator fee:

| | fees paid | gross revenue | fee load |
|---|---|---|---|
| settle every call | `fee x n` | `price x n` | `fee / price` -- **constant in n** |
| one settlement per batch | `fee` | `price x n` | `fee / (price x n)` -- **falls as 1/n** |

The left column is why micro-priced tools have not worked. At `{t.usd(price)} {SYM}`
a call, per-call settlement costs **{t.pct_bps(fee_load_bps(fee, price))}** of
revenue and stays there no matter how much volume arrives. Batching moves the fee
into the denominator.

`Session.authorized_atomic` is a **ceiling**, not a running sum. Under x402's
`batch-settlement` scheme the accumulator is a payment channel with a monotonic
cumulative voucher -- deposit, then per-request vouchers raising
`maxClaimableAmount`, then one `claim` and one `settle` on-chain. That is why
`Batch` carries two transaction hashes and why a batch is one settlement rather
than n of them.
            """,
            # markdown2 does not do pipe tables without this extra; without it the
            # table below renders as literal pipes. Verified, not assumed.
            extras=["tables"],
        ).classes("w-full").style(f"color:{t.INK}; font-size:0.85rem")


__all__ = ["render"]
