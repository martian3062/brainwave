"""Tools -- per-tool revenue, volume, and captured versus authorized.

The `upto` scheme is the reason this page has two charts instead of one. Under
`exact` a tool's price is its revenue and there is nothing to show. Under `upto`
the agent authorizes a CEILING and the gateway captures only what the tool
actually consumed, so "authorized" and "captured" are different numbers and the
gap between them is the honesty of the whole system. A ledger CHECK constraint
(`ck_call_capture_le_authorized`) makes the gap impossible to invert; this page
makes it visible.

Colour: tool names are NOMINAL -- reordering them changes nothing -- so the
revenue bars are all ONE colour (categorical slot 1). Shading each bar darker
where bigger would spend the identity channel re-encoding what bar length
already shows.
"""

from __future__ import annotations

from nicegui import ui

from app.config import settings
from app.ui import queries as q
from app.ui import theme as t

SYM = settings.x402_asset_symbol


def render() -> None:
    state = {"exclude_demo": False, "enabled_only": False}
    demo = q.demo_summary()

    with t.page_shell(
        "/tools",
        "Tools",
        "What each priced MCP tool in the catalogue has earned. Aggregated from the "
        "`call` ledger, not from the denormalised counters on `tool`.",
    ):
        _filter_row(state, demo, lambda: body.refresh())

        @ui.refreshable
        def body() -> None:
            _body(state, demo)

        body()


def _filter_row(state: dict, demo, refresh) -> None:
    with (
        ui.row()
        .classes("w-full items-center gap-5 p-3 rounded-xl flex-wrap")
        .style(f"background:{t.SURFACE}; border:1px solid {t.GRID}")
    ):
        ui.icon("filter_alt").style(f"color:{t.INK_MUTED}")
        ui.switch(
            "enabled tools only",
            value=state["enabled_only"],
            on_change=lambda e: (state.update(enabled_only=bool(e.value)), refresh()),
        ).props("dense")
        if demo.present:
            ui.switch(
                "exclude demo data",
                value=state["exclude_demo"],
                on_change=lambda e: (state.update(exclude_demo=bool(e.value)), refresh()),
            ).props("dense").style(f"color:{t.WARN_INK}")
        ui.space()
        ui.label(f"platform take {settings.platform_take_bps / 100:.1f}% by default").style(
            f"color:{t.INK_MUTED}; font-size:0.75rem"
        )


def _body(state: dict, demo) -> None:
    exclude = bool(state["exclude_demo"])
    rows = q.tool_rows(exclude_demo=exclude)
    if state["enabled_only"]:
        rows = [r for r in rows if r.enabled]

    if demo.present:
        t.demo_banner(demo, excluded=exclude)

    if not rows:
        t.empty_state(
            "No tools in the catalogue",
            "Nothing is registered in the `tool` table yet. `app.catalogue.register_tools()` "
            "is the seam that prices MCP tools and writes those rows; until it runs, the "
            "gateway serves free tools only and this page has nothing to report.",
            icon="handyman",
        )
        return

    _summary(rows)
    _drift_warning(rows)

    earning = [r for r in rows if r.captured_atomic > 0]
    if earning:
        _revenue_chart(earning)
        _upto_chart(rows)
    else:
        with t.card("Revenue by tool", "No tool has captured anything yet."):
            t.empty_state(
                "Catalogue priced, nothing sold",
                f"{t.compact_int(len(rows))} tool(s) are registered and priced, but no call "
                "has been captured against any of them. The table below shows the prices "
                "that are live.",
                icon="sell",
            )

    _table(rows)


