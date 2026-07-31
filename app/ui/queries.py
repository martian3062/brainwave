"""The dashboard's read layer. SQLModel over the ledger, nothing else.

Rules this module exists to enforce in one place rather than four:

* **Nothing is invented.** Every function returns what the ledger holds. When a
  table is empty the return value is empty -- never a sample, never a plausible
  default, never a "typical" figure. The pages turn an empty return into an
  explicit empty state.
* **Money stays integer.** Sums come back as `int` atomic units. The only float
  in the dashboard is produced by `theme.chart_value()` at the moment a number
  becomes a pixel.
* **Demo rows are separable at the row level.** Every ledger table carries
  `is_demo` (migration 0002). This module never writes its own predicate for it:
  it uses `app.demo.real_only()` / `demo_only()`, so "which reads exclude demo
  data" stays one grep. Row dataclasses carry `is_demo` through to the UI, which
  badges it -- a per-row flag is what the "mixed" case needs, because a real tool
  can perfectly well have demo calls against it.
* **A missing table is not a 500.** On a fresh Postgres where `alembic upgrade
  head` has not run yet, the dashboard renders its empty states instead of an
  exception page.

Aggregate figures are denominated in `settings.x402_asset_decimals`. The `call`
table stores `asset` but not `asset_decimals` (only `session`, `batch`, `receipt`
and `tool` do), so a deployment serving two assets with different scales would
need those sums split by asset. Single-asset today; the place to fix it is here.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, TypeVar

from sqlalchemy import func, or_
from sqlalchemy.exc import DatabaseError
from sqlmodel import Session as DBSession
from sqlmodel import col, select

from app.config import settings
from app.db import engine
from app.demo import DemoSummary, real_only
from app.demo import summary as demo_summary_of
from app.models import (
    Author,
    Batch,
    BatchStatus,
    Call,
    CallStatus,
    PaySession,
    Receipt,
    SessionStatus,
    Tool,
)

log = logging.getLogger("brainwave.ui.queries")

#: A call has a real, known capture amount from EXECUTED onwards. CHALLENGED (a
#: 402 was issued, nothing signed) and VERIFIED (signed, not yet run) have not
#: earned anything, and DECLINED/FAILED never will. Revenue figures use this set;
#: the funnel counts every status.
BILLABLE = (CallStatus.EXECUTED, CallStatus.CAPTURED, CallStatus.SETTLED)

T = TypeVar("T")


@contextmanager
def _read() -> Iterator[DBSession]:
    """A read-only session. No commit path -- the dashboard never writes."""
    with DBSession(engine) as db:
        yield db


def _safe(fn, default: T) -> T:
    """Return `default` when the ledger cannot be read (schema not migrated yet).

    Only database errors are swallowed. A bug in a query still raises, loudly.
    """
    try:
        return fn()
    except DatabaseError as exc:
        log.warning("ledger unreadable (%s) -- rendering empty state", exc.__class__.__name__)
        return default


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite has no tz-aware storage and hands values back naive. Postgres, the
    ledger of record, returns TIMESTAMPTZ. Normalise so bucketing is identical."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def demo_summary() -> DemoSummary:
    """`app.demo.summary()`, guarded. The dashboard's one demo-state question."""

    def run() -> DemoSummary:
        with _read() as db:
            return demo_summary_of(db)

    return _safe(
        run,
        DemoSummary(
            present=False,
            only_demo=False,
            mixed=False,
            counts={},
            real_counts={},
            demo_captured_atomic=0,
            real_captured_atomic=0,
        ),
    )


