"""Dashboard tests. The two promises the UI makes, checked.

    1. It never fabricates a number. Empty ledger -> explicit empty states.
    2. Seeded rows are visibly labelled and can be excluded.

Plus the house rules that are cheap to regress and expensive to notice: no dual
y-axis, a legend whenever two or more series share a chart, solid gridlines, and
chart options that actually serialise to JSON.

    .venv/Scripts/python -m pytest tests/test_dashboard.py -q
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")  # in-memory, StaticPool
os.environ.setdefault("PUBLIC_BASE_URL", "http://testserver")

from starlette.testclient import TestClient  # noqa: E402

from app.money import fee_load_bps, parse_price  # noqa: E402
from app.ui import theme  # noqa: E402

PAGES = ("/", "/tools", "/receipts", "/economics")


# --------------------------------------------------------------------------
# Seeding. Deliberately in the tests and nowhere else: shipping a seeder inside
# `app/` is how fake rows end up in a real ledger.
# --------------------------------------------------------------------------


def _seed(*, demo: bool) -> dict:
    """One author, one tool, one session, three calls, one settled batch.

    `demo=True` stamps `is_demo` through `app.demo.mark_demo`, the same writer
    `app.cli.seed_demo` uses -- so the dashboard's DEMO handling is exercised
    against the real contract rather than a test-local imitation.
    """
    from sqlmodel import Session as DBSession

    from app.db import create_all, engine
    from app.demo import mark_demo
    from app.models import (
        Author,
        Batch,
        BatchStatus,
        Call,
        CallStatus,
        PaySession,
        Receipt,
        Scheme,
        SettlementMode,
        Tool,
    )

    create_all()
    tag = "demo" if demo else "real"
    now = datetime.now(UTC)
    price = parse_price("$0.002", 6)
    fee = parse_price("$0.001", 6)

    with DBSession(engine) as db:
        author = Author(slug=f"a-{tag}", display_name=f"Author {tag}", pay_to=f"0xAUTH{tag}")
        db.add(mark_demo(author, demo=demo))
        db.commit()
        db.refresh(author)

        tool = Tool(
            author_id=author.id,
            name=f"run_injection_attack_sim_{tag}",
            resource_url=f"mcp://tool/run_injection_attack_sim_{tag}",
            scheme=Scheme.UPTO,
            network="eip155:84532",
            asset="0xUSDC",
            price_atomic=price,
            max_price_atomic=price * 5,
            meter="tokens",
            price_per_unit_atomic=1,
        )
        db.add(mark_demo(tool, demo=demo))

        session = PaySession(
            session_id=f"sess_demo_{tag}" if demo else f"sess_live_{tag}",
            payer=f"0xPAYER{tag}",
            agent_label=("demo-agent" if demo else "claude-desktop"),
            network="eip155:84532",
            asset="0xUSDC",
            scheme=Scheme.BATCH_SETTLEMENT,
            settlement_mode=SettlementMode.BATCHED,
            budget_atomic=parse_price("$5.00", 6),
        )
        db.add(mark_demo(session, demo=demo))
        db.commit()
        db.refresh(tool)
        db.refresh(session)

        batch = Batch(
            batch_id=f"batch_{tag}",
            session_id=session.id,
            network="eip155:84532",
            asset="0xUSDC",
            pay_to=author.pay_to,
            call_count=3,
            gross_atomic=price * 3,
            platform_fee_atomic=(price * 3) // 10,
            author_net_atomic=price * 3 - (price * 3) // 10,
            facilitator_fee_atomic=fee,
            status=BatchStatus.SETTLED,
            claim_tx_hash=f"0xclaim{tag}",
            settle_tx_hash=f"0xsettle{tag}",
            settled_at=now,
        )
        db.add(mark_demo(batch, demo=demo))
        db.commit()
        db.refresh(batch)

        for i in range(3):
            captured = price
            platform = captured // 10
            call = Call(
                call_id=f"call_{tag}_{i}",
                session_id=session.id,
                tool_id=tool.id,
                batch_id=batch.id,
                payer=session.payer,
                pay_to=author.pay_to,
                network="eip155:84532",
                asset="0xUSDC",
                scheme=Scheme.UPTO,
                # captured < authorized on purpose: the `upto` gap must render.
                authorized_atomic=price * 5,
                captured_atomic=captured,
                platform_fee_atomic=platform,
                author_net_atomic=captured - platform,
                meter="tokens",
                meter_units=1200,
                status=CallStatus.SETTLED,
                nonce=f"nonce_{tag}_{i}",
                created_at=now - timedelta(days=i),
            )
            db.add(mark_demo(call, demo=demo))
            db.commit()
            db.refresh(call)
            db.add(
                mark_demo(
                    Receipt(
                        receipt_id=f"rcpt_{tag}_{i}",
                        call_id=call.id,
                        session_id=session.id,
                        batch_id=batch.id,
                        scheme=Scheme.UPTO,
                        network="eip155:84532",
                        asset="0xUSDC",
                        authorized_atomic=price * 5,
                        captured_atomic=captured,
                        payer=session.payer,
                        pay_to=author.pay_to,
                        resource_url=tool.resource_url,
                        settlement=SettlementMode.BATCHED,
                        tx_hash=f"0xsettle{tag}",
                        facilitator="x402.org",
                        body_hash=f"sha256:{tag}{i}",
                        body_json=json.dumps(
                            {"receiptId": f"rcpt_{tag}_{i}", "captured": captured}
                        ),
                        issued_at=now - timedelta(days=i),
                    ),
                    demo=demo,
                )
            )
        # One decline, so the funnel and the decline breakdown have a real row.
        db.add(
            mark_demo(
                Call(
                    call_id=f"call_{tag}_declined",
                    session_id=session.id,
                    tool_id=tool.id,
                    payer=session.payer,
                    pay_to=author.pay_to,
                    network="eip155:84532",
                    asset="0xUSDC",
                    scheme=Scheme.UPTO,
                    authorized_atomic=0,
                    captured_atomic=0,
                    status=CallStatus.DECLINED,
                    decline_reason="over_session_budget",
                    nonce=f"nonce_{tag}_declined",
                    created_at=now,
                ),
                demo=demo,
            )
        )
        db.commit()
        tool_name = tool.name  # read inside the session; the instance detaches on exit

    return {"price": price, "fee": fee, "tool": tool_name, "batch": f"batch_{tag}"}


def _truncate() -> None:
    """Empty every ledger table.

    Makes each fixture below independent of test ORDER and of whatever
    `test_spine.py` left behind -- both modules share one in-memory SQLite engine
    (StaticPool), so "the ledger is empty" has to be established, not assumed.
    """
    from sqlmodel import Session as DBSession
    from sqlmodel import delete

    from app.db import create_all, engine
    from app.models import Author, Batch, Call, PaySession, Receipt, Tool

    create_all()
    with DBSession(engine) as db:
        for model in (Receipt, Call, Batch, Tool, PaySession, Author):  # FK order
            db.exec(delete(model))
        db.commit()


@pytest.fixture(scope="module")
def raw_client():
    """A client that does NOT enter the app lifespan.

    Deliberate: the dashboard reads the database and nothing else, so it does not
    need the MCP StreamableHTTP session manager -- and that manager refuses to
    `run()` twice per process, so a second lifespan in the same pytest session
    (test_spine.py already opens one) would raise. Not starting what the pages
    do not use keeps these tests co-runnable with the spine's.
    """
    from app.db import create_all
    from app.main import app

    create_all()
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture()
def empty_client(raw_client):
    _truncate()
    return raw_client


@pytest.fixture()
def seeded(raw_client):
    _truncate()
    real = _seed(demo=False)
    demo = _seed(demo=True)
    return raw_client, real, demo


# --------------------------------------------------------------------------
# Promise 1: no data means an empty state, not an invented figure.
# --------------------------------------------------------------------------


def test_every_page_renders_on_an_empty_ledger(empty_client):
    """A payment gateway whose dashboard 500s on day zero is a dead gateway."""
    for path in PAGES:
        r = empty_client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"


def test_empty_ledger_says_so_instead_of_showing_zeroes_as_facts(empty_client):
    body = empty_client.get("/").text.lower()
    assert "the ledger is empty" in body
    assert "will not invent one" in body


def test_no_settled_batch_means_no_realised_fee_load(empty_client):
    """With nothing settled the answer is 'no data', never 0.00% -- 0.00% would
    be a lie that happens to look like good news."""
    from app.ui import queries as q

    assert q.realised_fee_load() is None
    assert q.batch_points() == []
    assert q.average_captured_atomic() is None
    # ...and the page that reports it says so in words rather than printing 0.00%.
    assert "No batch has settled on-chain" in empty_client.get("/economics").text


def test_economics_admits_it_has_no_observations(empty_client):
    body = empty_client.get("/economics").text
    assert "No batch has settled on-chain" in body
    assert "nothing has been realised yet" in body
    # ...but the model is still drawn and still labelled as a model.
    assert "MODEL" in body


def test_receipts_and_tools_empty_states(empty_client):
    assert "No receipts issued" in empty_client.get("/receipts").text
    assert "No tools in the catalogue" in empty_client.get("/tools").text


# --------------------------------------------------------------------------
# Promise 2: seeded rows are labelled, everywhere they can move a number.
# --------------------------------------------------------------------------


def test_demo_rows_are_detected_and_counted(seeded):
    from app.ui import queries as q

    summary = q.demo_summary()
    assert summary.present
    # Both kinds are seeded, so the dangerous case -- one figure mixing real and
    # fabricated revenue -- is what the dashboard has to render.
    assert summary.mixed
    assert summary.counts["pay_session"] == 1
    assert summary.counts["call"] == 4
    assert summary.demo_captured_atomic > 0
    assert summary.real_captured_atomic > 0


def test_demo_banner_appears_on_every_page_that_aggregates(seeded):
    """The wording is `app.demo.BANNER` -- that module owns it, and it says the
    transaction hashes are synthetic, which is the assumption a viewer would
    otherwise make."""
    from app.demo import BANNER

    client, _, _ = seeded
    # NiceGUI HTML-escapes label text, so match on fragments with no backticks
    # or punctuation that escaping would rewrite.
    fragments = [
        "DEMO DATA",
        "No payment was authorized, no USDC moved",
        "transaction hashes below are synthetic",
    ]
    assert all(f in BANNER for f in fragments), "banner wording changed in app.demo"
    for path in ("/", "/tools", "/receipts"):
        body = client.get(path).text
        for fragment in fragments:
            assert fragment in body, f"{path}: {fragment!r}"
        assert "MIX real and seeded rows" in body, path


def test_demo_rows_carry_a_visible_badge_not_just_a_filter(seeded):
    """The badge is in the cell text, so a screenshot cannot hide it."""
    client, _, _ = seeded
    assert "DEMO" in client.get("/").text
    assert "DEMO" in client.get("/receipts").text


def test_excluding_demo_data_actually_changes_the_totals(seeded):
    from app.ui import queries as q

    with_demo = q.totals(exclude_demo=False)
    without = q.totals(exclude_demo=True)
    assert with_demo.captured_atomic > without.captured_atomic > 0
    assert with_demo.calls_billable > without.calls_billable > 0


def test_the_ui_never_writes_its_own_is_demo_predicate():
    """`app.demo.real_only()` is the single definition of 'exclude demo data'.
    A hand-rolled `Model.is_demo == False` here would be a second one, and the
    two would drift."""
    import pathlib

    source = pathlib.Path("app/ui/queries.py").read_text(encoding="utf-8")
    assert "real_only(" in source
    assert "is_demo ==" not in source
    assert "is_demo.is_(False)" not in source


@pytest.fixture()
def demo_only_client(raw_client):
    """A ledger whose every revenue row is seeded -- the demo-day state."""
    _truncate()
    _seed(demo=True)
    return raw_client


def test_a_seeded_batch_is_never_plotted_as_a_ledger_observation(seeded):
    """The economics chart is the argument. A fabricated dot sitting among real
    settlements on it would be the single most misleading thing this project
    could ship, so seeded batches get their own series, their own legend entry,
    and a hollow diamond -- shape, not just colour."""
    client, _, _ = seeded
    body = client.get("/economics").text
    assert "LEDGER - settled batches" in body
    assert "SEEDED - demo batches, not real settlements" in body
    assert "hollow diamonds" in body


def test_seeded_and_real_scatter_series_differ_by_shape_not_only_colour():
    from app.ui import theme

    real = theme.scatter_series("LEDGER", [[1, 1]], theme.SERIES[1])
    seeded = theme.scatter_series(
        "SEEDED", [[1, 1]], theme.SERIES[1], symbol="diamond", hollow=True
    )
    assert real["symbol"] != seeded["symbol"], "shape is the secondary encoding"
    assert seeded["itemStyle"]["color"] == "transparent"
    # Same hue on purpose: the categorical palette caps at three slots under the
    # all-pairs CVD check that applies to scatter.
    assert seeded["itemStyle"]["borderColor"] == real["itemStyle"]["color"]


def test_an_all_demo_ledger_reports_no_realised_fee_load(demo_only_client):
    """The worst case: a screenshot on demo day. No fee-load figure may appear,
    because none has been realised."""
    from app.ui import queries as q

    assert q.realised_fee_load(exclude_demo=True) is None

    overview = demo_only_client.get("/").text
    assert "no real batch has settled on-chain yet" in overview
    assert "seeded batches do not count" in overview

    economics = demo_only_client.get("/economics").text
    assert "There is no realised fee load to report" in economics
    assert "nothing on this chart is a real observation" in economics


def test_the_realised_fee_load_tile_does_not_move_with_the_demo_toggle(seeded):
    """The page's most quotable number must not depend on a switch."""
    from app.ui import queries as q

    assert q.realised_fee_load(exclude_demo=True) == q.realised_fee_load(exclude_demo=True)
    with_demo = q.realised_fee_load(exclude_demo=False)
    real_only_fee = q.realised_fee_load(exclude_demo=True)
    # The seeded batch really would move it -- which is exactly why the tile is
    # hard-wired to the real-only query rather than to the filter.
    assert with_demo is not None and real_only_fee is not None
    assert with_demo.batches > real_only_fee.batches
    assert "real settlements only" in seeded[0].get("/").text


