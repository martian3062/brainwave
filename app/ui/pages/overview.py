"""Overview -- revenue over time, call volume, realised fee load.

Everything here is read from the ledger. There is no seed, no sample series and
no placeholder figure anywhere on this page: with an empty `call` table it
renders empty states that say what is missing and what would fill it.

Chart choices, deliberately:

* Revenue over time is a STACKED COLUMN, not a line. Daily revenue is a discrete
  quantity per bucket, and stacking author-net on platform-take shows the split
  and the total in one mark. Two series, so a legend is present.
* Call volume is its OWN chart, single series, no legend. It is tempting to put
  volume on a second y-axis of the revenue chart; a dual-axis plot invents a
  correlation out of an arbitrary scale alignment, so it is never done here.
* Call outcomes are ORDINAL (402 issued -> verified -> executed -> captured ->
  settled), so they take the one-hue ramp: the reader sees the order in the
  colour. Declines are not a funnel stage; they are listed by reason.
"""

from __future__ import annotations

from nicegui import ui

from app.config import settings
from app.ui import queries as q
from app.ui import theme as t

WINDOWS = {7: "last 7 days", 14: "last 14 days", 30: "last 30 days", 90: "last 90 days"}


def render() -> None:
    state = {"days": 14, "exclude_demo": False}
    demo = q.demo_summary()

    with t.page_shell(
        "/",
        "Overview",
        "Every paid MCP tool call, metered and settled with x402 on Base. "
        "Figures below are computed from the ledger, never estimated.",
    ):
        _filter_row(state, demo, lambda: body.refresh())

        @ui.refreshable
        def body() -> None:
            _body(state, demo)

        body()


# --------------------------------------------------------------------------
# Filters. One row, above everything it scopes -- never inside a chart card.
# --------------------------------------------------------------------------


def _filter_row(state: dict, demo, refresh) -> None:
    with (
        ui.row()
        .classes("w-full items-center gap-4 p-3 rounded-xl flex-wrap")
        .style(f"background:{t.SURFACE}; border:1px solid {t.GRID}")
    ):
        ui.icon("filter_alt").style(f"color:{t.INK_MUTED}")
        window = (
            ui.select(
                options=WINDOWS,
                value=state["days"],
                label="window",
                on_change=lambda e: (state.update(days=int(e.value)), refresh()),
            )
            .props("dense outlined")
            .style("min-width:11rem")
        )
        window.tooltip("Scopes both time-series charts below.")

        if demo.present:
            ui.switch(
                "exclude demo data",
                value=state["exclude_demo"],
                on_change=lambda e: (state.update(exclude_demo=bool(e.value)), refresh()),
            ).props("dense").style(f"color:{t.WARN_INK}")
        ui.space()
        ui.label(
            f"settlement: {'batched' if settings.batching_enabled else 'per call'} - "
            f"window {settings.batch_window_seconds}s / {settings.batch_max_calls} calls"
        ).style(f"color:{t.INK_MUTED}; font-size:0.75rem")


# --------------------------------------------------------------------------


def _body(state: dict, demo) -> None:
    exclude = bool(state["exclude_demo"])
    totals = q.totals(exclude_demo=exclude)

    if demo.present:
        t.demo_banner(demo, excluded=exclude)

    if not totals.has_any_calls:
        _cold_start(exclude, demo)
        return

    _headline(totals, demo.present)
    _revenue_chart(state, exclude)
    _volume_chart(state, exclude)
    with ui.row().classes("w-full gap-5 items-start flex-wrap"):
        with ui.column().classes("flex-1 gap-5").style("min-width:min(100%,420px)"):
            _funnel_card(exclude)
        with ui.column().classes("flex-1 gap-5").style("min-width:min(100%,420px)"):
            _sessions_card(exclude)


def _cold_start(exclude: bool, demo) -> None:
    if exclude and demo.present:
        t.empty_state(
            "No non-demo calls recorded",
            "Every call in this ledger is seeded demo data. Turn off "
            "'exclude demo data' to see it, or point an agent at the MCP endpoint "
            "to record a real one.",
            icon="science",
        )
        return
    t.empty_state(
        "The ledger is empty",
        "No tool call has been metered yet, so there is nothing to report -- this "
        f"page will not invent one. Point an MCP client at {settings.mcp_public_url} "
        "and pay for a tool call; the row appears here immediately.",
        icon="database",
    )


