"""Populate the ledger with demo data, labelled as such at every level.

    python -m app.cli seed_demo
    python -m app.cli seed_demo --sessions 12 --seed 7
    python -m app.cli seed_demo --reset          # purge demo rows, then reseed
    python -m app.cli seed_demo --reset-only     # purge demo rows and stop

The dashboard needs rows before a single agent has paid anything. That is the
only reason this command exists, and it is the most dangerous command in the
repository: a screenshot of fabricated USDC revenue presented as a payment
gateway's ledger is a lie regardless of intent.

So the labelling is structural, and it is not one check that could be forgotten:

    schema      every table has `is_demo` (migration 0002)
    writer      every row here goes through `app.demo.mark_demo`, which raises
                on a model without the column -- see `_add()` below, which is
                the ONLY insert path in this file
    receipt     `"isDemo": true` sits INSIDE the hashed receipt body, so a
                receipt copied out of the database still says so and the flag
                cannot be removed without breaking `body_hash`
    tx hashes   synthetic and prefixed `0xdead`; never linked to an explorer
    reads       `app.demo.summary()` tells the dashboard to show the banner
    test        `tests/test_cli.py` asserts the seeder cannot produce a row
                with `is_demo=False`

The data itself is deterministic: same `--seed`, same rows, same money, same
receipt hashes. Screenshots stay stable and a diff of two runs is empty.

The tool catalogue is modelled on the real ERAYA MCP server
(`backend/mcp_server.py`, 18 tools), because pricing a genuinely expensive tool
like `run_injection_attack_sim` -- which runs a full injection attack through
the KAVACHA loop -- is a far better argument for paid MCP than pricing a toy.
"""

from __future__ import annotations

import argparse
import random
from datetime import timedelta
from typing import Any

from app.cli._out import Printer
from app.cli._x402 import new_id, synthetic_tx_hash
from app.config import settings
from app.money import format_atomic, parse_price, split_take
from app.pay import receipts as receipts_mod

# --------------------------------------------------------------------------
# The catalogue. Prices are the interesting part: the range from a $0.0005
# status read to a $0.05 attack simulation is what makes per-call settlement
# obviously wrong at the bottom of the range and obviously fine at the top.
# --------------------------------------------------------------------------

AUTHORS: list[dict[str, Any]] = [
    {
        "slug": "eraya-swarm",
        "display_name": "ERAYA Swarm (demo)",
        "pay_to": "0x000000000000000000000000000000000000dEaD",
        "platform_take_bps": None,  # inherits the global default
    },
    {
        "slug": "kavacha-security",
        "display_name": "KAVACHA Security (demo)",
        "pay_to": "0x00000000000000000000000000000000DeaDBeef",
        "platform_take_bps": 500,  # negotiated 5%
    },
    {
        "slug": "casper-chain",
        "display_name": "Casper Chain Tools (demo)",
        "pay_to": "0x000000000000000000000000000000000000BEEF",
        "platform_take_bps": None,
    },
]

TOOLS: list[dict[str, Any]] = [
    # -- cheap reads: the case FOR batching -------------------------------
    {
        "author": "eraya-swarm",
        "name": "get_swarm_status",
        "price": "$0.0005",
        "scheme": "exact",
        "tags": "swarm,status",
        "description": "Live status of the four-archetype agent swarm.",
    },
    {
        "author": "eraya-swarm",
        "name": "list_agents",
        "price": "$0.0005",
        "scheme": "exact",
        "tags": "swarm,discovery",
        "description": "Registered agents, archetypes and health.",
    },
    {
        "author": "eraya-swarm",
        "name": "get_open_incidents",
        "price": "$0.002",
        "scheme": "exact",
        "tags": "swarm,incidents",
        "description": "Open incidents by domain (5g, cloud, icu).",
    },
    {
        "author": "casper-chain",
        "name": "casper_chain_status",
        "price": "$0.001",
        "scheme": "exact",
        "tags": "casper,chain",
        "description": "Casper node height, era and sync state.",
    },
    {
        "author": "casper-chain",
        "name": "casper_deployed_contracts",
        "price": "$0.0015",
        "scheme": "exact",
        "tags": "casper,contracts",
        "description": "Deployed Odra contract hashes and package hashes.",
    },
    # -- metered: the case FOR `upto` --------------------------------------
    {
        "author": "eraya-swarm",
        "name": "get_recent_decisions",
        "price": "$0.001",
        "max_price": "$0.05",
        "scheme": "upto",
        "meter": "decisions",
        "unit_price": "$0.0004",
        "tags": "swarm,planner",
        "description": "PlannerAgent decisions with confidence and Guardian verdict.",
    },
    {
        "author": "eraya-swarm",
        "name": "get_a2a_message_log",
        "price": "$0.001",
        "max_price": "$0.04",
        "scheme": "upto",
        "meter": "messages",
        "unit_price": "$0.0002",
        "tags": "swarm,a2a",
        "description": "Signed agent-to-agent message log.",
    },
    # -- expensive work: the case FOR paid MCP at all ----------------------
    {
        "author": "kavacha-security",
        "name": "run_injection_attack_sim",
        "price": "$0.05",
        "scheme": "exact",
        "tags": "security,simulation,redteam",
        "description": (
            "Run a live prompt-injection attack through the KAVACHA kill-shot loop: "
            "InjectionSentinel detection, OPA policy veto, HMAC-signed rejection, "
            "audit write. Returns the full timeline and the BLOCKED verdict."
        ),
    },
    {
        "author": "kavacha-security",
        "name": "run_identity_spoof_sim",
        "price": "$0.03",
        "scheme": "exact",
        "tags": "security,simulation,a2a",
        "description": "Forge an A2A action.request with a wrong HMAC key; verify the rejection.",
    },
]

