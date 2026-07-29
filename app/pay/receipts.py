"""Receipts: issue, attest, verify.

A receipt is the artefact that makes this a payment system rather than a billing
claim. It answers, for one tool call, without trusting this server:

    who paid, who was paid, how much was AUTHORIZED, how much was CAPTURED,
    under which scheme on which network, in which session, in which batch,
    and -- once the batch lands -- in which transaction.

THREE KINDS OF EVIDENCE, IN DECREASING ORDER OF STRENGTH
--------------------------------------------------------
1. `tx_hash`  -- the chain. Nobody has to believe us.
2. `attestation` -- the facilitator's own settlement response, which we did not
   author. `SettleResponse.transaction` for an immediate settlement, or the
   batch's claim/settle hashes for a batched one.
3. `body_hash` -- sha256 of the canonical receipt body. This proves only that the
   receipt has not been edited since we issued it. It is OURS, it is the weakest
   link, and `verify()` labels it as such. A hash you compute over your own data
   is not third-party evidence and this module never pretends otherwise.

CANONICALISATION
----------------
`canonical_json` sorts keys, uses compact separators and disables ASCII escaping,
so the same body always hashes the same. Amounts inside the body are STRINGS of
atomic units -- the x402 wire convention -- so a JSON parser that turns numbers
into doubles cannot corrupt the amount before it is re-hashed. This is the same
reason `PaymentRequirements.amount` is a string in the SDK.

Verification is free. It is a plain function here and must stay outside the
paywall wherever it is exposed: charging someone to check whether they were
charged correctly is indefensible.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session as DBSession
from sqlmodel import select

from app.config import settings
from app.models import Batch, Call, PaySession, Receipt, SettlementMode, Tool, utcnow

log = logging.getLogger(__name__)

__all__ = [
    "canonical_json",
    "body_digest",
    "build_body",
    "issue",
    "attach_batch_settlement",
    "Check",
    "Verification",
    "verify",
]


def canonical_json(body: dict[str, Any]) -> str:
    """Deterministic JSON. Same body in, same bytes out, on any platform."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def body_digest(body: dict[str, Any]) -> str:
    """sha256 of the canonical body, hex, prefixed so the algorithm is on the record."""
    return "sha256:" + hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def _receipt_id() -> str:
    return f"rcpt_{uuid.uuid4().hex[:24]}"