# --------------------------------------------------------------------------
# Result shapes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Totals:
    """Everything the overview's tiles need, in one pass."""

    calls_total: int = 0
    calls_billable: int = 0
    calls_declined: int = 0
    calls_failed: int = 0
    sessions_total: int = 0
    sessions_open: int = 0
    tools_enabled: int = 0
    authors: int = 0

    authorized_atomic: int = 0
    captured_atomic: int = 0
    platform_fee_atomic: int = 0
    author_net_atomic: int = 0

    batches_total: int = 0
    batches_settled: int = 0
    batch_gross_atomic: int = 0
    facilitator_fee_atomic: int = 0
    settled_calls: int = 0

    first_call_at: datetime | None = None
    last_call_at: datetime | None = None

    @property
    def has_revenue(self) -> bool:
        return self.captured_atomic > 0

    @property
    def has_any_calls(self) -> bool:
        return self.calls_total > 0


@dataclass(frozen=True)
class Bucket:
    day: date
    calls: int = 0
    captured_atomic: int = 0
    platform_fee_atomic: int = 0
    author_net_atomic: int = 0
    declined: int = 0


@dataclass(frozen=True)
class FeeLoad:
    """Realised settlement economics, straight from settled batches.

    `realised_bps` is what the ledger actually paid. `per_call_bps` is the
    counterfactual for the SAME calls at the SAME gross had each one settled on
    its own -- that is `fee * call_count / gross`, not a model.
    """

    batches: int
    calls: int
    gross_atomic: int
    facilitator_fee_atomic: int
    realised_bps: int
    per_call_fee_atomic: int
    per_call_bps: int

    @property
    def saved_atomic(self) -> int:
        return max(0, self.per_call_fee_atomic - self.facilitator_fee_atomic)


@dataclass(frozen=True)
class ToolRow:
    tool_id: int
    name: str
    author: str
    scheme: str
    network: str
    enabled: bool
    price_atomic: int
    max_price_atomic: int | None
    meter: str | None
    decimals: int
    is_demo: bool = False
    calls: int = 0
    declined: int = 0
    authorized_atomic: int = 0
    captured_atomic: int = 0
    platform_fee_atomic: int = 0
    author_net_atomic: int = 0
    #: Demo calls against this tool. Independent of `is_demo`: a real tool can
    #: carry seeded calls, which is exactly the case a banner alone would hide.
    demo_calls: int = 0
    counter_calls: int = 0
    counter_captured_atomic: int = 0

    @property
    def capture_bps(self) -> int:
        """Captured as basis points of authorized. Under `upto` this is the whole
        story: what the agent risked versus what it was actually charged."""
        if self.authorized_atomic <= 0:
            return 0
        return (self.captured_atomic * 10_000) // self.authorized_atomic

    @property
    def unused_atomic(self) -> int:
        return max(0, self.authorized_atomic - self.captured_atomic)

    @property
    def tainted(self) -> bool:
        """Any part of this row's revenue is seeded."""
        return self.is_demo or self.demo_calls > 0


@dataclass(frozen=True)
class ReceiptRow:
    receipt_id: str
    issued_at: datetime | None
    tool_name: str
    session_public_id: str
    batch_public_id: str | None
    scheme: str
    settlement: str
    network: str
    decimals: int
    authorized_atomic: int
    captured_atomic: int
    payer: str
    pay_to: str
    resource_url: str
    tx_hash: str | None
    explorer_url: str | None
    facilitator: str
    attestation: str | None
    body_hash: str
    body_json: str
    verify_status: str | None
    verified_at: datetime | None
    is_demo: bool


@dataclass(frozen=True)
class BatchPoint:
    batch_public_id: str
    call_count: int
    gross_atomic: int
    facilitator_fee_atomic: int
    decimals: int
    settled_at: datetime | None
    status: str
    settle_tx_hash: str | None
    claim_tx_hash: str | None
    is_demo: bool

    @property
    def fee_load_bps(self) -> int:
        if self.gross_atomic <= 0:
            return 0
        return (self.facilitator_fee_atomic * 10_000) // self.gross_atomic


@dataclass(frozen=True)
class FunnelStage:
    label: str
    status: str
    count: int


@dataclass(frozen=True)
class DeclineReason:
    reason: str
    count: int


