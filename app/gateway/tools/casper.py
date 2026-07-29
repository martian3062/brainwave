"""Paid Casper chain reads.

Adapted from the ERAYA backend's `core/casper/sdk.py`, which is read-only for
this project: the shapes are copied, the module is not imported, and nothing
here writes to that codebase or its deployment.

Priced at $0.001 because that is what they cost: one JSON-RPC round trip to a
Casper 2.0 node each, with a real network wait and a real rate-limit budget
being consumed on our side. They are the honest cheap end of the catalogue, and
having a cheap tier is what makes the batching argument visible -- a $0.001 call
settled individually against a $0.001 facilitator fee burns 50% of the revenue.

These tools READ. The gateway holds no Casper key, signs nothing and submits
nothing. Every value returned can be independently re-fetched from a public node
with the same call, which is the point of returning `rpcMethod` alongside the
answer.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from app.gateway.config import gateway_settings as gw
from app.gateway.ledger import ToolSpec
from app.gateway.paid import paid
from app.gateway.tools._upstream import MOTES_PER_CSPR, casper_rpc, rpc_error
from app.models import Scheme

__all__ = ["register"]


def _cspr(motes: int) -> str:
    """Motes -> CSPR as an exact decimal STRING.

    Not a float. A balance is money, and `app.money`'s rule -- no float ever
    touches a value -- is not suspended because the chain happens to be Casper
    rather than Base.
    """
    whole, remainder = divmod(int(motes), MOTES_PER_CSPR)
    return f"{whole}.{remainder:09d}".rstrip("0").rstrip(".") or "0"


BALANCE = ToolSpec(
    name="casper_balance",
    description=(
        "Main-purse balance of a Casper account, in motes and CSPR, read live from a "
        "Casper 2.0 node over JSON-RPC. Defaults to the configured treasury account "
        "when public_key is omitted."
    ),
    price_atomic=gw.casper_read_atomic,
    scheme=Scheme.EXACT,
    tags=("casper", "chain", "balance", "read"),
    rationale="One query_balance JSON-RPC round trip.",
)

TRANSACTION = ToolSpec(
    name="casper_transaction",
    description=(
        "Look up a Casper transaction by hash: whether it succeeded, which block it "
        "landed in, gas cost and every transfer it moved. Handles both Casper 2.0 "
        "transactions and legacy deploys."
    ),
    price_atomic=gw.casper_read_atomic,
    scheme=Scheme.EXACT,
    tags=("casper", "chain", "transaction", "read"),
    rationale="Up to two info_get_transaction round trips (Version1, then Deploy).",
)

CHAIN_STATUS = ToolSpec(
    name="casper_chain_status",
    description=(
        "Health of the Casper node this gateway reads from: chain name, API version, "
        "latest block height and peer count. Use it to confirm the chain answers below "
        "came from a real, synced network."
    ),
    price_atomic=gw.casper_read_atomic,
    scheme=Scheme.EXACT,
    tags=("casper", "chain", "health", "read"),
    rationale="One info_get_status JSON-RPC round trip.",
)


def register(mcp: FastMCP) -> None:
    @mcp.tool(name=BALANCE.name, description=BALANCE.description)
    @paid(
        BALANCE,
        input_schema={
            "type": "object",
            "properties": {
                "public_key": {
                    "type": "string",
                    "description": "Hex account key ('01…' ed25519, '02…' secp256k1).",
                }
            },
            "required": [],
        },
        example={"public_key": ""},
    )
    async def casper_balance(public_key: str = "") -> dict:
        key = (public_key or gw.casper_public_key or "").strip()
        if not key:
            return {
                "ok": False,
                "error": "no public key given and CASPER_PUBLIC_KEY is not set",
                "rpcMethod": "query_balance",
            }

        response = await casper_rpc(
            "query_balance", {"purse_identifier": {"main_purse_under_public_key": key}}
        )
        if "result" not in response:
            return {
                "ok": False,
                "error": rpc_error(response),
                "publicKey": key,
                "rpcMethod": "query_balance",
                "node": gw.casper_node_rpc_url,
            }

        motes = int(response["result"]["balance"])
        return {
            "ok": True,
            "publicKey": key,
            "motes": str(motes),
            "cspr": _cspr(motes),
            "network": gw.casper_chain_name,
            "node": gw.casper_node_rpc_url,
            "rpcMethod": "query_balance",
        }

    @mcp.tool(name=TRANSACTION.name, description=TRANSACTION.description)
    @paid(
        TRANSACTION,
        input_schema={
            "type": "object",
            "properties": {
                "tx_hash": {"type": "string", "description": "Transaction or deploy hash (hex)."}
            },
            "required": ["tx_hash"],
        },
        example={"tx_hash": "0f2c…"},
    )
    async def casper_transaction(tx_hash: str) -> dict:
        tx_hash = (tx_hash or "").strip()
        if not tx_hash:
            return {"ok": False, "error": "tx_hash is required"}

        # Casper 2.0 addresses transactions by one of two identifier shapes and
        # gives no way to tell which applies from the hash alone, so both are
        # tried. This is why the tool is priced for up to two round trips.
        for identifier in ({"Version1": tx_hash}, {"Deploy": tx_hash}):
            response = await casper_rpc(
                "info_get_transaction",
                {"transaction_hash": identifier, "finalized_approvals": True},
            )
            if "result" not in response:
                continue

            info = response["result"].get("execution_info") or {}
            execution = (info.get("execution_result") or {}).get("Version2") or {}
            transfers: list[dict[str, Any]] = []
            for entry in execution.get("transfers") or []:
                transfer = entry.get("Version2") or entry.get("Version1") or {}
                sender = transfer.get("from")
                transfers.append(
                    {
                        "from": sender.get("AccountHash") if isinstance(sender, dict) else sender,
                        "to": transfer.get("to"),
                        "motes": str(transfer.get("amount")),
                    }
                )

            cost = int(execution.get("cost", 0) or 0)
            return {
                "ok": True,
                "txHash": tx_hash,
                "kind": "Deploy" if "Deploy" in identifier else "Version1",
                "blockHeight": info.get("block_height"),
                "success": execution.get("error_message") is None,
                "errorMessage": execution.get("error_message"),
                "costMotes": str(cost),
                "costCspr": _cspr(cost),
                "transfers": transfers,
                "explorerUrl": f"{gw.casper_explorer_base.rstrip('/')}/transaction/{tx_hash}",
                "rpcMethod": "info_get_transaction",
            }

        return {
            "ok": False,
            "txHash": tx_hash,
            "error": "transaction not found on chain",
            "triedIdentifiers": ["Version1", "Deploy"],
            "rpcMethod": "info_get_transaction",
        }

    @mcp.tool(name=CHAIN_STATUS.name, description=CHAIN_STATUS.description)
    @paid(
        CHAIN_STATUS,
        input_schema={"type": "object", "properties": {}, "required": []},
        example={},
    )
    async def casper_chain_status() -> dict:
        response = await casper_rpc("info_get_status")
        if "result" not in response:
            return {
                "ok": False,
                "error": rpc_error(response),
                "node": gw.casper_node_rpc_url,
                "rpcMethod": "info_get_status",
            }

        result = response["result"]
        block = result.get("last_added_block_info") or {}
        return {
            "ok": True,
            "node": gw.casper_node_rpc_url,
            "chainName": result.get("chainspec_name"),
            "apiVersion": result.get("api_version"),
            "buildVersion": result.get("build_version"),
            "blockHeight": block.get("height"),
            "blockHash": block.get("hash"),
            "peers": len(result.get("peers", []) or []),
            "rpcMethod": "info_get_status",
        }