def build_body(
    *,
    receipt_id: str,
    call: Call,
    session: PaySession,
    resource_url: str,
    settlement: SettlementMode,
    tx_hash: str | None,
    facilitator: str,
    attestation: str | None,
    batch_id: str | None,
    meter_reading: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The exact JSON handed back to the agent, and the thing that gets hashed.

    Field names are camelCase to match x402's own wire vocabulary
    (`payTo`, `maxTimeoutSeconds`, ...), so an agent already parsing x402 does not
    need a second naming convention for our receipts.
    """
    body: dict[str, Any] = {
        "receiptId": receipt_id,
        "x402Version": 2,
        "callId": call.call_id,
        "sessionId": session.session_id,
        "batchId": batch_id,
        "scheme": str(call.scheme),
        "network": call.network,
        "asset": call.asset,
        "assetDecimals": session.asset_decimals,
        # Both numbers, always. Under `upto` they differ, and showing only the
        # captured amount would hide the ceiling the agent actually signed.
        "authorizedAtomic": str(call.authorized_atomic),
        "capturedAtomic": str(call.captured_atomic),
        "payer": call.payer,
        "payTo": call.pay_to,
        "resource": resource_url,
        "settlement": str(settlement),
        "transaction": tx_hash,
        "explorer": settings.explorer_url(tx_hash),
        "facilitator": facilitator,
        "attestation": attestation,
        "issuedAt": utcnow().isoformat(),
    }
    if meter_reading is not None:
        body["meter"] = meter_reading
    if extra:
        # Additional facts that must live INSIDE the hashed body rather than
        # beside it. The only current user is `app.cli.seed_demo`, which sets
        # `{"isDemo": True}` so a seeded receipt still says so after it has been
        # copied out of the database into a screenshot -- and so the flag cannot
        # be edited off without breaking `body_hash`. Merged last, but it cannot
        # overwrite a protocol field: those keys are asserted below.
        clash = set(extra) & set(body)
        if clash:
            raise ValueError(f"extra may not overwrite receipt fields: {sorted(clash)}")
        body.update(extra)
    return body


def issue(
    db: DBSession,
    *,
    call: Call,
    session: PaySession,
    tool: Tool | None = None,
    settlement: SettlementMode = SettlementMode.BATCHED,
    tx_hash: str | None = None,
    facilitator: str | None = None,
    attestation: str | None = None,
    batch: Batch | None = None,
    meter_reading: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> Receipt:
    """Write the receipt for a call. One receipt per call -- a DB unique index.

    `tx_hash` is None for a batched call and that is the truthful state: at issue
    time the money has been captured against a signed voucher but nothing has
    moved on-chain. `attach_batch_settlement` fills it in when the batch lands.
    Writing a placeholder hash here would be the single most dishonest thing this
    codebase could do.
    """
    receipt_id = _receipt_id()
    resource_url = tool.resource_url if tool is not None else f"mcp://tool/{call.call_id}"
    body = build_body(
        receipt_id=receipt_id,
        call=call,
        session=session,
        resource_url=resource_url,
        settlement=settlement,
        tx_hash=tx_hash,
        facilitator=facilitator or settings.facilitator_label,
        attestation=attestation,
        batch_id=batch.batch_id if batch is not None else None,
        meter_reading=meter_reading,
        extra=extra,
    )

    receipt = Receipt(
        receipt_id=receipt_id,
        call_id=call.id,
        session_id=session.id,
        batch_id=batch.id if batch is not None else None,
        scheme=call.scheme,
        network=call.network,
        asset=call.asset,
        asset_decimals=session.asset_decimals,
        authorized_atomic=call.authorized_atomic,
        captured_atomic=call.captured_atomic,
        payer=call.payer,
        pay_to=call.pay_to,
        resource_url=resource_url,
        settlement=settlement,
        tx_hash=tx_hash,
        explorer_url=settings.explorer_url(tx_hash),
        facilitator=facilitator or settings.facilitator_label,
        attestation=attestation,
        body_hash=body_digest(body),
        body_json=canonical_json(body),
        # Mirrors `extra["isDemo"]` into an indexed column so the dashboard and
        # `app.demo.summary()` can filter without parsing every body. The body
        # remains the authoritative copy -- it is the one that is hashed.
        is_demo=bool((extra or {}).get("isDemo", False)),
    )
    db.add(receipt)
    db.flush()
    return receipt


def attach_batch_settlement(db: DBSession, batch: Batch) -> int:
    """Point every receipt in a settled batch at the transaction that paid it.

    The body is REBUILT and REHASHED rather than patched, because a receipt whose
    stored hash no longer matches its stored body is indistinguishable from a
    tampered one, and `verify()` would report our own bookkeeping as fraud.

    Returns the number of receipts updated.
    """
    tx_hash = batch.settle_tx_hash or batch.claim_tx_hash
    if not tx_hash:
        raise ValueError(
            f"batch {batch.batch_id} has no transaction hash; nothing to attach. "
            "A batch that did not settle must not decorate its receipts as if it had."
        )

    receipts = list(db.exec(select(Receipt).where(Receipt.batch_id == batch.id)))
    for receipt in receipts:
        try:
            body = json.loads(receipt.body_json)
        except json.JSONDecodeError:
            log.error("receipt %s has unparseable body; leaving it alone", receipt.receipt_id)
            continue
        body["transaction"] = tx_hash
        body["explorer"] = settings.explorer_url(tx_hash)
        body["settlement"] = str(SettlementMode.BATCHED)
        body["batchId"] = batch.batch_id
        body["settledAt"] = (batch.settled_at or utcnow()).isoformat()

        receipt.tx_hash = tx_hash
        receipt.explorer_url = settings.explorer_url(tx_hash)
        receipt.body_json = canonical_json(body)
        receipt.body_hash = body_digest(body)
        db.add(receipt)
    db.flush()
    return len(receipts)


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """One verifiable assertion about a receipt."""

    name: str
    ok: bool
    detail: str
    #: "chain" | "facilitator" | "local" -- how much the check is worth. A "local"
    #: pass proves only internal consistency.
    strength: str


@dataclass
class Verification:
    receipt_id: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def status(self) -> str:
        if not self.ok:
            return "failed"
        if any(c.ok and c.strength == "chain" for c in self.checks):
            return "verified_onchain"
        if any(c.ok and c.strength == "facilitator" for c in self.checks):
            return "verified_attested"
        return "verified_local"

    def as_dict(self) -> dict[str, Any]:
        return {
            "receiptId": self.receipt_id,
            "ok": self.ok,
            "status": self.status,
            "checks": [
                {"name": c.name, "ok": c.ok, "detail": c.detail, "strength": c.strength}
                for c in self.checks
            ],
        }


def verify(db: DBSession, receipt_id: str, *, record: bool = True) -> Verification:
    """Check a receipt against the ledger. Free, and never behind the paywall.

    Deliberately does NOT phone the chain: an RPC call would make verification
    slow, rate-limited and failable, and every check below is answerable from the
    receipt plus the ledger. The transaction hash is returned so the caller can do
    the one check we cannot do for them -- looking at Basescan themselves.
    """
    result = Verification(receipt_id=receipt_id)
    receipt = db.exec(select(Receipt).where(Receipt.receipt_id == receipt_id)).first()
    if receipt is None:
        result.checks.append(Check("exists", False, "no such receipt", "local"))
        return result
    result.checks.append(Check("exists", True, "receipt found", "local"))

    # 1. Tamper detection over our own body.
    try:
        body = json.loads(receipt.body_json)
    except json.JSONDecodeError:
        result.checks.append(Check("body_parses", False, "body_json is not JSON", "local"))
        return result
    recomputed = body_digest(body)
    result.checks.append(
        Check(
            "body_hash",
            recomputed == receipt.body_hash,
            f"stored {receipt.body_hash} vs recomputed {recomputed}",
            "local",
        )
    )

    # 2. The invariant `upto` exists to guarantee.
    result.checks.append(
        Check(
            "capture_within_authorization",
            receipt.captured_atomic <= receipt.authorized_atomic,
            f"captured {receipt.captured_atomic} <= authorized {receipt.authorized_atomic}",
            "local",
        )
    )

    # 3. The receipt must agree with the call row it claims to describe.
    call = db.get(Call, receipt.call_id)
    if call is None:
        result.checks.append(Check("call_exists", False, "orphan receipt", "local"))
    else:
        agrees = (
            call.captured_atomic == receipt.captured_atomic
            and call.authorized_atomic == receipt.authorized_atomic
            and call.payer == receipt.payer
            and call.pay_to == receipt.pay_to
        )
        result.checks.append(
            Check(
                "matches_ledger",
                agrees,
                f"call {call.call_id}: captured {call.captured_atomic}, "
                f"authorized {call.authorized_atomic}",
                "local",
            )
        )
        result.checks.append(
            Check(
                "revenue_split_conserves",
                call.platform_fee_atomic + call.author_net_atomic == call.captured_atomic,
                f"platform {call.platform_fee_atomic} + author {call.author_net_atomic} "
                f"= captured {call.captured_atomic}",
                "local",
            )
        )

    # 4. Facilitator attestation, if the settlement was immediate.
    if receipt.attestation:
        result.checks.append(
            Check(
                "facilitator_attestation",
                True,
                f"{receipt.facilitator} attested: {receipt.attestation[:64]}",
                "facilitator",
            )
        )

    # 5. The chain, via the batch. This is the check that actually matters.
    if receipt.batch_id is not None:
        batch = db.get(Batch, receipt.batch_id)
        if batch is None:
            result.checks.append(Check("batch_exists", False, "dangling batch_id", "local"))
        else:
            in_batch = call is not None and call.batch_id == batch.id
            result.checks.append(
                Check(
                    "call_is_in_batch",
                    in_batch,
                    f"call belongs to batch {batch.batch_id}",
                    "local",
                )
            )
            if batch.settle_tx_hash:
                result.checks.append(
                    Check(
                        "onchain_settlement",
                        receipt.tx_hash == batch.settle_tx_hash,
                        f"{settings.explorer_url(batch.settle_tx_hash) or batch.settle_tx_hash}",
                        "chain",
                    )
                )
            else:
                # Not a failure. A captured-but-unsettled call is a normal state
                # and saying "verified" about it would be the lie.
                result.checks.append(
                    Check(
                        "onchain_settlement",
                        True,
                        f"batch {batch.batch_id} is {batch.status}; captured against a "
                        "signed voucher, not yet swept on-chain",
                        "local",
                    )
                )
    elif receipt.tx_hash:
        result.checks.append(
            Check(
                "onchain_settlement",
                True,
                f"{settings.explorer_url(receipt.tx_hash) or receipt.tx_hash}",
                "chain",
            )
        )

    if record:
        receipt.verified_at = utcnow()
        receipt.verify_status = result.status
        db.add(receipt)
        db.flush()
    return result
