"""Money and economics. If any of these fail, the ledger is not evidence.

.venv/Scripts/python -m pytest tests/test_pay_money.py -q
"""

from __future__ import annotations

import ast
import os
import pathlib

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("PUBLIC_BASE_URL", "http://testserver")

from app.models import Scheme  # noqa: E402
from app.money import PriceError  # noqa: E402
from app.pay import economics  # noqa: E402
from app.pay.pricing import (  # noqa: E402
    PriceSpec,
    atomic,
    from_wire,
    payment_requirements,
    wire_amount,
)

PAY_DIR = pathlib.Path(__file__).resolve().parent.parent / "app" / "pay"


# ------------------------------------------------------------------ parsing --


def test_strict_parser_matches_the_spine_on_everything_it_accepts():
    assert atomic("$0.002", 6) == 2_000
    assert atomic("0.002 USDC", 6) == 2_000
    assert atomic("$1.00", 6) == 1_000_000
    assert atomic(2_000, 6) == 2_000


def test_the_strict_parser_refuses_floats():
    """The reason `app.pay.pricing.atomic` exists at all.

    `app.money.parse_price` stringifies whatever it is given, so `0.002` parses to
    2000 and a float has crossed a money boundary unnoticed -- while `0.1 + 0.2`
    raises, because `str()` of it is '0.30000000000000004'. Silent for clean
    literals, loud for dirty ones, which is the worst possible combination. This
    test pins BOTH halves: the hole in the permissive parser, and the fact that
    nothing in `app.pay` can fall into it.
    """
    from app.money import parse_price

    assert parse_price(0.002, 6) == 2_000  # the hole, documented
    with pytest.raises(PriceError):
        atomic(0.002, 6)  # closed, everywhere in app.pay
    with pytest.raises(PriceError):
        atomic(0.1 + 0.2, 6)


def test_sub_atomic_precision_is_an_error():
    with pytest.raises(PriceError):
        atomic("$0.0000001", 6)


def test_wire_round_trip_is_lossless():
    for value in (0, 1, 2_000, 500_000, 10**15):
        assert from_wire(wire_amount(value)) == value
    with pytest.raises(PriceError):
        wire_amount(-1)
    with pytest.raises(PriceError):
        wire_amount(True)  # bool is an int subclass; refuse it explicitly


# -------------------------------------------------------------- price specs --


def _spec(**kw):
    base = dict(
        price="$0.002",
        network="eip155:84532",
        asset="0x" + "22" * 20,
        pay_to="0x" + "de" * 20,
    )
    base.update(kw)
    return PriceSpec.declare(kw.pop("name", "t"), **{k: v for k, v in base.items()})


def test_exact_authorizes_exactly_the_price():
    spec = _spec()
    assert spec.price_atomic == 2_000
    assert spec.authorized_atomic == 2_000
    assert payment_requirements(spec).amount == "2000"


def test_upto_authorizes_the_ceiling_and_advertises_it():
    spec = _spec(
        price="$0.05", scheme="upto", max_price="$0.50", meter="seconds", price_per_unit="$0.01"
    )
    assert spec.price_atomic == 50_000
    assert spec.max_price_atomic == 500_000
    # The agent signs for the CEILING. Capture happens later and is always lower.
    assert spec.authorized_atomic == 500_000
    assert payment_requirements(spec).amount == "500000"
    assert payment_requirements(spec, amount_atomic=89_000).amount == "89000"


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(scheme="upto"),  # ceiling missing
        dict(scheme="upto", max_price="$0.50"),  # meter missing
        dict(scheme="upto", max_price="$0.001", meter="calls", price_per_unit="$0.001"),
        dict(max_price="$0.50"),  # max_price on a non-upto scheme
        dict(meter="calls"),  # meter without a per-unit price
        dict(price_per_unit="$0.001"),  # per-unit price without a meter
        dict(meter="nonsense", price_per_unit="$0.001"),
    ],
)
def test_incoherent_price_declarations_are_refused_at_import(kwargs):
    """Every one of these is a repricing typo that would silently mis-bill."""
    with pytest.raises(PriceError):
        spec = _spec(**kwargs)
        # `nonsense` is only rejected when the meter is resolved.
        from app.pay.meters import get_meter

        get_meter(spec.meter_name)


# -------------------------------------------------------------- revenue split --


@pytest.mark.parametrize("gross", [0, 1, 3, 7, 999, 2_000, 89_000, 10**12 + 7])
@pytest.mark.parametrize("bps", [0, 1, 250, 1_000, 3_333, 9_999, 10_000])
def test_split_conserves_exactly_for_every_input(gross, bps):
    platform, author = economics.split(gross, bps)
    assert platform + author == gross
    assert platform >= 0 and author >= 0
    assert isinstance(platform, int) and isinstance(author, int)


# ----------------------------------------------------------------- economics --