#: Agents that call the catalogue. Labels only -- the payer addresses are
#: derived deterministically and are not funded.
AGENTS = [
    ("claude-desktop", "did:agent:claude-desktop-demo"),
    ("langchain-quant", "did:agent:langchain-quant-demo"),
    ("cursor-ide", None),
    ("autogen-research", "did:agent:autogen-research-demo"),
]

#: Declines the Guardian actually produces, with the weights that make a
#: realistic funnel: most calls go through, a few are refused.
DECLINE_REASONS = ["over_session_budget", "per_call_max", "not_allowlisted", "needs_escalation"]

#: Namespace for every seeded tool and author.
#:
#: `tool.name` is UNIQUE, and the names above are modelled on the REAL catalogue
#: (`run_injection_attack_sim`, `casper_chain_status`, ... are registered by
#: `app.catalogue` at startup). Seeding without a namespace collides on the
#: unique index the moment the gateway has been started once, and -- worse -- a
#: seeded row that *did* land under a real tool's name would attach demo revenue
#: to a real tool in every dashboard query.
DEMO_PREFIX = "demo_"


def demo_tool_name(name: str) -> str:
    return f"{DEMO_PREFIX}{name}"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete every demo row (and nothing else) first, then reseed",
    )
    parser.add_argument(
        "--reset-only",
        action="store_true",
        help="delete every demo row and stop; seed nothing",
    )
    parser.add_argument("--sessions", type=int, default=8, help="sessions to create (default 8)")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed; same seed, same rows")
    parser.add_argument(
        "--days", type=int, default=7, help="spread activity over this many past days"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow seeding when APP_ENV=production (refused by default)",
    )


def run(args: argparse.Namespace, out: Printer) -> int:
    from sqlmodel import Session as DBSession

    from app.db import create_all, engine
    from app.demo import BANNER, purge, summary

    if settings.app_env == "production" and not args.force:
        out.fail("APP_ENV=production -- refusing to write demo rows into a live ledger")
        out.detail(
            "If this really is what you want, pass --force. Consider first whether the "
            "dashboard you are trying to fill is one somebody will read as real."
        )
        return 2

    create_all()

    removed: dict[str, int] | None = None
    if args.reset or args.reset_only:
        with DBSession(engine) as db:
            removed = purge(db)
        out.section("reset")
        for table, count in removed.items():
            (out.ok if count else out.skip)(f"{table:<14}{count} demo rows removed")
        out.raw()
        out.ok(f"{sum(removed.values())} demo rows removed; real rows untouched")
        if args.reset_only:
            if out.json_mode:
                out.emit_json({"reset": removed, "seeded": None})
            return 0

    out.title(
        "ERAYA x BRAINWAVE - seed demo ledger",
        f"deterministic (seed={args.seed}); every row carries is_demo=True",
    )
    out.banner(BANNER)

    result = _seed(args, out)

    with DBSession(engine) as db:
        state = summary(db)

    out.section("ledger now contains")
    for table, count in state.counts.items():
        out.kv(table, f"{count} demo", width=16, note=f"{state.real_counts[table]} real")
    out.raw()
    if state.mixed:
        out.warn(
            "the ledger now holds BOTH demo and real rows. Every revenue figure must be "
            "labelled per row, not just bannered -- use app.demo.real_only() in any view "
            "that reports revenue as fact."
        )
    else:
        out.ok("every row in the ledger is demo data")

    if out.json_mode:
        out.emit_json({"reset": removed, "seeded": result, "state": state.as_dict()})
    return 0


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------


