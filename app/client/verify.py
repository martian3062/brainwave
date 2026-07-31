"""Receipt verification. Three independent layers, and honesty about each.

A receipt that only the issuer can check is a log line with better formatting.
This module checks one, from the buyer's side, at three levels of trust:

  LOCAL     -- the receipt is internally consistent and its canonical digest
               matches. Needs nothing but the receipt. Catches tampering and
               transcription errors; proves nothing about the money.

  ATTESTED  -- the `attestation` recovers to an address we expected. Needs the
               attestor's public address. Proves the issuer signed these exact
               numbers; still proves nothing about the money.

  ON-CHAIN  -- the transaction named by the receipt really did move that asset
               to that address in at least that amount. Needs an RPC URL.
               This is the only layer that proves anything about the money.

Each layer reports separately and the summary never claims more than it checked:
`verified` means all attempted layers passed, `layers` says which were attempted,
and a receipt verified only locally says so.

--------------------------------------------------------------------------
WHAT IS OURS AND WHAT IS THE PROTOCOL'S
--------------------------------------------------------------------------
Be exact about this. x402 v2 standardizes `SettleResponse` -- success, payer,
transaction, network -- and nothing else. There is no standard receipt object
and no standard attestation format in x402==2.16.0; `grep -r attestation` over
the installed wheel returns nothing. So:

  * `SettleResponse` fields are read straight from the protocol.
  * The receipt envelope, its canonical form, its digest and its attestation
    signature are OURS. They are defined here, in one place, and the gateway
    imports this module rather than reimplementing the canonicalization -- two
    implementations of a canonical form is two canonical forms.

`canonical_json` is therefore load-bearing for both sides. Its rules: sorted
keys, no whitespace, UTF-8 preserved, and `None` values dropped so that adding
an optional field to the envelope does not change the digest of receipts that
do not use it.

--------------------------------------------------------------------------
BATCHED RECEIPTS HAVE NO TX HASH YET, AND THAT IS NOT A FAILURE
--------------------------------------------------------------------------
The whole economic argument is that N calls settle in ONE transaction at the end
of a window. So between the call and the window closing, a perfectly valid
receipt has `txHash: null`. A verifier that reports that as "unverified" would
be reporting the product as a bug. `PENDING_BATCH` is a distinct outcome from
`FAILED`, and `verified` is not set false by it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

log = logging.getLogger("brainwave.verify")

__all__ = [
    "canonical_json",
    "body_digest",
    "verify_body_hash",
    "sign_attestation",
    "recover_attestation_signer",
    "CheckStatus",
    "Check",
    "ReceiptVerification",
    "verify_receipt",
    "verify_onchain_transfer",
    "ERC20_TRANSFER_TOPIC",
]

#: keccak256("Transfer(address,address,uint256)"). Recomputed in a test rather
#: than trusted as a constant, because a wrong topic silently finds no transfers
#: and reports "mismatch" for a perfectly good settlement.
ERC20_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

#: What `sign_attestation` prefixes the canonical body with, so a signature over
#: a receipt can never be replayed as a signature over anything else.
ATTESTATION_PREFIX = "x402-brainwave-receipt-v1:"


# --------------------------------------------------------------------------
# Canonical form -- shared with the gateway, defined once
# --------------------------------------------------------------------------


def canonical_json(body: Mapping[str, Any]) -> str:
    """The one canonical serialization of a receipt body.

    Sorted keys, no separators whitespace, non-ASCII preserved. MUST match
    `app.pay.receipts.canonical_json` byte for byte -- this docstring used to
    say so and then drifted anyway: this copy dropped `None` values (`batchId`
    is `None` on every per-call receipt) and the server's does not, so every
    real receipt failed local verification with "the body has been altered"
    even though nothing had. Found by running one real settlement against the
    live deployed gateway; `app.pay.receipts` is the side that actually writes
    the stored hash, so it is the side this one must match, not the reverse.
    """
    return json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def body_digest(body: Mapping[str, Any]) -> str:
    """`sha256:`-prefixed hex of the canonical body -- matches
    `app.pay.receipts.body_digest` exactly. The stored/received `bodyHash`
    always carries that prefix; a bare hex digest can never match it."""
    return "sha256:" + hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def verify_body_hash(body: Mapping[str, Any], expected: str) -> bool:
    """Constant-time compare. A digest check that leaks timing is theatre."""
    return hmac.compare_digest(body_digest(body), (expected or "").strip().lower())


def sign_attestation(body: Mapping[str, Any], private_key: str) -> str:
    """Sign a receipt body (EIP-191 personal_sign over the prefixed canonical form).

    Here for symmetry and for the tests: the gateway is what actually calls it.
    EIP-191 rather than EIP-712 because a receipt is an off-chain statement that
    no contract will ever consume -- typed-data machinery would buy nothing and
    would make the format harder to verify from another language.
    """
    from eth_account import Account
    from eth_account.messages import encode_defunct

    message = encode_defunct(text=ATTESTATION_PREFIX + canonical_json(body))
    return Account.sign_message(message, private_key=private_key).signature.hex()


def recover_attestation_signer(body: Mapping[str, Any], attestation: str) -> str | None:
    """Recover the address that signed this body, or None if it does not recover."""
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct

        signature = attestation if attestation.startswith("0x") else "0x" + attestation
        message = encode_defunct(text=ATTESTATION_PREFIX + canonical_json(body))
        return Account.recover_message(message, signature=signature)
    except Exception as exc:
        log.debug("attestation did not recover: %s", exc)
        return None


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    #: Not attempted -- the input needed was not supplied. Never counts as a pass.
    SKIPPED = "skipped"
    #: Batched settlement has not closed its window yet. Correct, not failed.
    PENDING_BATCH = "pending_batch"
    #: We tried and could not reach the chain. Distinct from FAILED, because
    #: "the RPC is down" and "the money never moved" are not the same news.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: CheckStatus
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status is CheckStatus.FAILED

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.name,
            "status": str(self.status),
            "detail": self.detail,
            **({"evidence": self.evidence} if self.evidence else {}),
        }


@dataclass(frozen=True, slots=True)
class ReceiptVerification:
    receipt_id: str
    checks: tuple[Check, ...]

    @property
    def verified(self) -> bool:
        """True when nothing failed. Read `layers` to know what that covered."""
        return not any(c.failed for c in self.checks)

    @property
    def layers(self) -> list[str]:
        """Which layers actually ran. `verified` without "onchain" here proves
        the paperwork, not the payment."""
        return [c.name for c in self.checks if c.status is CheckStatus.PASSED]

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.failed]

    def as_dict(self) -> dict[str, Any]:
        return {
            "receiptId": self.receipt_id,
            "verified": self.verified,
            "verifiedLayers": self.layers,
            "checks": [c.as_dict() for c in self.checks],
        }

    def summary(self) -> str:
        icon = {
            CheckStatus.PASSED: "ok  ",
            CheckStatus.FAILED: "FAIL",
            CheckStatus.SKIPPED: "skip",
            CheckStatus.PENDING_BATCH: "wait",
            CheckStatus.UNAVAILABLE: "n/a ",
        }
        lines = [f"receipt {self.receipt_id}: {'VERIFIED' if self.verified else 'NOT VERIFIED'}"]
        for check in self.checks:
            lines.append(f"  [{icon[check.status]}] {check.name}: {check.detail}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# On-chain
# --------------------------------------------------------------------------


def _topic_address(topic: Any) -> str:
    """A 32-byte log topic holding an address -> the 20-byte address, lowercased."""
    raw = topic.hex() if isinstance(topic, (bytes, bytearray)) else str(topic)
    raw = raw[2:] if raw.startswith("0x") else raw
    return "0x" + raw[-40:].lower()


def verify_onchain_transfer(
    tx_hash: str,
    *,
    rpc_url: str,
    asset: str,
    pay_to: str,
    min_amount_atomic: int,
) -> Check:
    """Did this transaction actually move at least this much of this asset there?

    Reads the transaction's ERC-20 `Transfer` logs and looks for one whose token
    contract is `asset` and whose recipient is `pay_to`. `min_amount_atomic`
    rather than an exact match on purpose: a BATCH settlement legitimately moves
    the sum of many calls in one transfer, so a per-call receipt's amount is a
    lower bound on what its settlement transaction carried, not an equality.
    """
    try:
        from web3 import Web3
    except ImportError:  # pragma: no cover -- web3 arrives with x402[all]
        return Check("onchain", CheckStatus.SKIPPED, "web3 is not installed")

    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 20}))
        receipt = w3.eth.get_transaction_receipt(tx_hash)
    except Exception as exc:
        return Check(
            "onchain",
            CheckStatus.UNAVAILABLE,
            f"could not read {tx_hash} from {rpc_url}: {type(exc).__name__}: {exc}",
        )

    if receipt.get("status") != 1:
        return Check(
            "onchain",
            CheckStatus.FAILED,
            f"transaction {tx_hash} reverted on chain",
            {"blockNumber": receipt.get("blockNumber")},
        )

    asset_lc = (asset or "").lower()
    pay_to_lc = (pay_to or "").lower()
    transfers: list[dict[str, Any]] = []

    for entry in receipt.get("logs", []):
        topics = entry.get("topics") or []
        if not topics:
            continue
        topic0 = topics[0]
        topic0_hex = topic0.hex() if isinstance(topic0, (bytes, bytearray)) else str(topic0)
        if not topic0_hex.startswith("0x"):
            topic0_hex = "0x" + topic0_hex
        if topic0_hex.lower() != ERC20_TRANSFER_TOPIC:
            continue
        if len(topics) < 3:
            continue  # not the indexed (from, to) shape
        data = entry.get("data")
        data_hex = data.hex() if isinstance(data, (bytes, bytearray)) else str(data)
        try:
            value = int(data_hex, 16)
        except ValueError:
            continue
        transfers.append(
            {
                "token": str(entry.get("address", "")).lower(),
                "from": _topic_address(topics[1]),
                "to": _topic_address(topics[2]),
                "value": value,
            }
        )

    matching = [
        t
        for t in transfers
        if (not asset_lc or t["token"] == asset_lc)
        and (not pay_to_lc or t["to"] == pay_to_lc)
        and t["value"] >= min_amount_atomic
    ]

    if matching:
        best = max(matching, key=lambda t: t["value"])
        return Check(
            "onchain",
            CheckStatus.PASSED,
            f"{tx_hash} moved {best['value']} atomic units of {best['token']} "
            f"to {best['to']} (receipt claims >= {min_amount_atomic})",
            {"transfer": best, "blockNumber": receipt.get("blockNumber")},
        )

    return Check(
        "onchain",
        CheckStatus.FAILED,
        f"{tx_hash} contains no ERC-20 Transfer of >= {min_amount_atomic} of {asset} to {pay_to}",
        {"transfersFound": transfers},
    )


# --------------------------------------------------------------------------
# The whole receipt
# --------------------------------------------------------------------------

_INT_FIELDS = ("authorizedAtomic", "capturedAtomic")


def _as_int(value: Any) -> int | None:
    """x402 puts amounts on the wire as strings; receipts may carry either."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def verify_receipt(
    receipt: Mapping[str, Any],
    *,
    expected_body_hash: str | None = None,
    expected_attestor: str | None = None,
    rpc_url: str | None = None,
    expected_payer: str | None = None,
) -> ReceiptVerification:
    """Run every layer the supplied inputs make possible.

    `receipt` is the receipt BODY -- the object the gateway hashed and returned
    inside the tool response, not a database row. Everything not supplied is
    reported SKIPPED rather than quietly assumed.
    """
    body = {k: v for k, v in receipt.items() if k not in {"bodyHash", "attestation"}}
    checks: list[Check] = []
    receipt_id = str(receipt.get("receiptId") or receipt.get("id") or "<unknown>")

    # ---------------------------------------------------------- 1. structure
    authorized = _as_int(receipt.get("authorizedAtomic"))
    captured = _as_int(receipt.get("capturedAtomic"))
    problems: list[str] = []
    for name in _INT_FIELDS:
        if _as_int(receipt.get(name)) is None:
            problems.append(f"{name} is missing or not an integer of atomic units")
    if authorized is not None and captured is not None:
        if captured > authorized:
            problems.append(
                f"captured {captured} exceeds authorized {authorized} -- the `upto` "
                "invariant is broken, which means the server took more than it was allowed"
            )
        if captured < 0 or authorized < 0:
            problems.append("negative amounts")
    for required in ("network", "asset", "payTo"):
        if not receipt.get(required):
            problems.append(f"{required} is missing")
    if expected_payer and str(receipt.get("payer", "")).lower() != expected_payer.lower():
        problems.append(
            f"payer is {receipt.get('payer')!r}, expected {expected_payer!r} -- "
            "this receipt is not ours"
        )

    checks.append(
        Check("structure", CheckStatus.FAILED, "; ".join(problems))
        if problems
        else Check(
            "structure",
            CheckStatus.PASSED,
            f"captured {captured} of {authorized} authorized on {receipt.get('network')}",
        )
    )

    # ------------------------------------------------------------- 2. digest
    supplied_hash = expected_body_hash or receipt.get("bodyHash")
    if not supplied_hash:
        checks.append(Check("digest", CheckStatus.SKIPPED, "no bodyHash to compare against"))
    elif verify_body_hash(body, str(supplied_hash)):
        checks.append(Check("digest", CheckStatus.PASSED, f"sha256 matches {supplied_hash}"))
    else:
        checks.append(
            Check(
                "digest",
                CheckStatus.FAILED,
                "canonical sha256 does not match the receipt's bodyHash -- "
                "the body has been altered since it was issued",
                {"computed": body_digest(body), "claimed": str(supplied_hash)},
            )
        )

    # -------------------------------------------------------- 3. attestation
    attestation = receipt.get("attestation")
    if not attestation:
        checks.append(Check("attestation", CheckStatus.SKIPPED, "receipt carries no attestation"))
    elif not expected_attestor:
        recovered = recover_attestation_signer(body, str(attestation))
        checks.append(
            Check(
                "attestation",
                CheckStatus.SKIPPED,
                f"signature recovers to {recovered} but no expected attestor was given, "
                "so this proves nothing about who issued it",
                {"recovered": recovered or ""},
            )
        )
    else:
        recovered = recover_attestation_signer(body, str(attestation))
        if recovered and recovered.lower() == expected_attestor.lower():
            checks.append(Check("attestation", CheckStatus.PASSED, f"signed by {recovered}"))
        else:
            checks.append(
                Check(
                    "attestation",
                    CheckStatus.FAILED,
                    f"signature recovers to {recovered!r}, expected {expected_attestor!r}",
                    {"recovered": recovered or ""},
                )
            )

    # ------------------------------------------------------------ 4. onchain
    tx_hash = receipt.get("txHash") or receipt.get("transaction")
    settlement = str(receipt.get("settlement") or "").lower()
    if not tx_hash:
        if settlement == "batched":
            checks.append(
                Check(
                    "onchain",
                    CheckStatus.PENDING_BATCH,
                    "batched settlement: this call is authorized and captured, and settles "
                    "with the rest of the session at window close. No tx hash yet is "
                    "correct, not missing.",
                )
            )
        else:
            checks.append(
                Check(
                    "onchain",
                    CheckStatus.FAILED,
                    "receipt claims immediate settlement but names no transaction",
                )
            )
    elif not rpc_url:
        checks.append(
            Check(
                "onchain",
                CheckStatus.SKIPPED,
                f"receipt names {tx_hash} but no rpc_url was given, so the money was "
                "not actually checked",
            )
        )
    else:
        checks.append(
            verify_onchain_transfer(
                str(tx_hash),
                rpc_url=rpc_url,
                asset=str(receipt.get("asset") or ""),
                pay_to=str(receipt.get("payTo") or ""),
                min_amount_atomic=captured or 0,
            )
        )

    return ReceiptVerification(receipt_id=receipt_id, checks=tuple(checks))
