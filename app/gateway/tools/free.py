"""Tools that are never paywalled, and why each one has to be free.

**Discovery** (`list_paid_tools`, `how_to_pay`). Charging for the price list is
a closed shop, not a marketplace. An agent must be able to find out what a tool
costs, in what asset, on what network, before it signs anything -- otherwise the
only way to learn the price is to pay it.

**Verification** (`verify_receipt`, `payment_session`). A receipt you have to pay
to check is not evidence. These read only the caller's own artefacts and are
free on principle: the whole claim of this project is that its ledger is
auditable, and an auditable ledger with a paywall in front of the audit is not
one.

They are also the only tools here that touch no upstream and no key, so they
work on a completely empty `.env` -- which makes them the first thing a new
integrator can successfully call.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from app.config import settings
from app.gateway import ledger
from app.gateway.paid import AGENT_META_KEY, RECEIPT_META_KEY, REGISTRY
from app.gateway.resource_server import facilitator_status
from app.money import fee_load_bps, format_atomic

__all__ = ["register"]


def _fmt(atomic: int) -> str:
    return format_atomic(atomic, settings.x402_asset_decimals, symbol=settings.x402_asset_symbol)


def register(mcp: FastMCP) -> None:
    # ---------------------------------------------------------- discovery --
    @mcp.tool(
        name="list_paid_tools",
        description=(
            "FREE. Every paid tool on this gateway with its price, scheme, metering "
            "terms and network. Call this before paying for anything."
        ),
    )
    def list_paid_tools() -> str:
        rows = {row["name"]: row for row in ledger.catalogue()}
        declared = {spec.name: spec for spec in REGISTRY}
        tools: list[dict[str, Any]] = []

        for name in sorted(set(rows) | set(declared)):
            row = rows.get(name, {})
            spec = declared.get(name)
            entry: dict[str, Any] = dict(row) if row else {"name": name}
            if spec is not None:
                entry.setdefault("description", spec.description)
                entry["rationale"] = spec.rationale or None
                entry["authorizeAtomic"] = str(spec.authorized_atomic)
                entry["authorize"] = _fmt(spec.authorized_atomic)
                # The advertised 402 amount is fixed at process start (see
                # app/gateway/paid.py). If an operator repriced the row from
                # /admin, the ledger and the live challenge disagree until the
                # next restart -- so say so instead of letting a buyer discover
                # it through a rejected payment.
                live = row.get("priceAtomic")
                if live is not None and int(live) != spec.price_atomic:
                    entry["priceDrift"] = {
                        "advertisedAtomic": str(spec.price_atomic),
                        "ledgerAtomic": str(live),
                        "note": (
                            "The ledger row was repriced but the live 402 challenge "
                            "still advertises the boot-time price. Restart the gateway "
                            "to make the new price effective."
                        ),
                    }
            tools.append(entry)

        return json.dumps(
            {
                "count": len(tools),
                "network": settings.x402_network,
                "asset": settings.asset_address,
                "assetSymbol": settings.x402_asset_symbol,
                "assetDecimals": settings.x402_asset_decimals,
                "payTo": settings.pay_to_address,
                "settlement": "batched" if settings.batching_enabled else "per_call",
                "facilitator": facilitator_status(),
                "tools": tools,
                "free": [
                    "list_paid_tools",
                    "how_to_pay",
                    "verify_receipt",
                    "payment_session",
                    "gateway_info",
                ],
            }
        )

    @mcp.tool(
        name="how_to_pay",
        description=(
            "FREE. Exactly how to pay for a tool on this gateway: where the "
            "authorization goes in the MCP request, where the receipt comes back, and "
            "what differs between the exact and upto schemes."
        ),
    )
    def how_to_pay() -> str:
        demo_price = 2_000  # $0.002 at 6 decimals -- the canonical micro-call
        fee = settings.facilitator_fee_atomic
        return json.dumps(
            {
                "x402Version": 2,
                "transport": {
                    "note": (
                        "Over MCP, payment rides in JSON-RPC _meta -- NOT in an "
                        "X-PAYMENT header. Headers apply only to this server's "
                        "plain-HTTP paywalled routes, which are a different path."
                    ),
                    "requestMetaKey": "x402/payment",
                    "responseMetaKey": "x402/payment-response",
                    "receiptMetaKey": RECEIPT_META_KEY,
                    "agentLabelMetaKey": AGENT_META_KEY,
                },
                "flow": [
                    "Call the tool with no payment. The result is isError=true and its "
                    "structuredContent is an x402 PaymentRequired with `accepts`.",
                    "Pick an entry from `accepts` and sign it. In Python: "
                    "x402.mechanisms.evm.signers.EthAccountSigner over eth-account -- "
                    "no JavaScript is involved anywhere in this flow.",
                    "Call the tool again with the signed payload in _meta under 'x402/payment'.",
                    "The result carries the settlement under "
                    "'x402/payment-response' and this gateway's receipt under "
                    f"'{RECEIPT_META_KEY}' (also folded into the JSON body as `_payment`).",
                    "Check it any time with the free `verify_receipt` tool.",
                ],
                "clientHelper": (
                    "x402.mcp.create_x402_mcp_client wraps an MCP client so the whole "
                    "loop above is one call. This gateway did not invent that helper "
                    "and recommends using it."
                ),
                "schemes": {
                    "exact": (
                        "The price is known before the tool runs. You authorize it and "
                        "exactly that amount is captured."
                    ),
                    "upto": (
                        "The price is not knowable in advance (an LLM call). You "
                        "authorize a ceiling; the tool meters what it consumed and only "
                        "that is captured. The receipt reports authorized AND captured. "
                        "Advertised only once the facilitator publishes the "
                        "facilitatorAddress the scheme requires -- until then the tool "
                        "falls back to exact at the ceiling, which is worse for you, so "
                        "check the `scheme` field of the accepts entry you sign."
                    ),
                },
                "economics": {
                    "facilitatorFeePerSettlement": _fmt(fee),
                    "exampleCall": _fmt(demo_price),
                    "feeLoadIfSettledPerCallBps": fee_load_bps(fee, demo_price),
                    "feeLoadIf100CallsBatchedBps": fee_load_bps(fee, demo_price * 100),
                    "note": (
                        "At $0.002 a call, one $0.001 settlement per call burns 50% of "
                        "revenue. The same fee across a 100-call session is 0.5%. That "
                        "gap is why this gateway batches, and every number here is "
                        "recomputed from the ledger rather than quoted."
                    ),
                },
            }
        )

    # ------------------------------------------------------- verification --
    @mcp.tool(
        name="verify_receipt",
        description=(
            "FREE, always. Check a receipt this gateway issued: that the stored body "
            "still hashes to its recorded digest, that your copy matches the stored "
            "one, and whether a settlement transaction exists yet. Verification is "
            "never paywalled."
        ),
    )
    def verify_receipt(receipt_id: str, receipt_json: str = "") -> str:
        presented: dict[str, Any] | None = None
        if receipt_json.strip():
            try:
                parsed = json.loads(receipt_json)
                presented = parsed if isinstance(parsed, dict) else None
            except ValueError:
                return json.dumps(
                    {
                        "receiptId": receipt_id,
                        "error": "receipt_json is not valid JSON",
                        "hint": "pass the receipt object exactly as it was returned",
                    }
                )
        return json.dumps(ledger.verify_receipt(receipt_id.strip(), presented))

    @mcp.tool(
        name="payment_session",
        description=(
            "FREE. Audit one payment session: how many calls, how much was captured, "
            "how much has actually settled on-chain, and what the facilitator fee load "
            "works out to -- against what it would have been settling every call "
            "individually. The batching claim, recomputed from the ledger."
        ),
    )
    def payment_session(session_id: str) -> str:
        summary = ledger.session_summary(session_id.strip())
        if summary is None:
            return json.dumps({"sessionId": session_id, "found": False})
        return json.dumps({"found": True, **summary})