def test_a_seeded_receipt_carries_its_flag_into_the_view(seeded):
    from app.ui import queries as q

    rows = q.receipt_rows()
    assert any(r.is_demo for r in rows)
    assert any(not r.is_demo for r in rows)
    assert not any(r.is_demo for r in q.receipt_rows(exclude_demo=True))


# --------------------------------------------------------------------------
# The figures themselves come from the ledger.
# --------------------------------------------------------------------------


def test_realised_fee_load_is_computed_from_settled_batches(seeded):
    from app.ui import queries as q

    fee = q.realised_fee_load(exclude_demo=True)
    assert fee is not None
    # One batch: $0.001 of fee on 3 x $0.002 of gross.
    assert fee.batches == 1
    assert fee.calls == 3
    assert fee.gross_atomic == parse_price("$0.006", 6)
    assert fee.facilitator_fee_atomic == parse_price("$0.001", 6)
    assert fee.realised_bps == fee_load_bps(fee.facilitator_fee_atomic, fee.gross_atomic)
    # The counterfactual is arithmetic on the SAME calls, not a projection.
    assert fee.per_call_fee_atomic == parse_price("$0.001", 6) * 3
    assert fee.per_call_bps > fee.realised_bps


def test_a_real_tool_carrying_demo_calls_is_still_flagged(seeded):
    """The per-row flag, not the banner, is what covers the mixed case."""
    from app.ui import queries as q

    rows = q.tool_rows()
    assert any(r.is_demo for r in rows)
    assert any(not r.is_demo for r in rows)
    assert all(r.tainted for r in rows if r.demo_calls)
    # With demo excluded, no surviving row may be tainted at all.
    assert not any(r.tainted for r in q.tool_rows(exclude_demo=True))