# --------------------------------------------------------------------------
# Headline figures
# --------------------------------------------------------------------------


def _headline(totals: q.Totals, demo_present: bool) -> None:
    # ALWAYS real-only, regardless of the filter. "Realised" means a facilitator
    # actually charged us; a seeded batch has a synthetic hash and paid nothing.
    # Letting the toggle move this number would make the page's most quotable
    # figure depend on a switch, which is how a demo becomes a claim.
    fee = q.realised_fee_load(exclude_demo=True)

    with ui.row().classes("w-full gap-5 items-stretch flex-wrap"):
        with (
            ui.column()
            .classes("p-5 rounded-xl gap-4")
            .style(f"background:{t.SURFACE}; border:1px solid {t.GRID}; min-width:min(100%,340px)")
        ):
            # Exactly one hero figure per view.
            t.hero(
                "author net revenue",
                t.usd(totals.author_net_atomic, symbol=True),
                f"gross {t.usd(totals.captured_atomic, symbol=True)} captured, "
                f"platform take {t.usd(totals.platform_fee_atomic, symbol=True)}",
            )
            t.kv("billable calls", t.compact_int(totals.calls_billable))
            t.kv(
                "authorized",
                t.usd(totals.authorized_atomic, symbol=True)
                + (
                    f"  ({t.pct_bps((totals.captured_atomic * 10_000) // totals.authorized_atomic)}"
                    " captured)"
                    if totals.authorized_atomic
                    else ""
                ),
            )

        with ui.column().classes("flex-1 gap-4").style("min-width:min(100%,420px)"):
            with t.stat_row():
                t.stat(
                    "payment sessions",
                    t.compact_int(totals.sessions_total),
                    f"{t.compact_int(totals.sessions_open)} open",
                    icon="timeline",
                )
                t.stat(
                    "settled batches",
                    t.compact_int(totals.batches_settled),
                    f"of {t.compact_int(totals.batches_total)} opened",
                    icon="layers",
                )
                t.stat(
                    "declined",
                    t.compact_int(totals.calls_declined),
                    "guardian refused before signature"
                    if totals.calls_declined
                    else "none refused",
                    tone="warn" if totals.calls_declined else "default",
                    icon="block",
                )
            with t.stat_row():
                if fee is None:
                    t.stat(
                        "realised fee load",
                        "no data",
                        "no real batch has settled on-chain yet"
                        + (" (seeded batches do not count)" if demo_present else ""),
                        tone="warn",
                        icon="pending",
                    )
                    t.stat(
                        "catalogue",
                        t.compact_int(totals.tools_enabled),
                        f"{t.compact_int(totals.authors)} author(s)",
                        icon="handyman",
                    )
                else:
                    t.stat(
                        "realised fee load",
                        t.pct_bps(fee.realised_bps),
                        f"{t.usd(fee.facilitator_fee_atomic, symbol=True)} of facilitator fees "
                        f"on {t.usd(fee.gross_atomic, symbol=True)} gross"
                        + (" - real settlements only" if demo_present else ""),
                        tone="ok" if fee.realised_bps < fee.per_call_bps else "warn",
                        icon="percent",
                    )
                    t.stat(
                        "same calls, settled one by one",
                        t.pct_bps(fee.per_call_bps),
                        f"would have cost {t.usd(fee.per_call_fee_atomic, symbol=True)} - "
                        f"batching kept {t.usd(fee.saved_atomic, symbol=True)}",
                        tone="bad",
                        icon="call_split",
                    )

    if fee is not None:
        t.note(
            f"Reconciled from {t.compact_int(fee.batches)} settled batch(es) covering "
            f"{t.compact_int(fee.calls)} call(s). The counterfactual multiplies the configured "
            f"{t.usd(settings.facilitator_fee_atomic, symbol=True)} per-settlement fee by those "
            "same call counts -- it is arithmetic on the ledger, not a projection.",
            tone="ok",
            icon="fact_check",
        )


# --------------------------------------------------------------------------
# Time series
# --------------------------------------------------------------------------