@dataclass(frozen=True)
class SessionRow:
    session_public_id: str
    payer: str
    agent_label: str | None
    status: str
    settlement_mode: str
    calls: int
    authorized_atomic: int
    captured_atomic: int
    settled_atomic: int
    budget_atomic: int | None
    decimals: int
    opened_at: datetime | None
    is_demo: bool
    batches: int = 0
    facilitator_fee_atomic: int = 0


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------


def _int(v: Any) -> int:
    return int(v) if v is not None else 0


def totals(*, exclude_demo: bool = False) -> Totals:
    """One pass over the ledger for the overview tiles."""

    def run() -> Totals:
        with _read() as db:
            call_q = select(
                func.count(),
                func.sum(Call.authorized_atomic),
                func.sum(Call.captured_atomic),
                func.sum(Call.platform_fee_atomic),
                func.sum(Call.author_net_atomic),
                func.min(Call.created_at),
                func.max(Call.created_at),
            ).where(col(Call.status).in_(list(BILLABLE)))
            status_q = select(Call.status, func.count()).group_by(col(Call.status))
            sess_q = select(PaySession.status, func.count()).group_by(col(PaySession.status))
            batch_q = select(
                Batch.status,
                func.count(),
                func.sum(Batch.call_count),
                func.sum(Batch.gross_atomic),
                func.sum(Batch.facilitator_fee_atomic),
            ).group_by(col(Batch.status))
            tool_q = select(func.count()).select_from(Tool).where(col(Tool.enabled).is_(True))
            author_q = select(func.count()).select_from(Author)

            if exclude_demo:
                call_q = call_q.where(real_only(Call))
                status_q = status_q.where(real_only(Call))
                sess_q = sess_q.where(real_only(PaySession))
                batch_q = batch_q.where(real_only(Batch))
                tool_q = tool_q.where(real_only(Tool))
                author_q = author_q.where(real_only(Author))

            billable, authorized, captured, platform, author_net, first_at, last_at = db.exec(
                call_q
            ).one()
            by_status = {str(s): _int(n) for s, n in db.exec(status_q).all()}
            by_sess_status = {str(s): _int(n) for s, n in db.exec(sess_q).all()}
            batch_rows = db.exec(batch_q).all()
            tools_enabled = _int(db.exec(tool_q).one())
            authors = _int(db.exec(author_q).one())

        batches_total = sum(_int(r[1]) for r in batch_rows)
        settled = [r for r in batch_rows if str(r[0]) == BatchStatus.SETTLED]

        return Totals(
            calls_total=sum(by_status.values()),
            calls_billable=_int(billable),
            calls_declined=by_status.get(CallStatus.DECLINED, 0),
            calls_failed=by_status.get(CallStatus.FAILED, 0),
            sessions_total=sum(by_sess_status.values()),
            sessions_open=by_sess_status.get(SessionStatus.OPEN, 0),
            tools_enabled=tools_enabled,
            authors=authors,
            authorized_atomic=_int(authorized),
            captured_atomic=_int(captured),
            platform_fee_atomic=_int(platform),
            author_net_atomic=_int(author_net),
            batches_total=batches_total,
            batches_settled=sum(_int(r[1]) for r in settled),
            batch_gross_atomic=sum(_int(r[3]) for r in settled),
            facilitator_fee_atomic=sum(_int(r[4]) for r in settled),
            settled_calls=sum(_int(r[2]) for r in settled),
            first_call_at=_aware(first_at),
            last_call_at=_aware(last_at),
        )

    return _safe(run, Totals())


