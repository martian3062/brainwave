"""Prices: human strings in, integer atomic units out, x402 wire strings at the edge.

Three representations exist and they are never confused:

    "$0.002"   what an author writes in the catalogue. Parsed once, at import.
    2000       integer atomic units. The ONLY form money is stored or arithmetic'd in.
    "2000"     the x402 wire form. `PaymentRequirements.amount` is a *string* of
               atomic units -- that is the protocol's choice, not ours, and it is
               why no rounding can happen between us and the facilitator.

WHY THIS MODULE EXISTS WHEN `app.money` ALREADY PARSES PRICES
-------------------------------------------------------------
`app.money.parse_price` is the spine's converter and it is correct, but it is
permissive by design: it stringifies whatever it is given. Hand it the float
`0.002` and `str(0.002)` is `'0.002'`, so it returns 2000 and a float has just
travelled through a money path unnoticed. (Hand it `0.1 + 0.2` and it raises,
because `str()` of that is `'0.30000000000000004'` -- so the hole is silent for
clean literals and loud for dirty ones, which is the worst combination.)

Everything in `app.pay` goes through `atomic()` below, which refuses `float`
outright. `tests/test_pay_money.py::test_the_strict_parser_refuses_floats` pins it.

A NOTE ON THE SDK'S OWN MONEY HANDLING
--------------------------------------
`x402.http.x402_http_server_base.resolve_settlement_override_amount` does
`round(float(dollars) * 10**decimals)` and `UptoEvmScheme._default_money_conversion`
does `int(amount * (10 ** decimals))` on a float. We do not route prices through
either: we compute atomic units with `Decimal` and hand the SDK a string that is
already exact, so those paths are never reached with a fractional value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

# The pydantic ResourceInfo, NOT `x402.mcp.ResourceInfo`. The latter is a plain
# class with no `model_dump()`, and the SDK's 402 path calls `model_dump()` on it
# -- see app/mcp_app.py and tests/test_spine.py::test_x402_mcp_resource_info_is_the_wrong_class.
from x402.schemas.payments import PaymentRequirements, ResourceInfo

from app.models import Scheme
from app.money import PriceError, format_atomic, parse_price, to_decimal

__all__ = [
    "PriceError",
    "PriceSpec",
    "atomic",
    "wire_amount",
    "from_wire",
    "payment_requirements",
    "resource_info",
    "quote",
    "MeteredPrice",
]

#: Human-writable price. `int` means "already atomic"; `Decimal` and `str` are
#: parsed exactly. `float` is deliberately absent -- see the module docstring.
Price = str | int | Decimal


def atomic(price: Price, decimals: int) -> int:
    """Parse a price to integer atomic units, refusing floats.

    Delegates the actual parsing to `app.money.parse_price` (one converter in the
    repository, as designed) and adds the one guard that converter cannot add
    without changing its contract.

    Raises:
        PriceError: on a float, a negative value, or a price that needs more
            precision than the asset has decimals. $0.0000001 at 6 decimals is a
            bug, not a zero.
    """
    if isinstance(price, float):
        raise PriceError(
            f"float price {price!r}: money is integer atomic units in this codebase. "
            "Write the price as a string ('$0.002'), a Decimal, or an int of atomic units."
        )
    return parse_price(price, decimals)


def wire_amount(atomic_units: int) -> str:
    """Atomic units in the form x402 puts on the wire.

    `PaymentRequirements.amount` is typed `str` in `x402.schemas.payments`. Passing
    an int would be coerced by pydantic, but going through this function keeps the
    "integers inside, strings only at the boundary" rule visible at every call site.
    """
    if isinstance(atomic_units, bool) or not isinstance(atomic_units, int):
        raise PriceError(f"wire amount must be an int of atomic units, got {atomic_units!r}")
    if atomic_units < 0:
        raise PriceError(f"negative wire amount: {atomic_units}")
    return str(atomic_units)


def from_wire(amount: str) -> int:
    """Inverse of `wire_amount`. Facilitator responses carry amounts as strings."""
    try:
        value = int(str(amount))
    except (TypeError, ValueError) as exc:
        raise PriceError(f"not an atomic amount: {amount!r}") from exc
    if value < 0:
        raise PriceError(f"negative amount from wire: {amount!r}")
    return value


@dataclass(frozen=True)
class MeteredPrice:
    """The metering half of a price. Absent for a flat `exact` tool."""

    #: "tokens" | "bytes" | "seconds" | "calls" -- see app.pay.meters.
    meter: str
    #: Atomic units charged per metered unit, on top of the base price.
    price_per_unit_atomic: int

    def __post_init__(self) -> None:
        if self.price_per_unit_atomic < 0:
            raise PriceError(f"negative price per unit: {self.price_per_unit_atomic}")


@dataclass(frozen=True)
class PriceSpec:
    """Everything needed to advertise, meter and settle one tool.

    Frozen because it is built once at decoration time and then read on every
    call from multiple threads. The mutable, repriceable copy of these numbers is
    the `tool` row; this is the declaration that seeded it.
    """

    name: str
    resource_url: str
    scheme: Scheme
    network: str
    asset: str
    asset_decimals: int
    pay_to: str

    #: Base price, atomic units. What an `exact` call costs, and the floor of an
    #: `upto` call.
    price_atomic: int
    #: `upto` ceiling. None for `exact` and `batch-settlement`.
    max_price_atomic: int | None = None
    metered: MeteredPrice | None = None

    max_timeout_seconds: int = 300
    description: str | None = None
    tags: tuple[str, ...] = ()
    #: Scheme-specific `PaymentRequirements.extra`. `upto` needs
    #: `facilitatorAddress`, `batch-settlement` needs `receiverAuthorizer`; both
    #: are filled in by the SDK's own `enhance_payment_requirements` when the
    #: gateway has been initialized against a facilitator (see app.pay.gateway).
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.price_atomic < 0:
            raise PriceError(f"{self.name}: negative price {self.price_atomic}")
        if self.asset_decimals < 0:
            raise PriceError(f"{self.name}: negative asset decimals")

        if self.scheme is Scheme.UPTO:
            if self.max_price_atomic is None:
                raise PriceError(
                    f"{self.name}: the 'upto' scheme is a ceiling and a capture. "
                    "Declare max_price -- without it there is nothing to capture under."
                )
            if self.metered is None:
                raise PriceError(
                    f"{self.name}: 'upto' without a meter would always capture the base "
                    "price, which is 'exact' with extra steps. Declare a meter."
                )
        elif self.max_price_atomic is not None:
            raise PriceError(
                f"{self.name}: max_price is only meaningful for the 'upto' scheme; "
                f"this tool is '{self.scheme.value}'."
            )

        if self.max_price_atomic is not None and self.max_price_atomic < self.price_atomic:
            raise PriceError(
                f"{self.name}: max_price ({self.max_price_atomic}) is below the base price "
                f"({self.price_atomic}); the DB CHECK ck_tool_ceiling_ge_price says the same."
            )

    # ---------------------------------------------------------------- money ---

    @property
    def authorized_atomic(self) -> int:
        """What the agent is asked to authorize.

        For `upto` this is the ceiling, and the actual capture is decided after the
        tool has run. For every other scheme authorization and capture are the same
        number. This is the value that goes into `PaymentRequirements.amount`.
        """
        return self.max_price_atomic if self.max_price_atomic is not None else self.price_atomic

    @property
    def is_metered(self) -> bool:
        return self.metered is not None

    @property
    def meter_name(self) -> str | None:
        return self.metered.meter if self.metered else None

    @property
    def price_per_unit_atomic(self) -> int | None:
        return self.metered.price_per_unit_atomic if self.metered else None

    def display(self, atomic_units: int) -> str:
        return format_atomic(atomic_units, self.asset_decimals)

    def decimal(self, atomic_units: int) -> Decimal:
        return to_decimal(atomic_units, self.asset_decimals)

    # ------------------------------------------------------------ building ----

    @classmethod
    def declare(
        cls,
        name: str,
        *,
        price: Price,
        network: str,
        asset: str,
        pay_to: str,
        asset_decimals: int = 6,
        scheme: Scheme | str = Scheme.EXACT,
        max_price: Price | None = None,
        meter: str | None = None,
        price_per_unit: Price | None = None,
        resource_url: str | None = None,
        max_timeout_seconds: int = 300,
        description: str | None = None,
        tags: tuple[str, ...] | list[str] = (),
        extra: dict[str, Any] | None = None,
    ) -> PriceSpec:
        """Build a spec from what an author writes. All parsing happens here, once."""
        scheme_enum = Scheme(scheme) if not isinstance(scheme, Scheme) else scheme

        metered: MeteredPrice | None = None
        if meter is not None:
            if price_per_unit is None:
                raise PriceError(f"{name}: meter {meter!r} declared without price_per_unit")
            metered = MeteredPrice(
                meter=meter,
                price_per_unit_atomic=atomic(price_per_unit, asset_decimals),
            )
        elif price_per_unit is not None:
            raise PriceError(f"{name}: price_per_unit declared without a meter")

        return cls(
            name=name,
            # `mcp://tool/<name>` is the convention `create_payment_wrapper` itself
            # falls back to, so we match it rather than inventing a URL shape.
            resource_url=resource_url or f"mcp://tool/{name}",
            scheme=scheme_enum,
            network=network,
            asset=asset,
            asset_decimals=asset_decimals,
            pay_to=pay_to,
            price_atomic=atomic(price, asset_decimals),
            max_price_atomic=(atomic(max_price, asset_decimals) if max_price is not None else None),
            metered=metered,
            max_timeout_seconds=max_timeout_seconds,
            description=description,
            tags=tuple(tags),
            extra=dict(extra or {}),
        )


def payment_requirements(
    spec: PriceSpec, *, amount_atomic: int | None = None
) -> PaymentRequirements:
    """The `PaymentRequirements` this tool advertises.

    Built directly rather than through `x402ResourceServer.build_payment_requirements`
    for one reason: that method raises `RuntimeError("Server not initialized")` until
    `initialize()` has made a blocking HTTP call to the facilitator, and tool
    registration happens at import time. `app.pay.gateway.enhance` applies the
    scheme-specific `extra` (upto's `facilitatorAddress`, batch-settlement's
    `receiverAuthorizer`) later, using the SDK's own `enhance_payment_requirements`,
    once the facilitator has actually answered.

    `amount_atomic` overrides the advertised amount. That is how `upto` capture
    works: the same requirements object is settled with the metered amount instead
    of the ceiling.
    """
    return PaymentRequirements(
        scheme=spec.scheme.value,
        network=spec.network,
        asset=spec.asset,
        amount=wire_amount(spec.authorized_atomic if amount_atomic is None else amount_atomic),
        pay_to=spec.pay_to,
        max_timeout_seconds=spec.max_timeout_seconds,
        extra=dict(spec.extra),
    )


def resource_info(spec: PriceSpec) -> ResourceInfo:
    """The `ResourceInfo` carried in the 402 challenge.

    `x402.schemas.payments.ResourceInfo`, never `x402.mcp.ResourceInfo` -- the
    latter has no `model_dump()` and the SDK's own 402 path calls it.
    """
    return ResourceInfo(
        url=spec.resource_url,
        description=spec.description or f"Tool: {spec.name}",
        mime_type="application/json",
        # `tags` is capped at 5 entries of 32 chars by the bazaar spec.
        tags=list(spec.tags[:5]) or None,
    )


def quote(spec: PriceSpec) -> dict[str, Any]:
    """Human-readable price card. Display only -- amounts are strings, never floats."""
    body: dict[str, Any] = {
        "tool": spec.name,
        "resource": spec.resource_url,
        "scheme": spec.scheme.value,
        "network": spec.network,
        "asset": spec.asset,
        "assetDecimals": spec.asset_decimals,
        "price": spec.display(spec.price_atomic),
        "priceAtomic": wire_amount(spec.price_atomic),
        "authorizedAtomic": wire_amount(spec.authorized_atomic),
    }
    if spec.max_price_atomic is not None:
        body["maxPrice"] = spec.display(spec.max_price_atomic)
        body["maxPriceAtomic"] = wire_amount(spec.max_price_atomic)
    if spec.metered is not None:
        body["meter"] = spec.metered.meter
        body["pricePerUnit"] = spec.display(spec.metered.price_per_unit_atomic)
        body["pricePerUnitAtomic"] = wire_amount(spec.metered.price_per_unit_atomic)
    return body