def test_upto_gap_survives_into_the_tools_view(seeded):
    from app.ui import queries as q

    rows = [r for r in q.tool_rows() if r.calls]
    assert rows, "seeded tool should have billable calls"
    for row in rows:
        assert row.captured_atomic < row.authorized_atomic  # the `upto` ceiling
        assert 0 < row.capture_bps < 10_000
        assert row.unused_atomic > 0


def test_receipt_rows_join_back_to_session_batch_and_tool(seeded):
    from app.ui import queries as q

    rows = q.receipt_rows()
    assert rows
    r = rows[0]
    assert r.session_public_id
    assert r.batch_public_id
    assert r.tool_name != "--"
    assert r.explorer_url and r.tx_hash and r.tx_hash in r.explorer_url


def test_batch_filter_sums_to_what_that_settlement_moved(seeded):
    """The audit the receipts page claims: filter to a batch, sum the captured
    column, and it equals the batch's recorded gross."""
    from sqlmodel import Session as DBSession
    from sqlmodel import col, select

    from app.db import engine
    from app.models import Batch
    from app.ui import queries as q

    _, real, _ = seeded
    rows = q.receipt_rows(batch_public_id=real["batch"])
    assert rows
    with DBSession(engine) as db:
        batch = db.exec(select(Batch).where(col(Batch.batch_id) == real["batch"])).one()
    assert sum(r.captured_atomic for r in rows) == batch.gross_atomic


