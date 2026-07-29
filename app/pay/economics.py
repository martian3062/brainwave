"""The argument, as arithmetic.

    Per-call settlement at $0.002 with a $0.001 facilitator fee burns 50% of revenue.
    N authorizations settled once drives the fee load to ~0.5%.

Every number the README quotes is produced by a function in this module, and every
one of them is computed from integer atomic units. `fee_load_bps` returns basis
points -- an integer -- rather than a percentage float, because "50%" is a
rendering of 5000 bps and not the other way round.

WHAT IS AND IS NOT BEING CLAIMED
--------------------------------
The saving is real but it is not free, and the honest version of the claim has
three parts:

1. It is the FACILITATOR fee that amortises, not the gas. In the SDK's
   `batch-settlement` scheme the payer's per-call authorization is a signed
   voucher raising a cumulative ceiling -- no transaction, no fee. Only the
   channel manager's `claim` + `settle` touch the chain. Verified by reading
   `x402/mechanisms/evm/batch_settlement/server/settle.py::handle_before_settle`,
   which returns `SkipSettleResult` with `transaction=""` for every voucher
   payload.
2. The cost of batching is counterparty risk and float. Between capture and
   settlement the seller holds a claim, not money. `Session.captured_atomic -
   Session.settled_atomic` is exactly that exposure and the dashboard shows it.
3. `claim` and `settle` are two on-chain steps, so a batch costs at least two
   transactions, not one. `settlement_cost` below counts both. Quoting a
   one-transaction batch would flatter the number by 2x.

The comparison functions take the per-call baseline as a *hypothetical*: those
transactions were never sent. `realised_fee_load_bps` reads what actually
happened from the ledger, and it is the only figure that should ever be presented
as measured.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.money import PriceError, fee_load_bps, split_take

__all__ = [
    "BPS",
    "split",
    "settlement_cost",
    "SettlementComparison",
    "compare_settlement",
    "break_even_calls",
    "realised_fee_load_bps",
    "batch_conservation",
    "session_report",
    "economics_table",
]

#: One hundred percent, in basis points. 1 bps = 0.01%.
BPS = 10_000

#: `batch-settlement` reaches the chain twice per batch: `claim` submits the
#: cumulative vouchers, `settle` sweeps the claimed funds to the receiver. See
#: `BatchSettlementChannelManager.claim_and_settle`.
TX_PER_BATCH = 2


def split(gross_atomic: int, take_bps: int) -> tuple[int, int]:
    """`(platform_fee, author_net)`, summing exactly to `gross_atomic`.

    Thin alias for `app.money.split_take`, re-exported so revenue accounting reads
    from one module. The platform's cut is floored and the remainder goes to the
    author, so conservation holds for every input including gross=1.
    """
    return split_take(gross_atomic, take_bps)


def settlement_cost(
    settlements: int,
    fee_atomic: int,
    *,
    tx_per_settlement: int = TX_PER_BATCH,
    free_tx_remaining: int = 0,
) -> int:
    """Total facilitator cost of `settlements` settlements, in atomic units.

    `free_tx_remaining` models the hosted facilitator's free tier (CDP: the first
    1,000 transactions a month). It is applied to transactions, not settlements,
    because that is what the tier actually counts.
    """
    if settlements < 0 or fee_atomic < 0 or tx_per_settlement < 0 or free_tx_remaining < 0:
        raise PriceError("settlement_cost takes non-negative integers only")
    transactions = settlements * tx_per_settlement
    billable = max(0, transactions - free_tx_remaining)
    return billable * fee_atomic


@dataclass(frozen=True)
class SettlementComparison:
    """Per-call settlement against batched settlement, over the same revenue."""

    calls: int
    price_atomic: int
    gross_atomic: int
    fee_atomic: int

    per_call_settlements: int
    per_call_fee_atomic: int
    per_call_load_bps: int

    batched_settlements: int
    batched_fee_atomic: int
    batched_load_bps: int

    #: What batching kept, atomic units. Never negative: batching one call is the
    #: same as not batching it, plus the second transaction of the claim/settle
    #: pair -- so at tiny N the honest answer is that batching costs MORE, and
    #: this field will say so by being zero while `batched_fee_atomic` is higher.
    saved_atomic: int
    #: Improvement expressed in basis points of the per-call load, so no float is
    #: needed to say "100x better": 100x is 10_000 bps.
    improvement_bps: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "grossAtomic": str(self.gross_atomic),
            "perCall": {
                "settlements": self.per_call_settlements,
                "feeAtomic": str(self.per_call_fee_atomic),
                "loadBps": self.per_call_load_bps,
            },
            "batched": {
                "settlements": self.batched_settlements,
                "feeAtomic": str(self.batched_fee_atomic),
                "loadBps": self.batched_load_bps,
            },
            "savedAtomic": str(self.saved_atomic),
            "improvementBps": self.improvement_bps,
        }


def compare_settlement(
    calls: int,
    price_atomic: int,
    fee_atomic: int,
    *,
    batch_size: int | None = None,
    tx_per_settlement: int = TX_PER_BATCH,
) -> SettlementComparison:
    """The headline table, for one price and one call count.

    `batch_size=None` means the whole run settles once. Per-call settlement is
    counted as ONE transaction per call, because `exact` settles in a single
    `transferWithAuthorization`; batching is counted as `tx_per_settlement` per
    batch. Using the same transaction count for both would overstate the saving.
    """
    if calls < 0 or price_atomic < 0 or fee_atomic < 0:
        raise PriceError("compare_settlement takes non-negative integers only")

    gross = calls * price_atomic
    per_call_settlements = calls
    per_call_fee = settlement_cost(per_call_settlements, fee_atomic, tx_per_settlement=1)

    size = batch_size if batch_size else max(calls, 1)
    batched_settlements = -(-calls // size) if calls else 0  # integer ceiling
    batched_fee = settlement_cost(
        batched_settlements, fee_atomic, tx_per_settlement=tx_per_settlement
    )

    per_call_load = fee_load_bps(per_call_fee, gross)
    batched_load = fee_load_bps(batched_fee, gross)

    improvement = (per_call_load * BPS // batched_load) if batched_load > 0 else 0

    return SettlementComparison(
        calls=calls,
        price_atomic=price_atomic,
        gross_atomic=gross,
        fee_atomic=fee_atomic,
        per_call_settlements=per_call_settlements,
        per_call_fee_atomic=per_call_fee,
        per_call_load_bps=per_call_load,
        batched_settlements=batched_settlements,
        batched_fee_atomic=batched_fee,
        batched_load_bps=batched_load,
        saved_atomic=max(0, per_call_fee - batched_fee),
        improvement_bps=improvement,
    )


def break_even_calls(
    price_atomic: int,
    fee_atomic: int,
    target_bps: int,
    *,
    tx_per_settlement: int = TX_PER_BATCH,
) -> int:
    """Smallest batch whose fee load is at or under `target_bps`.

    Solves `tx * fee / (n * price) <= target/10000` for integer n:

        n >= tx * fee * 10000 / (price * target)

    Answers the operator's real question -- "how many calls before a session is
    worth settling?" -- and is what `BATCH_MIN_GROSS_PRICE` should be set from.
    """
    if price_atomic <= 0:
        raise PriceError("break_even_calls needs a positive price")
    if target_bps <= 0:
        raise PriceError("break_even_calls needs a positive target in bps")
    if fee_atomic == 0:
        return 1
    numerator = tx_per_settlement * fee_atomic * BPS
    denominator = price_atomic * target_bps
    return max(1, -(-numerator // denominator))


def realised_fee_load_bps(batches: list[Any]) -> int:
    """Fee load actually incurred, read off settled `Batch` rows.

    The only figure that may be presented as measured rather than modelled. Rows
    that never settled are excluded -- a batch whose claim failed cost nothing and
    earned nothing, and counting it either way would be a lie in a different
    direction.
    """
    gross = 0
    fees = 0
    for batch in batches:
        if not getattr(batch, "settle_tx_hash", None):
            continue
        gross += int(batch.gross_atomic)
        fees += int(batch.facilitator_fee_atomic)
    return fee_load_bps(fees, gross)


def batch_conservation(batch: Any, calls: list[Any]) -> tuple[bool, str]:
    """The audit: does this batch's gross equal the sum of the calls inside it?

    `(ok, explanation)`. Rounding cannot hide here because there is no rounding --
    both sides are integer atomic units, so the check is `==`, not `abs(a-b) < eps`.
    The split is checked too: platform + author must reconstruct gross exactly.
    """
    summed = sum(int(c.captured_atomic) for c in calls)
    gross = int(batch.gross_atomic)
    if summed != gross:
        return False, (
            f"batch {batch.batch_id}: gross {gross} != sum(captured) {summed} "
            f"over {len(calls)} calls"
        )
    parts = int(batch.platform_fee_atomic) + int(batch.author_net_atomic)
    if parts != gross:
        return False, (f"batch {batch.batch_id}: platform+author {parts} != gross {gross}")
    if int(batch.call_count) != len(calls):
        return False, (
            f"batch {batch.batch_id}: call_count {batch.call_count} != {len(calls)} rows"
        )
    return True, f"batch {batch.batch_id}: {len(calls)} calls conserve to {gross} atomic units"


def session_report(session: Any, calls: list[Any], batches: list[Any]) -> dict[str, Any]:
    """Everything the dashboard shows about one session's economics.

    Deliberately includes `unsettledAtomic`. Batching's cost is that the seller
    carries a claim instead of money between capture and settlement, and a
    dashboard that only ever showed the saving would be marketing.
    """
    captured = sum(int(c.captured_atomic) for c in calls)
    authorized = sum(int(c.authorized_atomic) for c in calls)
    settled = sum(int(b.gross_atomic) for b in batches if getattr(b, "settle_tx_hash", None))
    fees = sum(int(b.facilitator_fee_atomic) for b in batches if getattr(b, "settle_tx_hash", None))
    platform = sum(int(c.platform_fee_atomic) for c in calls)
    author = sum(int(c.author_net_atomic) for c in calls)

    return {
        "sessionId": session.session_id,
        "payer": session.payer,
        "settlementMode": str(session.settlement_mode),
        "calls": len(calls),
        "declined": int(session.declined_count),
        "authorizedAtomic": str(authorized),
        "capturedAtomic": str(captured),
        "settledAtomic": str(settled),
        # Capture minus settlement: the float the seller is carrying right now.
        "unsettledAtomic": str(captured - settled),
        "platformFeeAtomic": str(platform),
        "authorNetAtomic": str(author),
        "facilitatorFeeAtomic": str(fees),
        "settlements": len([b for b in batches if getattr(b, "settle_tx_hash", None)]),
        "realisedLoadBps": fee_load_bps(fees, settled),
        # The counterfactual, clearly labelled as one.
        "hypotheticalPerCallLoadBps": (
            fee_load_bps(len(calls) * _fee_of(batches), captured) if calls else 0
        ),
    }


def _fee_of(batches: list[Any]) -> int:
    """Per-settlement fee observed on this session's batches, for the counterfactual.

    Taken from real batches rather than from config so the comparison uses the fee
    that was actually charged. Falls back to 0 when nothing settled, which makes
    the counterfactual 0 rather than inventing a number.
    """
    settled = [b for b in batches if getattr(b, "settle_tx_hash", None)]
    if not settled:
        return 0
    return sum(int(b.facilitator_fee_atomic) for b in settled) // (len(settled) * TX_PER_BATCH)


def economics_table(
    prices_atomic: list[int],
    batch_sizes: list[int],
    fee_atomic: int,
) -> list[dict[str, Any]]:
    """Rows for the README's economics table. Pure arithmetic, no ledger, no chain."""
    rows: list[dict[str, Any]] = []
    for price in prices_atomic:
        for size in batch_sizes:
            rows.append(compare_settlement(size, price, fee_atomic, batch_size=size).as_dict())
    return rows