def _seed(args: argparse.Namespace, out: Printer) -> dict[str, Any]:
    from app.db import session_scope
    from app.demo import mark_demo
    from app.models import (
        Author,
        Batch,
        BatchStatus,
        Call,
        CallStatus,
        PaySession,
        Scheme,
        SessionStatus,
        SettlementMode,
        Tool,
        utcnow,
    )

    rng = random.Random(args.seed)
    d = settings.x402_asset_decimals
    sym = settings.x402_asset_symbol
    network = settings.x402_network
    asset = settings.asset_address
    now = utcnow()

    counts = {"authors": 0, "tools": 0, "sessions": 0, "calls": 0, "batches": 0, "receipts": 0}
    revenue = 0

    with session_scope() as db:

        def _add(row):
            """The only place this module constructs a ledger row.

            Funnelling every insert through `mark_demo` is what makes the
            guarantee checkable rather than aspirational: there is no second
            code path here that could forget the flag, and `mark_demo` raises on
            a model that lacks the column instead of silently doing nothing.

            Receipts are the one exception, and deliberately so: they go through
            `app.pay.receipts.issue()`, the same issuer the running gateway
            uses, which sets `is_demo` from the `{"isDemo": True}` carried
            inside the hashed body. A second receipt writer would be a second
            canonicalisation, and two canonicalisations drift.
            """
            db.add(mark_demo(row))
            db.flush()
            return row

        # -- authors -------------------------------------------------------
        authors: dict[str, Author] = {}
        for spec in AUTHORS:
            authors[spec["slug"]] = _add(
                Author(
                    slug=f"{DEMO_PREFIX}{spec['slug']}",
                    display_name=spec["display_name"],
                    pay_to=spec["pay_to"],
                    contact_email=f"{spec['slug']}@example.invalid",
                    platform_take_bps=spec["platform_take_bps"],
                    created_at=now - timedelta(days=args.days + 3),
                )
            )
            counts["authors"] += 1

        # -- tools ---------------------------------------------------------
        tools: list[Tool] = []
        for spec in TOOLS:
            author = authors[spec["author"]]
            tools.append(
                _add(
                    Tool(
                        author_id=author.id,
                        name=demo_tool_name(spec["name"]),
                        resource_url=f"mcp://tool/{demo_tool_name(spec['name'])}",
                        description=spec["description"],
                        tags=spec["tags"],
                        scheme=Scheme(spec["scheme"]),
                        network=network,
                        asset=asset,
                        asset_decimals=d,
                        price_atomic=parse_price(spec["price"], d),
                        max_price_atomic=(
                            parse_price(spec["max_price"], d) if spec.get("max_price") else None
                        ),
                        meter=spec.get("meter"),
                        price_per_unit_atomic=(
                            parse_price(spec["unit_price"], d) if spec.get("unit_price") else None
                        ),
                        created_at=now - timedelta(days=args.days + 2),
                    )
                )
            )
            counts["tools"] += 1

        # -- sessions ------------------------------------------------------
        for index in range(args.sessions):
            label, identity = AGENTS[index % len(AGENTS)]
            payer = _demo_payer(label, index)
            # Oldest first, so the dashboard's "recent activity" ordering has
            # something to order.
            opened = now - timedelta(days=rng.uniform(0, args.days), minutes=rng.randint(0, 600))
            # The last two sessions stay open so the dashboard has live rows and
            # `close_batch --dry-run` has something to plan against.
            still_open = index >= args.sessions - 2

            session_tools = rng.sample(tools, k=rng.randint(1, min(4, len(tools))))
            call_count = rng.randint(3, 40)

            first = session_tools[0].name.removeprefix(DEMO_PREFIX)
            pay_to = authors[next(t["author"] for t in TOOLS if t["name"] == first)].pay_to

            pay_session = _add(
                PaySession(
                    session_id=new_id("sess"),
                    payer=payer,
                    agent_label=label,
                    agent_identity=identity,
                    network=network,
                    asset=asset,
                    asset_decimals=d,
                    scheme=Scheme.BATCH_SETTLEMENT,
                    settlement_mode=SettlementMode.BATCHED,
                    status=SessionStatus.OPEN if still_open else SessionStatus.SETTLED,
                    channel_id="0x" + f"{rng.getrandbits(256):064x}",
                    budget_atomic=settings.session_budget_atomic,
                    opened_at=opened,
                )
            )
            counts["sessions"] += 1

            batch = None
            if not still_open:
                batch = _add(
                    Batch(
                        batch_id=new_id("batch"),
                        session_id=pay_session.id,
                        channel_id=pay_session.channel_id,
                        network=network,
                        asset=asset,
                        asset_decimals=d,
                        pay_to=pay_to,
                        facilitator_fee_atomic=settings.facilitator_fee_atomic * 2,
                        status=BatchStatus.SETTLED,
                        opened_at=opened,
                    )
                )
                counts["batches"] += 1

            authorized = 0
            captured = 0
            declined = 0
            platform_total = 0
            author_total = 0
            last_at = opened

            for _ in range(call_count):
                tool = rng.choice(session_tools)
                author = next(a for a in authors.values() if a.id == tool.author_id)
                take_bps = (
                    author.platform_take_bps
                    if author.platform_take_bps is not None
                    else settings.platform_take_bps
                )
                created = opened + timedelta(seconds=rng.randint(1, 3_600))
                last_at = max(last_at, created)

                # ~6% of calls are refused by the Guardian. The funnel belongs in
                # the ledger: a dashboard that only shows wins is a brochure.
                if rng.random() < 0.06:
                    _add(
                        Call(
                            call_id=new_id("call"),
                            session_id=pay_session.id,
                            tool_id=tool.id,
                            payer=payer,
                            pay_to=author.pay_to,
                            network=network,
                            asset=asset,
                            scheme=Scheme(tool.scheme),
                            authorized_atomic=0,
                            captured_atomic=0,
                            platform_fee_atomic=0,
                            author_net_atomic=0,
                            status=CallStatus.DECLINED,
                            decline_reason=rng.choice(DECLINE_REASONS),
                            created_at=created,
                        )
                    )
                    counts["calls"] += 1
                    declined += 1
                    continue

                auth_atomic = (
                    tool.max_price_atomic
                    if (tool.scheme == Scheme.UPTO and tool.max_price_atomic)
                    else tool.price_atomic
                )
                if tool.scheme == Scheme.UPTO and tool.price_per_unit_atomic:
                    units = rng.randint(3, 60)
                    cap_atomic = min(
                        tool.price_atomic + units * tool.price_per_unit_atomic, auth_atomic
                    )
                else:
                    units = None
                    cap_atomic = tool.price_atomic

                platform_fee, author_net = split_take(cap_atomic, take_bps)
                settled = batch is not None

                _add(
                    Call(
                        call_id=new_id("call"),
                        session_id=pay_session.id,
                        tool_id=tool.id,
                        batch_id=batch.id if settled else None,
                        payer=payer,
                        pay_to=author.pay_to,
                        network=network,
                        asset=asset,
                        scheme=Scheme(tool.scheme),
                        authorized_atomic=auth_atomic,
                        captured_atomic=cap_atomic,
                        platform_fee_atomic=platform_fee,
                        author_net_atomic=author_net,
                        meter=tool.meter,
                        meter_units=units,
                        status=CallStatus.SETTLED if settled else CallStatus.CAPTURED,
                        # Distinct per row: UNIQUE(network, nonce) on `call` is a
                        # replay defence and the seeder must not trip it.
                        nonce="0x" + f"{rng.getrandbits(256):064x}",
                        verify_ms=rng.randint(40, 260),
                        execute_ms=rng.randint(8, 1_800),
                        created_at=created,
                        executed_at=created,
                        settled_at=created if settled else None,
                    )
                )
                counts["calls"] += 1

                authorized += auth_atomic
                captured += cap_atomic
                platform_total += platform_fee
                author_total += author_net
                revenue += cap_atomic

                tool.total_calls += 1
                tool.total_captured_atomic += cap_atomic
                db.add(tool)

            # -- receipts, written from the calls we just made --------------
            counts["receipts"] += _issue_receipts(db, pay_session, batch)

            # `Session.authorized_atomic` under `batch-settlement` is the latest
            # cumulative VOUCHER CEILING, not the sum of the per-call ceilings.
            # The client raises the ceiling by the amount actually charged, so
            # it tracks capture -- while each `Call.authorized_atomic` still
            # holds that call's own ceiling, which under `upto` is much larger.
            # Summing the per-call ceilings here would overstate what the payer
            # ever committed to, on a row the dashboard displays.
            pay_session.authorized_atomic = (
                captured if pay_session.scheme == Scheme.BATCH_SETTLEMENT else authorized
            )
            pay_session.captured_atomic = captured
            pay_session.settled_atomic = captured if batch else 0
            pay_session.call_count = counts_of_settled(db, pay_session.id)
            pay_session.declined_count = declined
            pay_session.last_call_at = last_at
            if batch is not None:
                pay_session.closed_at = last_at + timedelta(minutes=5)
                batch.call_count = pay_session.call_count
                batch.gross_atomic = captured
                batch.platform_fee_atomic = platform_total
                batch.author_net_atomic = author_total
                batch.closed_at = pay_session.closed_at
                batch.settled_at = pay_session.closed_at
                batch.claim_tx_hash = synthetic_tx_hash(batch.batch_id + ":claim")
                batch.settle_tx_hash = synthetic_tx_hash(batch.batch_id + ":settle")
                db.add(batch)
                db.flush()
                # Same call the live closer makes. It REBUILDS and REHASHES each
                # body rather than patching the row, because a receipt whose
                # stored hash no longer matches its stored body is
                # indistinguishable from a tampered one -- and `doctor` would
                # report our own bookkeeping as fraud.
                receipts_mod.attach_batch_settlement(db, batch)
            db.add(pay_session)

    out.section("seeded")
    for key, value in counts.items():
        out.kv(key, str(value), width=14)
    out.kv("demo revenue", format_atomic(revenue, d, symbol=sym), width=14, note="not real")
    return {**counts, "capturedAtomic": str(revenue)}