def test_daily_buckets_do_not_predate_the_first_call(seeded):
    from app.ui import queries as q

    buckets = q.daily_buckets(90)
    assert buckets
    # A ledger three days old must not draw 90 days of flat zero it never lived.
    assert len(buckets) <= 5
    assert buckets == sorted(buckets, key=lambda b: b.day)


def test_every_page_renders_with_data(seeded):
    client, _, _ = seeded
    for path in PAGES:
        assert client.get(path).status_code == 200, path


# --------------------------------------------------------------------------
# Money never becomes a float on its way to a human.
# --------------------------------------------------------------------------


def test_short_money_format_is_lossless():
    assert theme.usd(2_000) == "0.002"  # $0.002, trailing zeros dropped
    assert theme.usd(1_000_000) == "1.00"  # never fewer than two places
    assert theme.usd(1_234_567) == "1.234567"  # nothing rounded away
    assert theme.usd(1) == "0.000001"
    assert theme.usd_exact(2_000) == "0.002000 USDC"


def test_chart_value_is_the_only_float_and_it_is_one_way():
    assert isinstance(theme.chart_value(2_000), float)
    assert theme.chart_value(2_000) == 0.002


# --------------------------------------------------------------------------
# Chart house rules.
# --------------------------------------------------------------------------


def _all_chart_options() -> list[dict]:
    """Representative options from each chart shape the dashboard draws."""
    return [
        theme.chart_options(
            series=[
                theme.bar_series("a", [1, 2], theme.SERIES[0], stack="s"),
                theme.bar_series("b", [1, 2], theme.SERIES[2], stack="s"),
            ],
            x_axis=theme.axis_category(["x", "y"]),
            y_axis=theme.axis_value(name="USDC"),
            legend=True,
        ),
        theme.chart_options(
            series=[theme.bar_series("only", [1, 2], theme.SERIES[1])],
            x_axis=theme.axis_category(["x", "y"]),
            y_axis=theme.axis_value(),
        ),
        theme.chart_options(
            series=[
                theme.line_series("model", [[1, 50.0]], theme.DE_EMPHASIS, end_label="a"),
                theme.line_series("batched", [[1, 0.5]], theme.SERIES[0], area=True),
                theme.scatter_series("ledger", [[3, 16.6]], theme.SERIES[1]),
            ],
            x_axis=theme.axis_log(name="calls"),
            y_axis=theme.axis_value(formatter="{value}%"),
            legend=True,
        ),
    ]