def test_the_headline_claim_survives_counting_both_transactions():
    """$0.002 a call, $0.001 a settlement.

    Per-call: one transaction per call, fee 1000 against gross 2000 -> 5000 bps.
    Batched: a batch is claim + settle, so TWO transactions -- 2000 against a
    gross of 200000 over 100 calls -> 100 bps.

    Note this is deliberately worse than the "0.5%" a one-transaction batch would
    show. 1% of revenue, not 50%, is still the argument, and it is the honest
    version of it.
    """
    price, fee = 2_000, 1_000
    result = economics.compare_settlement(100, price, fee, batch_size=100)
    assert result.gross_atomic == 200_000
    assert result.per_call_settlements == 100
    assert result.per_call_fee_atomic == 100_000
    assert result.per_call_load_bps == 5_000  # 50.00%
    assert result.batched_settlements == 1
    assert result.batched_fee_atomic == 2_000  # claim + settle
    assert result.batched_load_bps == 100  # 1.00%
    assert result.saved_atomic == 98_000
    assert result.improvement_bps == 500_000  # 50x, in bps


def test_batching_a_single_call_is_honestly_reported_as_worse():
    """Two transactions to settle one call costs twice as much as one.

    A comparison function that could never say "this made it worse" would not be
    a comparison function.
    """
    result = economics.compare_settlement(1, 2_000, 1_000, batch_size=1)
    assert result.batched_fee_atomic > result.per_call_fee_atomic
    assert result.saved_atomic == 0


def test_break_even_answers_the_operators_actual_question():
    # How many $0.002 calls before a $0.001-per-tx settlement costs under 1%?
    n = economics.break_even_calls(2_000, 1_000, 100)
    assert n == 100
    assert economics.compare_settlement(n, 2_000, 1_000, batch_size=n).batched_load_bps <= 100
    smaller = economics.compare_settlement(n - 1, 2_000, 1_000, batch_size=n - 1)
    assert smaller.batched_load_bps > 100


def test_free_tier_is_applied_to_transactions_not_settlements():
    # 3 batches = 6 transactions; 4 free leaves 2 billable.
    assert economics.settlement_cost(3, 1_000, free_tx_remaining=4) == 2_000
    assert economics.settlement_cost(3, 1_000, free_tx_remaining=100) == 0


def test_conservation_check_uses_integer_equality_not_a_tolerance():
    class FakeBatch:
        batch_id = "batch_x"
        gross_atomic = 100
        platform_fee_atomic = 10
        author_net_atomic = 90
        call_count = 2

    class FakeCall:
        def __init__(self, c):
            self.captured_atomic = c

    ok, detail = economics.batch_conservation(FakeBatch(), [FakeCall(40), FakeCall(60)])
    assert ok, detail
    ok, detail = economics.batch_conservation(FakeBatch(), [FakeCall(40), FakeCall(59)])
    assert not ok and "99" in detail


def test_realised_load_ignores_batches_that_never_settled():
    class B:
        def __init__(self, gross, fee, tx):
            self.gross_atomic, self.facilitator_fee_atomic, self.settle_tx_hash = gross, fee, tx

    settled, never = B(200_000, 2_000, "0xabc"), B(999_999, 9_999, None)
    assert economics.realised_fee_load_bps([settled, never]) == 100


# ------------------------------------------------------------- no floats, ever --


class _FloatHunter(ast.NodeVisitor):
    """Finds any float that could reach a money value in app/pay.

    Static, because a runtime test only covers the paths a test happened to walk.
    Flags: float literals, `float(...)`, `round(...)`, and true division `/` --
    which in Python always yields a float, so `captured / 2` would silently turn
    an integer amount into one.
    """

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.findings: list[str] = []

    def _flag(self, node: ast.AST, what: str) -> None:
        self.findings.append(f"{self.path.name}:{getattr(node, 'lineno', '?')}: {what}")

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, float):
            self._flag(node, f"float literal {node.value!r}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in ("float", "round"):
            self._flag(node, f"{node.func.id}() call")
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Div):
            self._flag(node, "true division '/' -- use '//' for atomic units")
        self.generic_visit(node)


#: `gateway.py` passes `timeout=30.0` to httpx, which is a duration, not money.
ALLOWED = {"gateway.py:float literal 30.0"}


def test_no_float_can_reach_a_money_value_in_app_pay():
    offenders: list[str] = []
    for path in sorted(PAY_DIR.glob("*.py")):
        hunter = _FloatHunter(path)
        hunter.visit(ast.parse(path.read_text(encoding="utf-8")))
        for finding in hunter.findings:
            name, _line, what = finding.split(":", 2)
            if f"{name}:{what.strip()}" in ALLOWED:
                continue
            offenders.append(finding)
    assert offenders == [], "floats in the money core:\n  " + "\n  ".join(offenders)


def test_every_price_spec_field_that_holds_money_is_an_int():
    spec = _spec(
        price="$0.05", scheme="upto", max_price="$0.50", meter="bytes", price_per_unit="$0.001"
    )
    for value in (
        spec.price_atomic,
        spec.max_price_atomic,
        spec.authorized_atomic,
        spec.price_per_unit_atomic,
    ):
        assert isinstance(value, int) and not isinstance(value, bool)
    assert spec.scheme is Scheme.UPTO
