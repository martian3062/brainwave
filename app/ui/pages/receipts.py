"""Receipts -- the evidence explorer.

A receipt is only useful if it can be walked back to the money. This page exists
to do exactly that walk, in both directions:

    receipt -> call -> session -> batch -> on-chain tx -> Basescan

Filter by session to see every call an agent paid for in one run; filter by batch
to see every call one on-chain settlement covered. The sum of the captured column
under a batch filter is what that transaction moved -- that equality is the audit,
and it is checkable here without trusting anything this gateway says.

Under `upto`, authorized and captured are different numbers and BOTH are shown.
Showing only the charge is what makes a payment system a black box.
"""

from __future__ import annotations

import json

from nicegui import ui

from app.config import settings
from app.ui import queries as q
from app.ui import theme as t

SYM = settings.x402_asset_symbol
ANY = "-- any --"


def render() -> None:
    state: dict = {
        "session": None,
        "batch": None,
        "settled_only": False,
        "search": "",
        "exclude_demo": False,
    }
    demo = q.demo_summary()

    with t.page_shell(
        "/receipts",
        "Receipts",
        "Every metered call leaves one. Filter by session or by batch, then follow the "
        "transaction hash out to the block explorer.",
    ):
        _filter_row(state, demo, lambda: body.refresh())

        @ui.refreshable
        def body() -> None:
            _body(state, demo)

        body()


def _filter_row(state: dict, demo, refresh) -> None:
    sessions, batches = q.receipt_filter_options()

    with (
        ui.row()
        .classes("w-full items-center gap-4 p-3 rounded-xl flex-wrap")
        .style(f"background:{t.SURFACE}; border:1px solid {t.GRID}")
    ):
        ui.icon("filter_alt").style(f"color:{t.INK_MUTED}")

        def _set(key: str, value):
            state[key] = None if value in (ANY, "", None) else value
            refresh()

        ui.select(
            options=[ANY, *sessions],
            value=state["session"] or ANY,
            label="session",
            on_change=lambda e: _set("session", e.value),
        ).props("dense outlined dark").style("min-width:16rem").tooltip(
            "Only sessions that actually have receipts are offered."
        )

        ui.select(
            options=[ANY, *batches],
            value=state["batch"] or ANY,
            label="batch",
            on_change=lambda e: _set("batch", e.value),
        ).props("dense outlined dark").style("min-width:16rem").tooltip(
            "Filter to one on-chain settlement. The captured column then sums to "
            "what that transaction moved."
        )

        ui.input(
            label="search",
            placeholder="receipt id, tx hash, payer, resource",
            value=state["search"],
            on_change=lambda e: (state.update(search=e.value or ""), refresh()),
        ).props('dense outlined dark clearable debounce="400"').style("min-width:18rem")

        ui.switch(
            "settled only",
            value=state["settled_only"],
            on_change=lambda e: (state.update(settled_only=bool(e.value)), refresh()),
        ).props("dense dark").tooltip("Receipts that carry a transaction hash.")

        if demo.present:
            ui.switch(
                "exclude demo data",
                value=state["exclude_demo"],
                on_change=lambda e: (state.update(exclude_demo=bool(e.value)), refresh()),
            ).props("dense dark").style(f"color:{t.WARN_INK}")


def _body(state: dict, demo) -> None:
    rows = q.receipt_rows(
        session_public_id=state["session"],
        batch_public_id=state["batch"],
        settled_only=bool(state["settled_only"]),
        search=state["search"] or None,
        exclude_demo=bool(state["exclude_demo"]),
    )

    if demo.present:
        t.demo_banner(demo, excluded=bool(state["exclude_demo"]))

    if not rows:
        _empty(state)
        return

    _rollup(rows, state)
    _table(rows)


def _empty(state: dict) -> None:
    filtered = any(
        (
            state["session"],
            state["batch"],
            state["settled_only"],
            state["search"],
            state["exclude_demo"],
        )
    )
    if filtered:
        t.empty_state(
            "No receipts match these filters",
            "Nothing in the ledger satisfies the current filter set. Clear a filter "
            "to widen the search.",
            icon="search_off",
        )
        return
    t.empty_state(
        "No receipts issued",
        "A receipt is written when a paid call completes. None has yet, so there is "
        "nothing to show -- this page will not manufacture one. The receipt body, its "
        "sha256, and the facilitator attestation all come straight from the `receipt` "
        "table when they exist.",
        icon="receipt_long",
    )