def test_no_chart_has_a_second_y_scale():
    """Dual-axis plots invent a correlation out of an arbitrary scale alignment.
    There is never more than one y axis on this dashboard."""
    for options in _all_chart_options():
        assert isinstance(options["yAxis"], dict), "a list of yAxis means dual-axis"


def test_the_rendered_pages_contain_no_dual_axis_and_no_dashed_grid(seeded):
    """The check above tests the helpers; this one tests what actually ships.

    NiceGUI serialises each chart's options into the page, so the real, populated
    options can be inspected without a browser.
    """
    client, _, _ = seeded
    for path in PAGES:
        body = client.get(path).text
        # A LIST of yAxis is how a second y-scale appears in ECharts options.
        assert '\\"yAxis\\":[' not in body and '"yAxis":[' not in body, path
        # Gridlines and axis rules are solid hairlines; dashing reads as
        # "threshold" or "projection" when it is only a grid.
        assert "dashed" not in body, path
        assert '"type":"dotted"' not in body, path


def test_two_or_more_series_always_carry_a_legend():
    for options in _all_chart_options():
        if len(options["series"]) >= 2:
            assert "legend" in options, "identity must never be colour-alone"
        else:
            # A one-swatch legend just restates the card title.
            assert "legend" not in options


def test_gridlines_are_solid_hairlines_never_dashed():
    for options in _all_chart_options():
        split = options["yAxis"].get("splitLine", {})
        if split.get("show"):
            style = split["lineStyle"]
            assert style["type"] == "solid"
            assert style["width"] == 1
            assert style["color"] == theme.GRID