def daily_buckets(days: int = 14, *, exclude_demo: bool = False) -> list[Bucket]:
    """Per-day revenue and volume, oldest first.

    Bucketed in Python rather than in SQL on purpose: `date_trunc` is Postgres,
    `strftime` is SQLite, and a dashboard that quietly renders different buckets
    on the local fallback than on the ledger of record is worse than a slow one.

    Days with no calls are emitted as zeros -- that is not fabrication, a quiet
    day earned nothing. The window starts at the first recorded call, so a ledger
    two hours old does not draw a fortnight of flat zero it was never live for.
    """

    def run() -> list[Bucket]:
        cutoff = datetime.now(UTC) - timedelta(days=days - 1)
        cutoff = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)

        with _read() as db:
            q = select(
                Call.created_at,
                Call.status,
                Call.captured_atomic,
                Call.platform_fee_atomic,
                Call.author_net_atomic,
            ).where(col(Call.created_at) >= cutoff)
            if exclude_demo:
                q = q.where(real_only(Call))
            rows = db.exec(q).all()

        if not rows:
            return []

        billable = {str(s) for s in BILLABLE}
        acc: dict[date, dict[str, int]] = {}
        for created, status, captured, platform, net in rows:
            when = _aware(created)
            if when is None:
                continue
            slot = acc.setdefault(
                when.date(),
                {"calls": 0, "captured": 0, "platform": 0, "net": 0, "declined": 0},
            )
            if str(status) == CallStatus.DECLINED:
                slot["declined"] += 1
            elif str(status) in billable:
                slot["calls"] += 1
                slot["captured"] += _int(captured)
                slot["platform"] += _int(platform)
                slot["net"] += _int(net)

        start = min(acc)
        end = max(datetime.now(UTC).date(), max(acc))
        out: list[Bucket] = []
        cursor = start
        while cursor <= end:
            s = acc.get(cursor)
            out.append(
                Bucket(
                    day=cursor,
                    calls=s["calls"] if s else 0,
                    captured_atomic=s["captured"] if s else 0,
                    platform_fee_atomic=s["platform"] if s else 0,
                    author_net_atomic=s["net"] if s else 0,
                    declined=s["declined"] if s else 0,
                )
            )
            cursor += timedelta(days=1)
        return out

    return _safe(run, [])


def realised_fee_load(*, exclude_demo: bool = False) -> FeeLoad | None:
    """What batching actually cost, from settled batches only. None = none yet.

    This is the ledger's own answer to the submission's central claim. It is not
    modelled and not extrapolated: `facilitator_fee_atomic` is what the batches
    were charged, `gross_atomic` is what they moved, and the counterfactual
    multiplies the configured per-settlement fee by the number of calls those
    same batches covered.
    """

    def run() -> FeeLoad | None:
        with _read() as db:
            q = select(
                func.count(),
                func.sum(Batch.call_count),
                func.sum(Batch.gross_atomic),
                func.sum(Batch.facilitator_fee_atomic),
            ).where(col(Batch.status) == BatchStatus.SETTLED)
            if exclude_demo:
                q = q.where(real_only(Batch))
            n, calls, gross, fee = db.exec(q).one()

        n, calls, gross, fee = _int(n), _int(calls), _int(gross), _int(fee)
        if n == 0 or gross <= 0:
            return None

        per_call_fee = settings.facilitator_fee_atomic * calls
        return FeeLoad(
            batches=n,
            calls=calls,
            gross_atomic=gross,
            facilitator_fee_atomic=fee,
            realised_bps=(fee * 10_000) // gross,
            per_call_fee_atomic=per_call_fee,
            per_call_bps=(per_call_fee * 10_000) // gross,
        )

    return _safe(run, None)