def counts_of_settled(db, session_pk: int) -> int:
    from sqlalchemy import func
    from sqlmodel import select

    from app.models import Call, CallStatus

    stmt = (
        select(func.count())
        .select_from(Call)
        .where(Call.session_id == session_pk, Call.status != CallStatus.DECLINED)
    )
    return int(db.exec(stmt).one() or 0)


def _issue_receipts(db, pay_session, batch) -> int:
    """One receipt per captured call, through the SAME code the runtime uses.

    `app.pay.receipts.issue()` is the gateway's own issuer. Writing a second one
    here would eventually drift -- two canonicalisations means two hashes, and a
    receipt that verifies in one path and fails in the other is worse than no
    receipt at all.

    Built from the Call ROWS rather than from the seeding loop's local variables,
    also on purpose: if the ledger and the receipts ever disagree, that is a bug
    worth catching, and `doctor` is what catches it. Generating both from the
    same in-memory values would guarantee agreement and prove nothing.

    `extra={"isDemo": True}` puts the flag INSIDE the hashed body. `issue()`
    mirrors it into the indexed `is_demo` column. No attestation is invented:
    there was no facilitator, so forging its signature would make a demo receipt
    look independently verifiable when it is not.
    """
    from sqlmodel import select

    from app.models import Call, CallStatus, Tool

    written = 0
    calls = db.exec(
        select(Call).where(Call.session_id == pay_session.id, Call.status != CallStatus.DECLINED)
    ).all()
    for call in calls:
        meter_reading = (
            {"unit": call.meter, "units": str(call.meter_units or 0)} if call.meter else None
        )
        receipts_mod.issue(
            db,
            call=call,
            session=pay_session,
            tool=db.get(Tool, call.tool_id),
            settlement=pay_session.settlement_mode,
            batch=batch,
            meter_reading=meter_reading,
            attestation=None,
            extra={"isDemo": True},
        )
        written += 1
    return written


def _demo_payer(label: str, index: int) -> str:
    """A deterministic, obviously-fake payer address.

    Derived from the agent label so the same agent keeps the same address across
    sessions (which is what makes a "top callers" view meaningful), and salted
    with the index so different agents never collide.

    The `0xDEC0DE` prefix is valid hex -- so `doctor`'s address-shape check
    passes and demo rows are not flagged as malformed data -- while still being
    unmistakable at a glance. An address that is obviously fake AND structurally
    valid is what a demo needs; one that is neither is a trap.
    """
    import hashlib

    digest = hashlib.sha256(f"eraya-demo-payer:{label}:{index // len(AGENTS)}".encode()).hexdigest()
    return "0xDEC0DE" + digest[:34]