def _revenue_chart(state: dict, exclude: bool) -> None:
    buckets = q.daily_buckets(state["days"], exclude_demo=exclude)
    with t.card(
        "Revenue over time",
        "Captured revenue per day, split into what the tool author keeps and the "
        f"platform take ({settings.platform_take_bps / 100:.1f}% by default).",
    ):
        if not buckets or all(b.captured_atomic == 0 for b in buckets):
            t.empty_state(
                "No captured revenue in this window",
                f"There are no billable calls in the {WINDOWS[state['days']]}. "
                "Widen the window, or record a paid call.",
                icon="show_chart",
            )
            return

        labels = [b.day.strftime("%d %b") for b in buckets]
        net = [t.chart_value(b.author_net_atomic) for b in buckets]
        take = [t.chart_value(b.platform_fee_atomic) for b in buckets]

        ui.echart(
            t.chart_options(
                series=[
                    t.bar_series("author net", net, t.SERIES[0], stack="rev"),
                    t.bar_series("platform take", take, t.SERIES[2], stack="rev"),
                ],
                x_axis=t.axis_category(labels),
                y_axis=t.axis_value(name=settings.x402_asset_symbol),
                legend=True,
            )
        ).classes("w-full").style("height:320px")

        with t.table_view("Table view - revenue by day"):
            t.data_table(
                columns=[
                    {
                        "name": "day",
                        "label": "Day",
                        "field": "day",
                        "align": "left",
                        "sortable": True,
                    },
                    {
                        "name": "calls",
                        "label": "Billable calls",
                        "field": "calls",
                        "align": "right",
                        "sortable": True,
                    },
                    {
                        "name": "net",
                        "label": f"Author net ({settings.x402_asset_symbol})",
                        "field": "net",
                        "align": "right",
                    },
                    {
                        "name": "take",
                        "label": f"Platform take ({settings.x402_asset_symbol})",
                        "field": "take",
                        "align": "right",
                    },
                    {
                        "name": "gross",
                        "label": f"Gross ({settings.x402_asset_symbol})",
                        "field": "gross",
                        "align": "right",
                    },
                ],
                rows=[
                    {
                        "id": b.day.isoformat(),
                        "day": b.day.isoformat(),
                        "calls": b.calls,
                        "net": t.usd_exact(b.author_net_atomic, symbol=False),
                        "take": t.usd_exact(b.platform_fee_atomic, symbol=False),
                        "gross": t.usd_exact(b.captured_atomic, symbol=False),
                    }
                    for b in reversed(buckets)
                ],
                pagination=10,
            )


def _volume_chart(state: dict, exclude: bool) -> None:
    buckets = q.daily_buckets(state["days"], exclude_demo=exclude)
    with t.card(
        "Call volume",
        "Billable calls per day. Kept as its own chart: overlaying a count on the "
        "revenue chart would need a second y-scale, and the alignment of two "
        "scales is arbitrary -- it manufactures a correlation the data does not have.",
    ):
        if not buckets:
            t.empty_state(
                "No calls in this window",
                f"Nothing was metered in the {WINDOWS[state['days']]}.",
                icon="bar_chart",
            )
            return

        labels = [b.day.strftime("%d %b") for b in buckets]
        calls = [b.calls for b in buckets]
        declined = [b.declined for b in buckets]

        # One series unless there is something to compare against; a legend for
        # a single series just restates the card title.
        series = [t.bar_series("billable calls", calls, t.SERIES[1])]
        if any(declined):
            series.append(t.bar_series("declined", declined, t.SERIES[2]))

        ui.echart(
            t.chart_options(
                series=series,
                x_axis=t.axis_category(labels),
                y_axis=t.axis_value(name="calls"),
                legend=any(declined),
            )
        ).classes("w-full").style("height:260px")

        with t.table_view("Table view - volume by day"):
            t.data_table(
                columns=[
                    {"name": "day", "label": "Day", "field": "day", "align": "left"},
                    {"name": "calls", "label": "Billable", "field": "calls", "align": "right"},
                    {
                        "name": "declined",
                        "label": "Declined",
                        "field": "declined",
                        "align": "right",
                    },
                ],
                rows=[
                    {
                        "id": b.day.isoformat(),
                        "day": b.day.isoformat(),
                        "calls": b.calls,
                        "declined": b.declined,
                    }
                    for b in reversed(buckets)
                ],
                pagination=10,
            )


# --------------------------------------------------------------------------
# Funnel + declines
# --------------------------------------------------------------------------