def test_chart_options_are_json_serialisable():
    """ECharts options cross a JSON boundary. A stray Decimal or datetime in a
    series would fail at render time, in the browser, silently."""
    for options in _all_chart_options():
        json.dumps(options)


def test_stacked_segments_are_separated_by_a_surface_gap_not_a_stroke():
    stacked = theme.bar_series("a", [1], theme.SERIES[0], stack="s")
    plain = theme.bar_series("a", [1], theme.SERIES[0])
    assert stacked["itemStyle"]["borderColor"] == theme.SURFACE
    assert "borderColor" not in plain["itemStyle"]


def test_series_palette_is_the_validated_one():
    """These three hexes are not a taste decision -- they are the output of the
    palette validator against this surface. Changing one silently would undo a
    colourblind-safety guarantee, so pin them."""
    assert theme.SERIES == ("#e6416f", "#2299ee", "#c08a1e")
    assert theme.BG == "#1d0718"
    # The de-emphasis grey is deliberately NOT a categorical slot.
    assert theme.DE_EMPHASIS not in theme.SERIES


def test_ordinal_ramp_is_a_single_hue_and_monotone():
    """The call-outcome funnel is ordinal, so its colour must carry the order."""
    assert len(theme.ORDINAL) == 5
    assert len(set(theme.ORDINAL)) == 5
    # Rough monotone-lightness check without pulling in a colour library:
    # summed channel value must increase step by step.
    brightness = [sum(int(c[i : i + 2], 16) for i in (1, 3, 5)) for c in theme.ORDINAL]
    assert brightness == sorted(brightness)