def _summary(rows: list[q.ToolRow]) -> None:
    gross = sum(r.captured_atomic for r in rows)
    net = sum(r.author_net_atomic for r in rows)
    authorized = sum(r.authorized_atomic for r in rows)
    calls = sum(r.calls for r in rows)
    unused = sum(r.unused_atomic for r in rows)

    with t.stat_row():
        t.stat(
            "priced tools",
            t.compact_int(len(rows)),
            f"{t.compact_int(sum(1 for r in rows if r.enabled))} enabled",
            icon="sell",
        )
        t.stat(
            "calls captured",
            t.compact_int(calls),
            f"{t.compact_int(sum(r.declined for r in rows))} declined",
            icon="bolt",
        )
        t.stat(
            "gross captured",
            t.usd(gross, symbol=True),
            f"author net {t.usd(net, symbol=True)}",
            icon="payments",
        )
        if authorized:
            t.stat(
                "authorization used",
                t.pct_bps((gross * 10_000) // authorized),
                f"{t.usd(unused, symbol=True)} authorized but never charged",
                tone="ok",
                icon="verified",
            )
        else:
            t.stat(
                "authorization used",
                "no data",
                "nothing has been authorized yet",
                tone="warn",
                icon="pending",
            )


def _drift_warning(rows: list[q.ToolRow]) -> None:
    """`Tool.total_calls` / `total_captured_atomic` are a cache. The `call` rows
    are the ledger. If they ever disagree, say so rather than picking one."""
    drifted = [
        r
        for r in rows
        if (r.counter_calls or r.counter_captured_atomic)
        and (r.counter_calls != r.calls or r.counter_captured_atomic != r.captured_atomic)
    ]
    if drifted:
        t.note(
            "Counter drift on "
            + ", ".join(r.name for r in drifted[:4])
            + (f" and {len(drifted) - 4} more" if len(drifted) > 4 else "")
            + ". The denormalised totals on `tool` disagree with the `call` rows. Every "
            "figure on this page comes from `call`, which is the ledger of record.",
            tone="warn",
            icon="warning",
        )


def _revenue_chart(rows: list[q.ToolRow]) -> None:
    top = rows[:12]
    with t.card(
        "Revenue by tool",
        "Captured revenue, highest first. Nominal categories, so one colour: bar "
        "length already encodes the value."
        + (f" Showing the top 12 of {len(rows)}." if len(rows) > 12 else ""),
    ):
        data = [t.chart_value(r.captured_atomic, r.decimals) for r in reversed(top)]
        labels = [("DEMO " if r.tainted else "") + r.name for r in reversed(top)]
        ui.echart(
            t.chart_options(
                series=[t.bar_series("captured", data, t.SERIES[0], horizontal=True)],
                x_axis=t.axis_value(name=SYM),
                y_axis={**t.axis_category(labels), "boundaryGap": True},
                tooltip_trigger="item",
                tooltip_formatter="{b}: {c} " + SYM,
                grid={"left": 8, "right": 32, "top": 8, "bottom": 8, "containLabel": True},
            )
        ).classes("w-full").style(f"height:{max(200, 34 * len(top) + 60)}px")


def _upto_chart(rows: list[q.ToolRow]) -> None:
    """Authorized versus captured. Only meaningful where something was authorized."""
    subject = [r for r in rows if r.authorized_atomic > 0][:12]
    if not subject:
        return

    has_upto = any(r.scheme == "upto" for r in subject)
    with t.card(
        "Authorized vs captured",
        "What agents put at risk against what they were actually charged. Under the "
        "`upto` scheme these differ by design: the agent authorizes a ceiling and only "
        "real consumption is captured. The database refuses to store the inverse."
        + ("" if has_upto else " No `upto` tool is priced yet, so these bars match."),
    ):
        subject_r = list(reversed(subject))
        labels = [r.name for r in subject_r]
        authorized = [t.chart_value(r.authorized_atomic, r.decimals) for r in subject_r]
        captured = [t.chart_value(r.captured_atomic, r.decimals) for r in subject_r]

        ui.echart(
            t.chart_options(
                series=[
                    t.bar_series("authorized (ceiling)", authorized, t.SERIES[1], horizontal=True),
                    t.bar_series("captured (charged)", captured, t.SERIES[0], horizontal=True),
                ],
                x_axis=t.axis_value(name=SYM),
                y_axis={**t.axis_category(labels), "boundaryGap": True},
                legend=True,
                grid={"left": 8, "right": 32, "top": 34, "bottom": 8, "containLabel": True},
            )
        ).classes("w-full").style(f"height:{max(220, 48 * len(subject) + 70)}px")


def _table(rows: list[q.ToolRow]) -> None:
    with t.card("Catalogue", "Every priced tool, with the numbers behind the charts."):
        table = t.data_table(
            columns=[
                {
                    "name": "name",
                    "label": "Tool",
                    "field": "name",
                    "align": "left",
                    "sortable": True,
                },
                {
                    "name": "author",
                    "label": "Author",
                    "field": "author",
                    "align": "left",
                    "sortable": True,
                },
                {"name": "scheme", "label": "Scheme", "field": "scheme", "align": "left"},
                {"name": "price", "label": f"Price ({SYM})", "field": "price", "align": "right"},
                {
                    "name": "ceiling",
                    "label": f"Ceiling ({SYM})",
                    "field": "ceiling",
                    "align": "right",
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
                    "label": f"Captured ({SYM})",
                    "field": "captured",
                    "align": "right",
                    "sortable": True,
                },
                {"name": "net", "label": f"Author net ({SYM})", "field": "net", "align": "right"},
                {"name": "used", "label": "Ceiling used", "field": "used", "align": "right"},
                {"name": "state", "label": "State", "field": "state", "align": "left"},
            ],
            rows=[
                {
                    "id": r.tool_id,
                    "name": ("DEMO  " if r.tainted else "") + r.name,
                    "author": r.author,
                    "scheme": r.scheme + (f" / {r.meter}" if r.meter else ""),
                    "price": t.usd(r.price_atomic, r.decimals),
                    "ceiling": (
                        t.usd(r.max_price_atomic, r.decimals)
                        if r.max_price_atomic is not None
                        else "--"
                    ),
                    "calls": r.calls,
                    "captured": t.usd(r.captured_atomic, r.decimals),
                    "net": t.usd(r.author_net_atomic, r.decimals),
                    "used": t.pct_bps(r.capture_bps) if r.authorized_atomic else "--",
                    "state": "enabled" if r.enabled else "disabled",
                }
                for r in rows
            ],
            row_key="id",
            pagination={"rowsPerPage": 15, "sortBy": "captured", "descending": True},
        )
        table.props("wrap-cells")
        ui.label(
            "'Ceiling used' is captured as a percentage of authorized. 100% means the tool "
            "charged the full ceiling; anything lower is money the agent authorized and "
            "was never taken."
        ).style(f"color:{t.INK_MUTED}; font-size:0.72rem")


__all__ = ["render"]