def funnel(*, exclude_demo: bool = False) -> tuple[list[FunnelStage], list[DeclineReason]]:
    """Call outcomes in protocol order, plus why the declines declined.

    The stages are ORDINAL -- challenged precedes verified precedes executed --
    so they take the one-hue ramp, not categorical slots.
    """

    order = [
        (CallStatus.CHALLENGED, "402 issued"),
        (CallStatus.VERIFIED, "authorization verified"),
        (CallStatus.EXECUTED, "tool executed"),
        (CallStatus.CAPTURED, "captured"),
        (CallStatus.SETTLED, "settled on-chain"),
    ]

    def run() -> tuple[list[FunnelStage], list[DeclineReason]]:
        with _read() as db:
            q = select(Call.status, func.count()).group_by(col(Call.status))
            r = (
                select(Call.decline_reason, func.count())
                .where(col(Call.status) == CallStatus.DECLINED)
                .group_by(col(Call.decline_reason))
            )
            if exclude_demo:
                q = q.where(real_only(Call))
                r = r.where(real_only(Call))
            counts = {str(s): _int(n) for s, n in db.exec(q).all()}
            reasons = [
                DeclineReason(reason=str(reason or "unspecified"), count=_int(n))
                for reason, n in db.exec(r).all()
            ]

        if not counts:
            return [], []
        stages = [
            FunnelStage(label=label, status=str(s), count=counts.get(str(s), 0))
            for s, label in order
        ]
        reasons.sort(key=lambda x: -x.count)
        return stages, reasons

    return _safe(run, ([], []))


def tool_rows(*, exclude_demo: bool = False) -> list[ToolRow]:
    """Per-tool economics, aggregated from `call` rather than from the
    denormalised counters on `tool`.

    `Tool.total_calls` / `Tool.total_captured_atomic` are a cache the metering
    layer maintains; the `call` rows are the ledger. Both are returned so the
    tools page can show a drift warning if they ever disagree.
    """

    def run() -> list[ToolRow]:
        with _read() as db:
            cat_q = select(Tool, Author.display_name).join(
                Author, col(Tool.author_id) == col(Author.id), isouter=True
            )
            if exclude_demo:
                cat_q = cat_q.where(real_only(Tool))
            catalogue = db.exec(cat_q).all()
            if not catalogue:
                return []

            agg_q = (
                select(
                    Call.tool_id,
                    func.count(),
                    func.sum(Call.authorized_atomic),
                    func.sum(Call.captured_atomic),
                    func.sum(Call.platform_fee_atomic),
                    func.sum(Call.author_net_atomic),
                )
                .where(col(Call.status).in_(list(BILLABLE)))
                .group_by(col(Call.tool_id))
            )
            dec_q = (
                select(Call.tool_id, func.count())
                .where(col(Call.status) == CallStatus.DECLINED)
                .group_by(col(Call.tool_id))
            )
            if exclude_demo:
                agg_q = agg_q.where(real_only(Call))
                dec_q = dec_q.where(real_only(Call))
            agg = {int(r[0]): r for r in db.exec(agg_q).all() if r[0] is not None}
            declines = {int(t): _int(n) for t, n in db.exec(dec_q).all() if t is not None}

            demo_q = (
                select(Call.tool_id, func.count())
                .where(col(Call.is_demo).is_(True))
                .group_by(col(Call.tool_id))
            )
            demo_calls = {int(t): _int(n) for t, n in db.exec(demo_q).all() if t is not None}

        out: list[ToolRow] = []
        for tool, author_name in catalogue:
            key = int(tool.id or 0)
            a = agg.get(key)
            out.append(
                ToolRow(
                    tool_id=key,
                    name=tool.name,
                    author=author_name or "unassigned",
                    scheme=str(tool.scheme),
                    network=tool.network,
                    enabled=bool(tool.enabled),
                    price_atomic=int(tool.price_atomic),
                    max_price_atomic=(
                        int(tool.max_price_atomic) if tool.max_price_atomic is not None else None
                    ),
                    meter=tool.meter,
                    decimals=int(tool.asset_decimals),
                    is_demo=bool(tool.is_demo),
                    calls=_int(a[1]) if a else 0,
                    declined=declines.get(key, 0),
                    authorized_atomic=_int(a[2]) if a else 0,
                    captured_atomic=_int(a[3]) if a else 0,
                    platform_fee_atomic=_int(a[4]) if a else 0,
                    author_net_atomic=_int(a[5]) if a else 0,
                    demo_calls=0 if exclude_demo else demo_calls.get(key, 0),
                    counter_calls=int(tool.total_calls),
                    counter_captured_atomic=int(tool.total_captured_atomic),
                )
            )
        out.sort(key=lambda t: (-t.captured_atomic, -t.calls, t.name))
        return out

    return _safe(run, [])


