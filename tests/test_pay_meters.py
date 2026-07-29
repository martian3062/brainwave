"""Meters, and the one invariant `upto` exists to guarantee.

.venv/Scripts/python -m pytest tests/test_pay_meters.py -q
"""

from __future__ import annotations

import os
import random

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("PUBLIC_BASE_URL", "http://testserver")

from app.money import PriceError  # noqa: E402
from app.pay.meters import (  # noqa: E402
    DECLARED_METER_KEY,
    ExecutionOutcome,
    capture,
    declared_units,
    get_meter,
    strip_meter_declaration,
)
from app.pay.pricing import PriceSpec  # noqa: E402

NET = "eip155:84532"
ASSET = "0x" + "22" * 20
PAY_TO = "0x" + "de" * 20


def metered(meter: str, *, price="$0.05", per_unit="$0.001", ceiling="$0.50") -> PriceSpec:
    return PriceSpec.declare(
        f"tool_{meter}",
        price=price,
        scheme="upto",
        max_price=ceiling,
        meter=meter,
        price_per_unit=per_unit,
        network=NET,
        asset=ASSET,
        pay_to=PAY_TO,
    )


def flat() -> PriceSpec:
    return PriceSpec.declare("flat", price="$0.002", network=NET, asset=ASSET, pay_to=PAY_TO)


def outcome(text="", *, ms=0, declared=None, result=None) -> ExecutionOutcome:
    return ExecutionOutcome(
        result=result if result is not None else text,
        result_text=text,
        elapsed_ms=ms,
        declared=declared,
    )


# ------------------------------------------------------------- unit counting --


def test_bytes_meter_counts_utf8_bytes_not_characters():
    """Billing `len(str)` would systematically under-charge non-Latin output.

    'héllo' is 5 characters and 6 bytes; a CJK response is 3 bytes per character.
    """
    assert get_meter("bytes").units(outcome("hello")) == 5
    assert get_meter("bytes").units(outcome("héllo")) == 6
    assert get_meter("bytes").units(outcome("答案")) == 6


def test_seconds_meter_rounds_up_with_integer_arithmetic():
    seconds = get_meter("seconds")
    assert seconds.units(outcome(ms=0)) == 0
    assert seconds.units(outcome(ms=1)) == 1  # a started second is consumed
    assert seconds.units(outcome(ms=1_000)) == 1
    assert seconds.units(outcome(ms=1_001)) == 2
    assert seconds.units(outcome(ms=59_999)) == 60
    assert all(isinstance(seconds.units(outcome(ms=n)), int) for n in (0, 1, 999, 10**7))


def test_tokens_meter_bills_zero_rather_than_guessing():
    """No `len(text) // 4` estimator anywhere. An unmeasured call bills the base.

    Under-charging on a missing measurement is a business problem. Over-charging
    on a guessed one is a correctness problem, and only one of those is acceptable
    in a payment system.
    """
    tokens = get_meter("tokens")
    assert tokens.units(outcome("a very long response " * 500)) == 0
    assert tokens.units(outcome("short", declared=1_487)) == 1_487


def test_flat_meter_is_one_unit_per_call():
    assert get_meter("calls").units(outcome("anything")) == 1
    assert get_meter(None).units(outcome("")) == 1


def test_an_unknown_meter_is_an_error_not_a_silent_fallback():
    """Falling back to flat pricing would mean a typo quietly stopped billing
    for consumption, and nobody would find out until the invoice."""
    with pytest.raises(PriceError):
        get_meter("tokns")


# ------------------------------------------------------------- declarations --


def test_a_tool_declares_its_own_usage_and_it_never_reaches_the_agent():
    result = {"answer": 42, DECLARED_METER_KEY: {"units": 1_487}}
    assert declared_units(result) == 1_487
    assert strip_meter_declaration(result) == {"answer": 42}
    assert DECLARED_METER_KEY not in strip_meter_declaration(result)


def test_a_bare_int_declaration_is_accepted_too():
    assert declared_units({"x": 1, DECLARED_METER_KEY: 12}) == 12
    assert declared_units({"x": 1}) is None
    assert declared_units("a string result") is None


@pytest.mark.parametrize("bad", [1.5, "1487", True, -3])
def test_a_garbage_declaration_fails_loudly_at_the_authors_desk(bad):
    with pytest.raises(PriceError):
        declared_units({DECLARED_METER_KEY: {"units": bad}})


# ----------------------------------------------------------------- capture ---


def test_capture_is_base_plus_consumption():
    spec = metered("bytes")  # $0.05 base, $0.001 per byte, $0.50 ceiling
    reading = capture(spec, outcome("x" * 39))
    assert reading.units == 39
    assert reading.computed_atomic == 50_000 + 39 * 1_000
    assert reading.captured_atomic == 89_000
    assert reading.capped is False


def test_the_ceiling_binds_and_the_seller_eats_the_difference():
    spec = metered("bytes")
    reading = capture(spec, outcome("x" * 10_000))  # would be 10_050_000
    assert reading.computed_atomic == 10_050_000
    assert reading.captured_atomic == 500_000  # the ceiling, exactly
    assert reading.capped is True


def test_an_unmetered_tool_captures_its_price_and_nothing_else():
    reading = capture(flat(), outcome("x" * 10_000, ms=99_999))
    assert reading.captured_atomic == 2_000
    assert reading.units == 0
    assert reading.authorized_atomic == 2_000


@pytest.mark.parametrize("seed", range(200))
def test_capture_never_exceeds_authorization_property(seed):
    """The promise `upto` makes to a buyer, over randomised prices and outputs.

    Checked here, again by a CHECK constraint on the `call` table, and again by
    `MeterReading.__post_init__`. Three times, because a payment system that can
    charge more than the authorization is not a payment system.
    """
    rng = random.Random(seed)
    base = rng.randrange(0, 100_000)
    ceiling = base + rng.randrange(0, 1_000_000)
    per_unit = rng.randrange(0, 10_000)
    meter = rng.choice(["bytes", "seconds", "tokens", "calls"])

    spec = PriceSpec.declare(
        f"prop_{seed}",
        price=base,
        scheme="upto",
        max_price=ceiling,
        meter=meter,
        price_per_unit=per_unit,
        network=NET,
        asset=ASSET,
        pay_to=PAY_TO,
    )
    reading = capture(
        spec,
        outcome(
            "é" * rng.randrange(0, 5_000),
            ms=rng.randrange(0, 10**7),
            declared=rng.choice([None, rng.randrange(0, 10**6)]),
        ),
    )
    assert 0 <= reading.captured_atomic <= reading.authorized_atomic == ceiling
    assert isinstance(reading.captured_atomic, int)
    assert reading.capped == (reading.computed_atomic > ceiling)


def test_a_meter_reading_that_would_overcharge_cannot_be_constructed():
    from app.pay.meters import MeterReading

    with pytest.raises(PriceError):
        MeterReading(
            meter="bytes",
            units=1,
            computed_atomic=2,
            captured_atomic=501,
            authorized_atomic=500,
            capped=False,
        )


def test_negative_elapsed_time_is_refused():
    """`monotonic_ns` cannot go backwards, but `time.time()` can across an NTP
    step -- so the type refuses the value the wrong clock would produce."""
    with pytest.raises(PriceError):
        ExecutionOutcome(result=None, result_text="", elapsed_ms=-1)
