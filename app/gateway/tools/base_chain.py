"""Paid Base/EVM chain reads.

BRAINWAVE settles on Base, but until this module the only priced chain reads in
the catalogue were Casper's (inherited read-only from ERAYA -- see casper.py).
These three fill that gap with reads that matter to THIS project's own claim:
what has `payTo` actually received, and did a given settlement transaction
really move USDC. `base_transaction` decodes the receipt's Transfer event logs
rather than just linking out to an explorer, so a claimTxHash/settleTxHash can
be resolved natively through this gateway's own MCP surface.

Priced at the same $0.001 cheap tier as the Casper reads, for the same reason:
one JSON-RPC round trip, a real network wait, a real cost on our side.

These tools READ. The gateway holds no EVM signing key for these calls and
never submits a Base transaction from here -- only the facilitator's own
settlement path does that. Every value returned can be independently
re-fetched from any Base node with the same call, which is why `rpcMethod` and
`node` are returned alongside the answer.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from app.config import settings
from app.gateway.config import gateway_settings as gw
from app.gateway.ledger import ToolSpec
from app.gateway.paid import paid
from app.gateway.tools._upstream import evm_rpc, rpc_error
from app.models import Scheme
from app.money import format_atomic

__all__ = ["register"]

#: keccak256("balanceOf(address)")[:4] -- the standard ERC-20 selector.
_BALANCE_OF_SELECTOR = "0x70a08231"
#: keccak256("Transfer(address,address,uint256)") -- the standard ERC-20 event topic.
_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _wei_to_eth(wei: int) -> str:
    """Wei -> ETH as an exact decimal STRING.

    Not a float. A balance is money, and `app.money`'s rule -- no float ever
    touches a value -- is not suspended because the unit happens to be wei
    rather than USDC atomic units.
    """
    whole, remainder = divmod(int(wei), 10**18)
    return f"{whole}.{remainder:018d}".rstrip("0").rstrip(".") or "0"


def _pad_address(address: str) -> str:
    return address.lower().removeprefix("0x").rjust(64, "0")


def _balance_of_calldata(address: str) -> str:
    return _BALANCE_OF_SELECTOR + _pad_address(address)


def _hex_to_int(value: Any) -> int:
    if not value or value in ("0x", "0x0"):
        return 0
    return int(value, 16)


def _is_address(value: str) -> bool:
    return value.startswith("0x") and len(value) == 42


def _decode_usdc_transfers(logs: list[dict[str, Any]], usdc_address: str) -> list[dict[str, Any]]:
    """Pull Transfer(from, to, value) events for the configured USDC contract
    out of a transaction receipt's logs. Everything else in the receipt is
    noise for this project's purposes -- what matters is whether USDC moved,
    and to where."""
    target = usdc_address.lower()
    transfers: list[dict[str, Any]] = []
    for log in logs or []:
        if str(log.get("address", "")).lower() != target:
            continue
        topics = log.get("topics") or []
        if len(topics) < 3 or str(topics[0]).lower() != _TRANSFER_TOPIC:
            continue
        value = _hex_to_int(log.get("data"))
        transfers.append(
            {
                "from": "0x" + str(topics[1])[-40:],
                "to": "0x" + str(topics[2])[-40:],
                "valueAtomic": str(value),
                "value": format_atomic(
                    value, settings.x402_asset_decimals, symbol=settings.x402_asset_symbol
                ),
            }
        )
    return transfers


BALANCE = ToolSpec(
    name="base_balance",
    description=(
        "ETH (gas) and USDC balance of a Base account, read live over JSON-RPC from the "
        "configured Base node. Omit `address` to check this gateway's own configured "
        "payTo address -- the fastest way to see what it has actually been paid."
    ),
    price_atomic=gw.base_read_atomic,
    scheme=Scheme.EXACT,
    tags=("base", "chain", "balance", "read", "usdc"),
    rationale="One eth_getBalance plus one eth_call (USDC balanceOf) JSON-RPC round trip.",
)

TRANSACTION = ToolSpec(
    name="base_transaction",
    description=(
        "Look up a Base transaction by hash: success, block, gas used, and any USDC "
        "transfers it moved -- decoded from the receipt's Transfer event logs, not just "
        "a link out to an explorer. This is how a settlement's claimTxHash or "
        "settleTxHash gets verified from inside this gateway rather than only on Basescan."
    ),
    price_atomic=gw.base_read_atomic,
    scheme=Scheme.EXACT,
    tags=("base", "chain", "transaction", "read", "usdc"),
    rationale="One eth_getTransactionReceipt JSON-RPC round trip.",
)

CHAIN_STATUS = ToolSpec(
    name="base_chain_status",
    description=(
        "Health of the Base node this gateway reads from: chain id and latest block "
        "height, so the two tools above are checkably answers from a live, "
        "correctly-networked node rather than a stale or misconfigured one."
    ),
    price_atomic=gw.base_read_atomic,
    scheme=Scheme.EXACT,
    tags=("base", "chain", "health", "read"),
    rationale="One eth_blockNumber plus one eth_chainId JSON-RPC round trip.",
)


def register(mcp: FastMCP) -> None:
    @mcp.tool(name=BALANCE.name, description=BALANCE.description)
    @paid(
        BALANCE,
        input_schema={
            "type": "object",
            "properties": {
                "address": {
                    "type": "string",
                    "description": "0x-prefixed account address. Defaults to PAY_TO_ADDRESS.",
                }
            },
            "required": [],
        },
        example={"address": ""},
    )
    async def base_balance(address: str = "") -> dict:
        addr = (address or settings.pay_to_address or "").strip()
        if not _is_address(addr):
            return {"ok": False, "error": "address must be a 0x-prefixed 20-byte address"}

        eth_response = await evm_rpc("eth_getBalance", [addr, "latest"])
        if "result" not in eth_response:
            return {
                "ok": False,
                "error": rpc_error(eth_response),
                "address": addr,
                "rpcMethod": "eth_getBalance",
                "node": gw.base_rpc_url,
            }
        wei = _hex_to_int(eth_response["result"])

        usdc_response = await evm_rpc(
            "eth_call",
            [{"to": settings.asset_address, "data": _balance_of_calldata(addr)}, "latest"],
        )
        usdc_ok = "result" in usdc_response
        usdc_atomic = _hex_to_int(usdc_response["result"]) if usdc_ok else None

        return {
            "ok": True,
            "address": addr,
            "network": settings.x402_network,
            "weiAtomic": str(wei),
            "eth": _wei_to_eth(wei),
            "usdcAtomic": str(usdc_atomic) if usdc_atomic is not None else None,
            "usdc": (
                format_atomic(
                    usdc_atomic, settings.x402_asset_decimals, symbol=settings.x402_asset_symbol
                )
                if usdc_atomic is not None
                else None
            ),
            "usdcError": None if usdc_ok else rpc_error(usdc_response),
            "node": gw.base_rpc_url,
            "rpcMethod": "eth_getBalance + eth_call",
        }

    @mcp.tool(name=TRANSACTION.name, description=TRANSACTION.description)
    @paid(
        TRANSACTION,
        input_schema={
            "type": "object",
            "properties": {
                "tx_hash": {"type": "string", "description": "0x-prefixed transaction hash."}
            },
            "required": ["tx_hash"],
        },
        example={"tx_hash": "0x…"},
    )
    async def base_transaction(tx_hash: str) -> dict:
        tx_hash = (tx_hash or "").strip()
        if not tx_hash:
            return {"ok": False, "error": "tx_hash is required"}

        response = await evm_rpc("eth_getTransactionReceipt", [tx_hash])
        receipt = response.get("result") if "result" in response else None
        if not receipt:
            return {
                "ok": False,
                "txHash": tx_hash,
                "error": rpc_error(response, "transaction not found on chain"),
                "rpcMethod": "eth_getTransactionReceipt",
                "node": gw.base_rpc_url,
            }

        return {
            "ok": True,
            "txHash": tx_hash,
            "success": receipt.get("status") == "0x1",
            "blockNumber": _hex_to_int(receipt.get("blockNumber")),
            "gasUsed": _hex_to_int(receipt.get("gasUsed")),
            "from": receipt.get("from"),
            "to": receipt.get("to"),
            "usdcTransfers": _decode_usdc_transfers(receipt.get("logs") or [], settings.asset_address),
            "explorerUrl": settings.explorer_url(tx_hash),
            "rpcMethod": "eth_getTransactionReceipt",
        }

    @mcp.tool(name=CHAIN_STATUS.name, description=CHAIN_STATUS.description)
    @paid(CHAIN_STATUS, input_schema={"type": "object", "properties": {}, "required": []}, example={})
    async def base_chain_status() -> dict:
        block_response = await evm_rpc("eth_blockNumber")
        if "result" not in block_response:
            return {
                "ok": False,
                "error": rpc_error(block_response),
                "node": gw.base_rpc_url,
                "rpcMethod": "eth_blockNumber",
            }
        chain_response = await evm_rpc("eth_chainId")

        return {
            "ok": True,
            "node": gw.base_rpc_url,
            "network": settings.x402_network,
            "chainIdHex": chain_response.get("result"),
            "chainId": _hex_to_int(chain_response["result"]) if "result" in chain_response else None,
            "blockNumber": _hex_to_int(block_response["result"]),
            "rpcMethod": "eth_blockNumber + eth_chainId",
        }