def _rollup(rows: list[q.ReceiptRow], state: dict) -> None:
    """The audit line: what this selection captured, and what settled it."""
    captured = sum(r.captured_atomic for r in rows)
    authorized = sum(r.authorized_atomic for r in rows)
    settled = [r for r in rows if r.tx_hash]
    txs = {r.tx_hash for r in settled if r.tx_hash}
    decimals = rows[0].decimals

    with t.stat_row():
        t.stat(
            "receipts",
            t.compact_int(len(rows)),
            f"{t.compact_int(len(settled))} carry a tx hash",
            icon="receipt_long",
        )
        t.stat(
            "captured",
            t.usd(captured, decimals, symbol=True),
            f"authorized {t.usd(authorized, decimals, symbol=True)}",
            icon="payments",
        )
        t.stat(
            "on-chain settlements",
            t.compact_int(len(txs)),
            "distinct transactions covering these receipts",
            tone="ok" if txs else "warn",
            icon="link",
        )
        if txs:
            t.stat(
                "calls per settlement",
                f"{len(settled) / len(txs):.1f}",
                "the ratio the fee argument turns on",
                tone="ok",
                icon="functions",
            )
        else:
            t.stat(
                "calls per settlement",
                "no data",
                "nothing has settled on-chain yet",
                tone="warn",
                icon="pending",
            )

    if state["batch"]:
        t.note(
            f"Filtered to batch {state['batch']}. These {t.compact_int(len(rows))} receipts "
            f"sum to {t.usd(captured, decimals, symbol=True)} captured. That figure is what "
            "the settlement transaction moved -- open the explorer link on any row and "
            "check it against the chain.",
            tone="ok",
            icon="fact_check",
        )


def _table(rows: list[q.ReceiptRow]) -> None:
    with t.card(
        "Receipts",
        "Click a row for the full receipt body, its hash, and the facilitator attestation.",
    ):
        by_id = {r.receipt_id: r for r in rows}
        table = t.data_table(
            columns=[
                {
                    "name": "receipt",
                    "label": "Receipt",
                    "field": "receipt",
                    "align": "left",
                    "sortable": True,
                },
                {
                    "name": "issued",
                    "label": "Issued (UTC)",
                    "field": "issued",
                    "align": "left",
                    "sortable": True,
                },
                {"name": "tool", "label": "Tool", "field": "tool", "align": "left"},
                {"name": "scheme", "label": "Scheme", "field": "scheme", "align": "left"},
                {
                    "name": "authorized",
                    "label": f"Authorized ({SYM})",
                    "field": "authorized",
                    "align": "right",
                },
                {
                    "name": "captured",
                    "label": f"Captured ({SYM})",
                    "field": "captured",
                    "align": "right",
                    "sortable": True,
                },
                {"name": "session", "label": "Session", "field": "session", "align": "left"},
                {"name": "batch", "label": "Batch", "field": "batch", "align": "left"},
                {"name": "tx", "label": "Settlement", "field": "tx", "align": "left"},
            ],
            rows=[
                {
                    "id": r.receipt_id,
                    "receipt": ("DEMO  " if r.is_demo else "") + t.shorten(r.receipt_id, 14, 4),
                    "issued": r.issued_at.strftime("%Y-%m-%d %H:%M:%S") if r.issued_at else "--",
                    "tool": r.tool_name,
                    "scheme": r.scheme,
                    "authorized": t.usd(r.authorized_atomic, r.decimals),
                    "captured": t.usd(r.captured_atomic, r.decimals),
                    "session": t.shorten(r.session_public_id, 12, 4),
                    "batch": t.shorten(r.batch_public_id, 12, 4) if r.batch_public_id else "--",
                    "tx": t.shorten(r.tx_hash, 10, 6) if r.tx_hash else "pending",
                }
                for r in rows
            ],
            row_key="id",
            pagination={"rowsPerPage": 20, "sortBy": "issued", "descending": True},
        )

        # ONE dialog, reused. Building a fresh `ui.dialog()` per click would leak
        # an element into the page on every row a user opens.
        #
        # Row click rather than a link inside the cell: a table cell slot means a
        # Vue/HTML template string, and there is no markup anywhere in this
        # project.
        with (
            ui.dialog() as dialog,
            ui.card()
            .classes("w-full max-w-3xl gap-4 p-6")
            .style(f"background:{t.SURFACE}; border:1px solid {t.GRID}") as panel,
        ):
            pass

        def _open(event) -> None:
            row = event.args[1] if len(event.args) > 1 else None
            receipt = by_id.get((row or {}).get("id"))
            if receipt is None:
                return
            panel.clear()
            with panel:
                _detail(receipt, dialog)
            dialog.open()

        table.on("rowClick", _open)
        ui.label(
            "Amounts are shown at full asset precision in the detail view. "
            "'pending' means the receipt exists but its batch has not settled."
        ).style(f"color:{t.INK_MUTED}; font-size:0.72rem")