def receipt_rows(
    *,
    session_public_id: str | None = None,
    batch_public_id: str | None = None,
    settled_only: bool = False,
    search: str | None = None,
    exclude_demo: bool = False,
    limit: int = 500,
) -> list[ReceiptRow]:
    """Receipts joined to the tool that earned them and the batch that paid.

    `Receipt.session_id` and `Receipt.batch_id` are integer FKs; the strings an
    author actually quotes are `pay_session.session_id` and `batch.batch_id`,
    which is why this join exists at all.
    """

    def run() -> list[ReceiptRow]:
        with _read() as db:
            q = (
                select(Receipt, PaySession.session_id, Tool.name, Batch.batch_id)
                .join(PaySession, col(Receipt.session_id) == col(PaySession.id))
                .join(Call, col(Receipt.call_id) == col(Call.id))
                .join(Tool, col(Call.tool_id) == col(Tool.id), isouter=True)
                .join(Batch, col(Receipt.batch_id) == col(Batch.id), isouter=True)
                .order_by(col(Receipt.issued_at).desc())
                .limit(limit)
            )
            if session_public_id:
                q = q.where(col(PaySession.session_id) == session_public_id)
            if batch_public_id:
                q = q.where(col(Batch.batch_id) == batch_public_id)
            if settled_only:
                q = q.where(col(Receipt.tx_hash).is_not(None))
            if exclude_demo:
                q = q.where(real_only(Receipt))
            if search:
                like = f"%{search.strip()}%"
                q = q.where(
                    or_(
                        col(Receipt.receipt_id).ilike(like),
                        col(Receipt.tx_hash).ilike(like),
                        col(Receipt.payer).ilike(like),
                        col(Receipt.resource_url).ilike(like),
                    )
                )
            rows = db.exec(q).all()

        return [
            ReceiptRow(
                receipt_id=receipt.receipt_id,
                issued_at=_aware(receipt.issued_at),
                tool_name=tool_name or "--",
                session_public_id=sess_pub,
                batch_public_id=batch_pub,
                scheme=str(receipt.scheme),
                settlement=str(receipt.settlement),
                network=receipt.network,
                decimals=int(receipt.asset_decimals),
                authorized_atomic=int(receipt.authorized_atomic),
                captured_atomic=int(receipt.captured_atomic),
                payer=receipt.payer,
                pay_to=receipt.pay_to,
                resource_url=receipt.resource_url,
                tx_hash=receipt.tx_hash,
                # Prefer the URL stored with the receipt; fall back to the
                # configured explorer for this network.
                explorer_url=receipt.explorer_url or settings.explorer_url(receipt.tx_hash),
                facilitator=receipt.facilitator,
                attestation=receipt.attestation,
                body_hash=receipt.body_hash,
                body_json=receipt.body_json,
                verify_status=receipt.verify_status,
                verified_at=_aware(receipt.verified_at),
                is_demo=bool(receipt.is_demo),
            )
            for receipt, sess_pub, tool_name, batch_pub in rows
        ]

    return _safe(run, [])


def receipt_filter_options() -> tuple[list[str], list[str]]:
    """Session and batch public ids that actually have receipts. Nothing else is
    offered as a filter -- an option that returns nothing is a bug report."""

    def run() -> tuple[list[str], list[str]]:
        with _read() as db:
            sessions = db.exec(
                select(PaySession.session_id)
                .join(Receipt, col(Receipt.session_id) == col(PaySession.id))
                .distinct()
                .order_by(col(PaySession.session_id))
            ).all()
            batches = db.exec(
                select(Batch.batch_id)
                .join(Receipt, col(Receipt.batch_id) == col(Batch.id))
                .distinct()
                .order_by(col(Batch.batch_id))
            ).all()
        return [str(s) for s in sessions], [str(b) for b in batches]

    return _safe(run, ([], []))


