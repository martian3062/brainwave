"""The metering and money core.

WHAT IS OURS AND WHAT IS THE SDK'S -- read this before reading anything else.

The x402 Python SDK already implements paid MCP. `x402.mcp.create_payment_wrapper`
issues the 402 challenge, verifies the authorization with the facilitator, runs the
handler and settles -- carrying payment in the MCP `_meta` keys `x402/payment` and
`x402/payment-response`. `x402.mechanisms.evm.batch_settlement` already implements
payment channels with cumulative vouchers and a claim/settle channel manager. We
did not write either and do not claim to.

`app.pay` is the layer the SDK does not have:

  pricing.py    exact integer prices and the `PaymentRequirements` they produce
  meters.py     what an `upto` call actually consumed, in integer units
  decorator.py  `@paid()` -- a thin wrapper OVER `create_payment_wrapper` that adds
                pricing, metering, a ledger and receipts. The protocol work stays
                in the SDK; the only protocol-adjacent thing we do is adjust the
                settled amount for `upto` (see the module docstring -- the MCP
                wrapper has no equivalent of the HTTP path's settlement overrides)
  receipts.py   issue / attest / verify, with session -> batch -> tx reconciliation
  batching.py   session windows and batch close, built ON the SDK's channel manager
  economics.py  platform take, author net, and the realised fee load that the
                README's economics table quotes
  gateway.py    the x402 SDK object graph (facilitator client, resource server,
                registered schemes) as lazily built, injectable singletons

THE ONE RULE THAT RUNS THROUGH ALL OF IT: money is an integer number of atomic
units. Not a float, not a Decimal outside a parser, not a string except on the
x402 wire where the protocol itself says string. `tests/test_pay_money.py` fails
the build if a float ever reaches a money path.
"""

from __future__ import annotations

__all__ = [
    "pricing",
    "meters",
    "economics",
    "receipts",
    "batching",
    "gateway",
    "decorator",
]
