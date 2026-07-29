"""Meters: how much a call actually consumed, in integer units.

A meter turns "the tool ran" into an integer, and `capture()` turns that integer
into an amount of money that is never more than what the agent authorized.

THE INVARIANT
-------------
    captured <= authorized,  always, for every meter, for every input.

It is checked three times on purpose: `capture()` clamps, `Call` has a database
CHECK constraint (`ck_call_capture_le_authorized`), and
`tests/test_pay_meters.py` property-tests it over random inputs. Three because
this is the promise `upto` makes to a buyer, and a payment system that can charge
more than the authorization is not a payment system.

WHY THERE IS NO TOKEN ESTIMATOR
-------------------------------
The obvious `len(text) // 4` token heuristic is a way to bill an agent for a
number nobody measured. A meter that cannot measure returns ZERO billable units,
so an unmeasured call captures the base price and nothing more. Under-charging is
a business problem; over-charging on a guess is a correctness problem.

A tool declares real usage by returning a dict containing `_meter`:

    return {"answer": ..., "_meter": {"units": 1487}}

`app.pay.decorator` strips that key before the result reaches the agent, so the
declaration is billing metadata and never leaks into the tool's output contract.

EVERY NUMBER HERE IS AN int. Duration is measured with `time.monotonic_ns()` and
divided down with integer arithmetic; `time.time()` is wall-clock and goes
backwards across an NTP step, which would produce a negative billable duration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.money import PriceError

__all__ = [
    "DECLARED_METER_KEY",
    "ExecutionOutcome",
    "Meter",
    "MeterReading",
    "capture",
    "get_meter",
    "METERS",
    "declared_units",
    "strip_meter_declaration",
]

#: Key a tool uses to declare its own consumption. Stripped from the response.
DECLARED_METER_KEY = "_meter"


@dataclass(frozen=True)
class ExecutionOutcome:
    """What the wrapper observed while the tool ran.

    `elapsed_ms` comes from `monotonic_ns`, so it is a non-negative integer even
    across a clock adjustment.
    """

    #: The value the handler returned, after `_meter` was stripped.
    result: Any
    #: The exact text that will be sent to the agent. Byte metering uses this, so
    #: what is billed is what was delivered.
    result_text: str
    elapsed_ms: int
    #: Units the tool declared for itself, if any.
    declared: int | None = None

    def __post_init__(self) -> None:
        if self.elapsed_ms < 0:
            raise PriceError(f"negative elapsed_ms: {self.elapsed_ms}")
        if self.declared is not None:
            if isinstance(self.declared, bool) or not isinstance(self.declared, int):
                raise PriceError(f"declared meter units must be an int, got {self.declared!r}")
            if self.declared < 0:
                raise PriceError(f"negative declared meter units: {self.declared}")


class Meter(Protocol):
    """A billing strategy. `name` is what lands in `call.meter`."""

    name: str

    def units(self, outcome: ExecutionOutcome) -> int:
        """Billable units consumed. Non-negative integer, never an estimate."""
        ...


class _Flat:
    """One unit per call. The degenerate meter; `exact` pricing in meter clothing."""

    name = "calls"

    def units(self, outcome: ExecutionOutcome) -> int:  # noqa: ARG002
        return 1


class _Tokens:
    """Tokens the tool declared it used.

    Undeclared means zero: see the module docstring on why there is no estimator.
    """

    name = "tokens"

    def units(self, outcome: ExecutionOutcome) -> int:
        return outcome.declared if outcome.declared is not None else 0


class _Bytes:
    """UTF-8 bytes of the response actually delivered to the agent.

    Measured on the encoded text, not `len(str)`: a 500-character response of CJK
    or emoji is not 500 bytes, and billing the character count would systematically
    under-charge non-Latin output.
    """

    name = "bytes"

    def units(self, outcome: ExecutionOutcome) -> int:
        if outcome.declared is not None:
            return outcome.declared
        return len(outcome.result_text.encode("utf-8"))


class _Seconds:
    """Wall-clock seconds of execution, rounded UP.

    A tool that ran for 1 ms bills as one second. Rounding down would make a fast
    tool free, which is exactly backwards for a compute meter -- the floor is the
    base price, and the first started second is consumed.
    """

    name = "seconds"

    def units(self, outcome: ExecutionOutcome) -> int:
        if outcome.declared is not None:
            return outcome.declared
        # Integer ceiling division. `math.ceil(ms / 1000)` would route a money
        # input through a float.
        return -(-outcome.elapsed_ms // 1000)


METERS: dict[str, Meter] = {m.name: m for m in (_Flat(), _Tokens(), _Bytes(), _Seconds())}


def get_meter(name: str | None) -> Meter:
    """Look up a meter by name. Unknown names are a startup error, not a fallback.

    Silently falling back to flat pricing would mean a repricing typo quietly
    stopped charging for consumption, and nobody would notice until the invoice.
    """
    if name is None:
        return METERS["calls"]
    try:
        return METERS[name]
    except KeyError:
        raise PriceError(f"unknown meter {name!r}; known meters are {sorted(METERS)}") from None


@dataclass(frozen=True)
class MeterReading:
    """The billing decision for one call, and the evidence behind it."""

    meter: str
    units: int
    #: Base + metered, before the ceiling was applied.
    computed_atomic: int
    #: What will actually be captured. Never above `authorized_atomic`.
    captured_atomic: int
    authorized_atomic: int
    #: True when the ceiling bound the price -- i.e. the tool consumed more than
    #: the agent authorized and we ate the difference rather than overcharging.
    capped: bool

    def __post_init__(self) -> None:
        if self.captured_atomic > self.authorized_atomic:
            raise PriceError(
                f"capture {self.captured_atomic} exceeds authorization "
                f"{self.authorized_atomic} -- this must be impossible"
            )
        if self.captured_atomic < 0:
            raise PriceError(f"negative capture: {self.captured_atomic}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "meter": self.meter,
            "units": self.units,
            "computedAtomic": str(self.computed_atomic),
            "capturedAtomic": str(self.captured_atomic),
            "authorizedAtomic": str(self.authorized_atomic),
            "capped": self.capped,
        }


def capture(spec: Any, outcome: ExecutionOutcome) -> MeterReading:
    """Decide what this call captured.

        captured = min(base + units * per_unit, ceiling, authorized)

    `spec` is an `app.pay.pricing.PriceSpec` (untyped here to keep this module
    importable without a cycle). For an unmetered tool the result is simply the
    base price, so this one function covers `exact`, `upto` and
    `batch-settlement` without a branch at the call site.
    """
    authorized = int(spec.authorized_atomic)
    base = int(spec.price_atomic)

    if spec.metered is None:
        computed = base
        units = 0
        meter_name = spec.meter_name or "calls"
    else:
        meter = get_meter(spec.metered.meter)
        units = meter.units(outcome)
        if units < 0:
            raise PriceError(f"meter {meter.name!r} returned negative units: {units}")
        computed = base + units * int(spec.metered.price_per_unit_atomic)
        meter_name = meter.name

    ceiling = authorized
    if spec.max_price_atomic is not None:
        ceiling = min(ceiling, int(spec.max_price_atomic))

    captured = computed if computed < ceiling else ceiling
    if captured < 0:
        captured = 0

    return MeterReading(
        meter=meter_name,
        units=units,
        computed_atomic=computed,
        captured_atomic=captured,
        authorized_atomic=authorized,
        capped=computed > ceiling,
    )


def declared_units(result: Any) -> int | None:
    """Read a tool's own `_meter` declaration, if it made one.

    Tolerant of shape (`{"units": N}` or a bare int) and strict about type: a
    float or a negative is a `PriceError`, never a silent zero, because a tool
    reporting garbage should fail loudly at the author's desk.
    """
    if not isinstance(result, dict):
        return None
    raw = result.get(DECLARED_METER_KEY)
    if raw is None:
        return None
    value = raw.get("units") if isinstance(raw, dict) else raw
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise PriceError(
            f"{DECLARED_METER_KEY}.units must be an int of consumed units, got {value!r}"
        )
    if value < 0:
        raise PriceError(f"{DECLARED_METER_KEY}.units is negative: {value}")
    return value


def strip_meter_declaration(result: Any) -> Any:
    """Remove `_meter` so billing metadata never reaches the agent's payload."""
    if isinstance(result, dict) and DECLARED_METER_KEY in result:
        return {k: v for k, v in result.items() if k != DECLARED_METER_KEY}
    return result
