"""Money. Integer atomic units, everywhere, always.

There is exactly one representation of value in this codebase: a Python `int`
counting the token's smallest unit (6 decimals for USDC, so $0.002 == 2000).
Nothing here returns a float, no column anywhere is a Float, and the only place
`Decimal` appears is inside the parser -- it never escapes.

This is not fastidiousness. The whole submission rests on a ledger claim:
sum(captured) across a session equals the amount settled on-chain, exactly. One
float round-trip and that claim is a rounding artefact instead of a proof.

x402 itself agrees: `PaymentRequirements.amount` is a *string* of atomic units.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

__all__ = [
    "parse_price",
    "format_atomic",
    "to_decimal",
    "split_take",
    "fee_load_bps",
    "PriceError",
]


class PriceError(ValueError):
    """A price string could not be represented exactly in atomic units."""


def parse_price(price: str | int | Decimal, decimals: int) -> int:
    """Parse a human price into integer atomic units.

    Accepts ``"$0.002"``, ``"0.002"``, ``"0.002 USDC"``, ``Decimal("0.002")``,
    or an ``int`` that is *already* atomic (passed through unchanged).

    Raises `PriceError` if the value cannot be represented exactly -- $0.0000001
    at 6 decimals is not 0, it is a bug, and rounding it silently would put a
    lie in the ledger.
    """
    if isinstance(price, bool):  # bool is an int subclass; refuse it explicitly
        raise PriceError(f"not a price: {price!r}")
    if isinstance(price, int):
        if price < 0:
            raise PriceError(f"negative price: {price}")
        return price

    if isinstance(price, Decimal):
        value = price
    else:
        text = str(price).strip().replace("$", "").replace(",", "").replace("_", "")
        # tolerate a trailing symbol: "0.002 USDC"
        text = text.split()[0] if text.split() else text
        try:
            value = Decimal(text)
        except (InvalidOperation, IndexError) as exc:
            raise PriceError(f"cannot parse price {price!r}") from exc

    if value < 0:
        raise PriceError(f"negative price: {price!r}")

    scaled = value * (Decimal(10) ** decimals)
    atomic = int(scaled)
    if Decimal(atomic) != scaled:
        raise PriceError(
            f"price {price!r} needs more precision than {decimals} decimals allow "
            f"(smallest representable unit is {format_atomic(1, decimals)})"
        )
    return atomic


def format_atomic(atomic: int, decimals: int, *, symbol: str = "") -> str:
    """Render atomic units for humans: ``2000, 6 -> '0.002000'``."""
    text = f"{to_decimal(atomic, decimals):.{decimals}f}"
    return f"{text} {symbol}".strip() if symbol else text


def to_decimal(atomic: int, decimals: int) -> Decimal:
    """Exact decimal view of an atomic amount. For display and reports only."""
    return Decimal(atomic) / (Decimal(10) ** decimals)


def split_take(gross_atomic: int, take_bps: int) -> tuple[int, int]:
    """Split gross revenue into ``(platform_fee, author_net)``.

    Floor the platform's cut and give the remainder to the author, so
    ``platform + author == gross`` holds exactly for every input. Conservation
    is a property test, not a comment.
    """
    if gross_atomic < 0:
        raise PriceError(f"negative gross: {gross_atomic}")
    if not 0 <= take_bps <= 10_000:
        raise PriceError(f"take_bps out of range: {take_bps}")
    platform = (gross_atomic * take_bps) // 10_000
    return platform, gross_atomic - platform


def fee_load_bps(settlement_cost_atomic: int, gross_atomic: int) -> int:
    """Settlement cost as basis points of gross revenue.

    This is the headline number of the whole submission. Per-call at $0.002 with
    a $0.001 facilitator fee: 5000 bps (50%). The same 100 calls batched into one
    settlement: 50 bps (0.5%).
    """
    if gross_atomic <= 0:
        return 0
    return (settlement_cost_atomic * 10_000) // gross_atomic