def batch_points(*, exclude_demo: bool = False, limit: int = 400) -> list[BatchPoint]:
    """Settled batches as (calls-per-batch, realised fee load) observations.

    These are the real points the economics page plots against the model curve.
    Only SETTLED batches with a positive gross qualify: an open batch has not
    been charged a facilitator fee yet, so its fee load is not a fact.
    """

    def run() -> list[BatchPoint]:
        with _read() as db:
            q = (
                select(Batch)
                .where(col(Batch.status) == BatchStatus.SETTLED)
                .where(col(Batch.gross_atomic) > 0)
                .order_by(col(Batch.settled_at).desc())
                .limit(limit)
            )
            if exclude_demo:
                q = q.where(real_only(Batch))
            rows = db.exec(q).all()

        return [
            BatchPoint(
                batch_public_id=b.batch_id,
                call_count=int(b.call_count),
                gross_atomic=int(b.gross_atomic),
                facilitator_fee_atomic=int(b.facilitator_fee_atomic),
                decimals=int(b.asset_decimals),
                settled_at=_aware(b.settled_at),
                status=str(b.status),
                settle_tx_hash=b.settle_tx_hash,
                claim_tx_hash=b.claim_tx_hash,
                is_demo=bool(b.is_demo),
            )
            for b in rows
        ]

    return _safe(run, [])


def session_rows(*, exclude_demo: bool = False, limit: int = 100) -> list[SessionRow]:
    """Recent payment sessions, most recently opened first."""

    def run() -> list[SessionRow]:
        with _read() as db:
            q = select(PaySession).order_by(col(PaySession.opened_at).desc()).limit(limit)
            if exclude_demo:
                q = q.where(real_only(PaySession))
            sessions = db.exec(q).all()
            if not sessions:
                return []
            ids = [int(s.id or 0) for s in sessions]
            fees = {
                int(sid): (_int(n), _int(fee))
                for sid, n, fee in db.exec(
                    select(
                        Batch.session_id,
                        func.count(),
                        func.sum(Batch.facilitator_fee_atomic),
                    )
                    .where(col(Batch.session_id).in_(ids))
                    .where(col(Batch.status) == BatchStatus.SETTLED)
                    .group_by(col(Batch.session_id))
                ).all()
            }

        return [
            SessionRow(
                session_public_id=s.session_id,
                payer=s.payer,
                agent_label=s.agent_label,
                status=str(s.status),
                settlement_mode=str(s.settlement_mode),
                calls=int(s.call_count),
                authorized_atomic=int(s.authorized_atomic),
                captured_atomic=int(s.captured_atomic),
                settled_atomic=int(s.settled_atomic),
                budget_atomic=(int(s.budget_atomic) if s.budget_atomic is not None else None),
                decimals=int(s.asset_decimals),
                opened_at=_aware(s.opened_at),
                is_demo=bool(s.is_demo),
                batches=fees.get(int(s.id or 0), (0, 0))[0],
                facilitator_fee_atomic=fees.get(int(s.id or 0), (0, 0))[1],
            )
            for s in sessions
        ]

    return _safe(run, [])


def average_captured_atomic(*, exclude_demo: bool = False) -> int | None:
    """Mean capture per billable call, exact integer division. None if no calls.

    The economics page offers this as a price input so the model can be run at
    the catalogue's ACTUAL average price instead of only at the spec's $0.002.
    """

    def run() -> int | None:
        with _read() as db:
            q = select(func.count(), func.sum(Call.captured_atomic)).where(
                col(Call.status).in_(list(BILLABLE))
            )
            if exclude_demo:
                q = q.where(real_only(Call))
            n, total = db.exec(q).one()
        n, total = _int(n), _int(total)
        return (total // n) if n and total else None

    return _safe(run, None)