def _detail(r: q.ReceiptRow, dialog) -> None:
    """Fill the (single, reused) dialog panel with one receipt."""
    with ui.row().classes("w-full items-start gap-3 no-wrap"):
        with ui.column().classes("gap-1"):
            t.eyebrow("receipt")
            ui.label(r.receipt_id).style(
                f"color:{t.CREAM}; font-size:1.05rem; font-weight:600; font-family:monospace"
            )
        ui.space()
        if r.is_demo:
            t.demo_badge()
        ui.button(icon="close", on_click=dialog.close).props("flat round dense dark")

    ui.separator().style(f"background:{t.GRID}")

    with ui.column().classes("w-full gap-2"):
        t.kv("issued", r.issued_at.isoformat() if r.issued_at else "--")
        t.kv("resource", r.resource_url, mono=True)
        t.kv("scheme / settlement", f"{r.scheme} / {r.settlement}")
        t.kv("network", r.network, mono=True)
        t.kv("authorized", t.usd_exact(r.authorized_atomic, r.decimals))
        t.kv("captured", t.usd_exact(r.captured_atomic, r.decimals))
        if r.captured_atomic < r.authorized_atomic:
            t.kv(
                "never charged",
                t.usd_exact(r.authorized_atomic - r.captured_atomic, r.decimals),
                tone="ok",
            )
        t.kv("payer", r.payer, mono=True)
        t.kv("pay to", r.pay_to, mono=True)
        t.kv("session", r.session_public_id, mono=True)
        t.kv("batch", r.batch_public_id or "not batched yet", mono=True)
        t.kv("facilitator", r.facilitator or "--")
        t.kv("body sha256", r.body_hash or "--", mono=True)
        t.kv(
            "attestation",
            r.attestation or "none -- this receipt is not independently verifiable",
            mono=bool(r.attestation),
            tone="default" if r.attestation else "warn",
        )
        t.kv(
            "verification",
            f"{r.verify_status} at {r.verified_at.isoformat()}"
            if r.verify_status and r.verified_at
            else (r.verify_status or "not verified yet"),
            tone="ok" if r.verify_status == "valid" else "default",
        )

    ui.separator().style(f"background:{t.GRID}")

    t.eyebrow("receipt body, verbatim")
    ui.label(
        "Exactly the JSON that was returned to the agent. `body sha256` above is its "
        "digest, so a tampered receipt fails locally before anyone calls the facilitator."
    ).style(f"color:{t.INK_MUTED}; font-size:0.75rem")
    ui.code(_pretty(r.body_json), language="json").classes("w-full").style(
        f"background:{t.BG}; max-height:16rem; overflow:auto"
    )

    with ui.row().classes("w-full items-center gap-3"):
        if r.is_demo:
            # A seeded receipt's tx hash is synthetic. Offering a Basescan link
            # for it would invite the one conclusion this dashboard must never
            # let a viewer draw, so the link is withheld and the hash is shown
            # marked instead.
            t.note(
                "Seeded row: the transaction hash below is synthetic and no explorer link "
                "is offered for it, because there is nothing on chain to open. "
                f"Hash as stored: {r.tx_hash or 'none'}",
                tone="warn",
                icon="science",
            )
        elif r.explorer_url:
            ui.button(
                "Open on the block explorer",
                icon="open_in_new",
                on_click=lambda: ui.navigate.to(r.explorer_url, new_tab=True),
            ).props("outline dark").style(f"color:{t.ACCENT}")
            ui.label(r.tx_hash or "").style(
                f"color:{t.INK_MUTED}; font-size:0.72rem; font-family:monospace"
            )
        else:
            t.note(
                "No transaction hash yet: this call is captured in the ledger and is "
                "waiting for its batch to settle. Nothing is shown here until the chain "
                "confirms it.",
                tone="warn",
                icon="pending",
            )


def _pretty(body: str) -> str:
    """Pretty-print the stored body WITHOUT changing it if it is not JSON.

    The stored string is evidence; if it does not parse it is displayed raw
    rather than coerced into looking well-formed.
    """
    try:
        return json.dumps(json.loads(body or "{}"), indent=2, sort_keys=True)
    except (ValueError, TypeError):
        return body or "{}"


__all__ = ["render"]