def _funnel_card(exclude: bool) -> None:
    stages, reasons = q.funnel(exclude_demo=exclude)
    with t.card(
        "Call outcomes",
        "Where calls stop. A row exists from the moment a 402 is issued, so unpaid "
        "probes and refusals are visible, not just the wins.",
    ):
        if not stages or all(s.count == 0 for s in stages):
            t.empty_state(
                "No calls recorded", "Nothing has reached the paywall yet.", icon="filter_list"
            )
            return

        # ORDINAL: position in the protocol sequence, so one hue, monotone
        # lightness. The reader sees the order in the colour.
        data = [
            {
                "value": s.count,
                "itemStyle": {"color": t.ORDINAL[i % len(t.ORDINAL)], "borderRadius": [0, 4, 4, 0]},
            }
            for i, s in enumerate(stages)
        ]
        ui.echart(
            t.chart_options(
                series=[t.bar_series("calls", data, t.ORDINAL[2], horizontal=True, label="{c}")],
                x_axis=t.axis_value(name="calls"),
                y_axis={
                    **t.axis_category([s.label for s in stages]),
                    "inverse": True,
                    "boundaryGap": True,
                },
                tooltip_trigger="item",
                tooltip_formatter="{b}: {c} call(s)",
                grid={"left": 8, "right": 48, "top": 8, "bottom": 8, "containLabel": True},
            )
        ).classes("w-full").style("height:240px")

        if reasons:
            ui.separator().style(f"background:{t.GRID}")
            t.eyebrow("declined, by reason")
            for r in reasons:
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    ui.icon("block").style(f"color:{t.BAD_INK}; font-size:0.9rem")
                    ui.label(r.reason).style(
                        f"color:{t.INK}; font-size:0.82rem; font-family:monospace"
                    )
                    ui.space()
                    ui.label(t.compact_int(r.count)).style(
                        f"color:{t.INK_2}; font-size:0.82rem; font-variant-numeric:tabular-nums"
                    )
            ui.label(
                "Declines are buyer-side Guardian refusals: no authorization was ever "
                "signed, so nothing could be settled."
            ).style(f"color:{t.INK_MUTED}; font-size:0.72rem")


def _sessions_card(exclude: bool) -> None:
    rows = q.session_rows(exclude_demo=exclude, limit=50)
    with t.card(
        "Recent payment sessions",
        "One agent run each: N authorizations accumulating toward one settlement.",
    ):
        if not rows:
            t.empty_state(
                "No sessions", "No agent has opened a payment session yet.", icon="timeline"
            )
            return

        t.data_table(
            columns=[
                {
                    "name": "session",
                    "label": "Session",
                    "field": "session",
                    "align": "left",
                    "sortable": True,
                },
                {"name": "agent", "label": "Agent", "field": "agent", "align": "left"},
                {
                    "name": "status",
                    "label": "Status",
                    "field": "status",
                    "align": "left",
                    "sortable": True,
                },
                {
                    "name": "calls",
                    "label": "Calls",
                    "field": "calls",
                    "align": "right",
                    "sortable": True,
                },
                {
                    "name": "captured",
                    "label": f"Captured ({settings.x402_asset_symbol})",
                    "field": "captured",
                    "align": "right",
                },
                {
                    "name": "settled",
                    "label": f"Settled ({settings.x402_asset_symbol})",
                    "field": "settled",
                    "align": "right",
                },
            ],
            rows=[
                {
                    "id": r.session_public_id,
                    # The DEMO marker rides in the visible cell text, so a seeded
                    # row cannot be mistaken for a real one even in a screenshot.
                    "session": ("DEMO  " if r.is_demo else "")
                    + t.shorten(r.session_public_id, 16, 4),
                    "agent": r.agent_label or t.shorten(r.payer),
                    "status": r.status,
                    "calls": r.calls,
                    "captured": t.usd(r.captured_atomic, r.decimals),
                    "settled": t.usd(r.settled_atomic, r.decimals),
                }
                for r in rows
            ],
            pagination=8,
        )
        ui.label(
            "Captured leads settled until the batch closes; that gap is the float "
            "batching earns its keep on."
        ).style(f"color:{t.INK_MUTED}; font-size:0.72rem")


# --------------------------------------------------------------------------

__all__ = ["render"]
