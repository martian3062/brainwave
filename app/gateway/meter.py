"""Metering: how a tool tells the payment layer what it actually consumed.

This is the piece that makes the `upto` scheme mean something. Under `exact`
the price is known before the tool runs, so there is nothing to meter. Under
`upto` the agent authorizes a CEILING and the server captures only what was
consumed -- but the x402 SDK has no channel for a tool handler to say how much
that was. `create_payment_wrapper` calls
``resource_server.settle_payment(payload, payment_requirements)`` with the SAME
requirements object it verified, so whatever amount was advertised in the 402 is
the amount that would settle.

The hook we use is the one the SDK's own permit2 code already reads:

    x402/mechanisms/evm/upto/permit2_utils.py:388
        settlement_amount = int(requirements.amount)
    ...:542
        if settlement_amount > permitted_amount:  -> error

So the *server* decides capture by handing settle a `PaymentRequirements` whose
`amount` is the captured value, while the payload's permit2 `permitted` amount
stays the ceiling. `app.gateway.resource_server.MeteredResourceServer` does
exactly that swap, and this module is how it learns the number.

Mechanism: a `ContextVar`. `create_payment_wrapper` awaits the handler and then
awaits settle inside the *same* task, so a contextvar set during execution is
still visible at settle time. (It also survives `asyncio.to_thread`, which
copies the context -- relevant because the SDK falls back to `to_thread` for a
synchronous `settle_payment`. Ours is async, so that path is not taken, but the
property is why this is safe either way.)

Tool handlers call the module-level `record()`. Outside a paid call there is no
active meter and `record()` is a no-op, so the same handler function runs
unchanged in a unit test or from a free tool.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Meter",
    "record",
    "current_meter",
    "meter_scope",
    "MeterError",
]


class MeterError(ValueError):
    """A metered tool reported something that cannot be charged for."""


@dataclass
class Meter:
    """Per-call metering state.

    `base_atomic` is charged unconditionally (a minimum / setup fee). Each
    recorded unit adds `price_per_unit_atomic`. The total is clamped to
    `max_atomic`, which is also the amount advertised in the 402 challenge and
    therefore the amount the agent authorized -- capture can never exceed it.
    The `call` table has a CHECK constraint saying the same thing, so a bug here
    is a database error rather than an overcharge.
    """

    tool_name: str
    #: Charged even if the tool records nothing (covers a failed-but-executed call).
    base_atomic: int = 0
    #: Atomic units per metered unit. None for `exact` tools.
    price_per_unit_atomic: int | None = None
    #: The authorized ceiling. Capture is clamped here.
    max_atomic: int | None = None
    #: "tokens" | "bytes" | "seconds" | "calls" -- recorded on the Call row.
    unit: str | None = None

    #: Set by `record()`. None means the tool never metered, so only `base_atomic`
    #: is captured -- the honest reading of "it ran but consumed nothing billable".
    units: int | None = None
    #: Free-form evidence shown in the receipt (model id, token split, ...).
    detail: dict[str, Any] = field(default_factory=dict)
    #: True once capture has been computed; guards against double-capture.
    settled: bool = False

    def record(self, units: int, **detail: Any) -> None:
        """Called by a tool handler once it knows its consumption."""
        if isinstance(units, bool) or not isinstance(units, int):
            raise MeterError(f"{self.tool_name}: metered units must be int, got {units!r}")
        if units < 0:
            raise MeterError(f"{self.tool_name}: negative metered units: {units}")
        self.units = (self.units or 0) + units
        self.detail.update(detail)

    def capture_atomic(self) -> int:
        """The amount to actually settle, in atomic units.

        Clamped to `max_atomic` rather than raising: a metering overrun is our
        problem, not the payer's, and silently charging past the authorization
        is the one outcome that must be impossible.
        """
        amount = self.base_atomic
        if self.units is not None and self.price_per_unit_atomic:
            amount += self.units * self.price_per_unit_atomic
        if self.max_atomic is not None:
            amount = min(amount, self.max_atomic)
        return max(amount, 0)

    def clamped(self) -> bool:
        """True when metered consumption exceeded the authorized ceiling.

        Surfaced in the receipt: the agent got more work than it paid for, and
        pretending otherwise would make the ledger a worse record than the truth.
        """
        if self.max_atomic is None or self.units is None or not self.price_per_unit_atomic:
            return False
        return self.base_atomic + self.units * self.price_per_unit_atomic > self.max_atomic

    def as_evidence(self) -> dict[str, Any]:
        """Metering evidence for the receipt body."""
        out: dict[str, Any] = {
            "meter": self.unit,
            "units": self.units,
            "pricePerUnitAtomic": self.price_per_unit_atomic,
            "baseAtomic": self.base_atomic,
            "capturedAtomic": self.capture_atomic(),
        }
        if self.max_atomic is not None:
            out["ceilingAtomic"] = self.max_atomic
        if self.clamped():
            out["clamped"] = True
        if self.detail:
            out["detail"] = dict(self.detail)
        return out


_CURRENT: contextvars.ContextVar[Meter | None] = contextvars.ContextVar(
    "eraya_gateway_meter", default=None
)


def current_meter() -> Meter | None:
    """The meter for the paid call in flight, or None outside one."""
    return _CURRENT.get()


def record(units: int, **detail: Any) -> None:
    """Report consumption from inside a tool handler.

    A no-op when no paid call is in flight, so handlers stay plain functions
    that can be imported and unit-tested without a payment context.
    """
    meter = _CURRENT.get()
    if meter is not None:
        meter.record(units, **detail)


class meter_scope:  # noqa: N801 -- used as a context manager, reads better lowercase
    """Bind a `Meter` for the duration of one paid call."""

    __slots__ = ("_meter", "_token")

    def __init__(self, meter: Meter) -> None:
        self._meter = meter
        self._token: contextvars.Token | None = None

    def __enter__(self) -> Meter:
        self._token = _CURRENT.set(self._meter)
        return self._meter

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            _CURRENT.reset(self._token)
            self._token = None
