"""Protocol helpers shared by the CLI commands.

Everything here is a thin adapter over the installed x402 SDK. Where the SDK has
a function, this module calls it rather than reimplementing it:

    payment payload construction   x402.mechanisms.evm.exact.ExactEvmScheme
    EIP-712 signing                x402.mechanisms.evm.signers.EthAccountSigner
    EIP-712 hashing                x402.mechanisms.evm.eip712.hash_eip3009_authorization
    signature recovery             x402.mechanisms.evm.verify.verify_eoa_signature
    MCP _meta encoding             x402.mcp.utils.attach_payment_to_meta
    wire schemas                   x402.schemas.payments.*

The 402 challenge is the one place we build a dict by hand, and `app.cli.doctor`
cross-checks that dict against the SDK's own
`x402.mcp.server._create_payment_required_result` so the hand-built version
cannot drift.

------------------------------------------------------------------------------
WHAT "OFFLINE" MEANS HERE, EXACTLY
------------------------------------------------------------------------------
`OfflineFacilitator` is a real verifier, not a stub, and the distinction is the
whole point of `simulate` being trustworthy:

  IT DOES CHECK   the EIP-712 domain and struct hash, the ECDSA recovery (so a
                  tampered byte fails), payTo, asset, amount, the validAfter /
                  validBefore window, and nonce reuse.
  IT DOES NOT     read the payer's on-chain USDC balance, and it does not submit
                  `transferWithAuthorization`. Both need a network and funds.

So a `simulate` run proves the payload an agent produces is well formed and
correctly signed. It does not prove the payer is solvent. The CLI prints that
sentence rather than hiding behind the word "simulation".
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from x402.mcp.constants import MCP_PAYMENT_META_KEY, MCP_PAYMENT_RESPONSE_META_KEY
from x402.mcp.utils import attach_payment_to_meta
from x402.mechanisms.evm.eip712 import hash_eip3009_authorization
from x402.mechanisms.evm.exact.client import ExactEvmScheme
from x402.mechanisms.evm.types import ExactEIP3009Payload
from x402.mechanisms.evm.utils import get_asset_info, get_evm_chain_id
from x402.mechanisms.evm.verify import verify_eoa_signature
from x402.schemas.payments import PaymentPayload, PaymentRequired, PaymentRequirements, ResourceInfo
from x402.schemas.responses import SettleResponse, VerifyResponse

from app.config import settings
from app.models import Scheme

__all__ = [
    "MCP_PAYMENT_META_KEY",
    "MCP_PAYMENT_RESPONSE_META_KEY",
    "ToolSpec",
    "SimulatedChannel",
    "OfflineFacilitator",
    "Check",
    "tamper_signature",
    "payment_scheme_for",
    "build_requirements",
    "build_challenge",
    "tools_call_request",
    "demo_account",
    "eip712_hash_of",
    "new_id",
    "synthetic_tx_hash",
]


# --------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------


def new_id(prefix: str) -> str:
    """`sess_9f2c...`. Short, sortable enough, and unmistakably ours."""
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def synthetic_tx_hash(seed: str) -> str:
    """A 32-byte hex string that is NOT a transaction hash.

    Used only where a demo or dry run needs the shape of one. Derived from a
    seed rather than random so a `--dry-run` twice in a row prints the same
    plan, and prefixed `0xdead` so nobody pastes it into a block explorer
    expecting a hit.
    """
    digest = hashlib.sha256(f"eraya-brainwave-synthetic:{seed}".encode()).hexdigest()
    return "0xdead" + digest[:60]  # 0x + 64 hex, the width of a real tx hash


# --------------------------------------------------------------------------
# A priced tool, independent of the database
# --------------------------------------------------------------------------


@dataclass
class ToolSpec:
    """Enough of a `Tool` to price a call without a database.

    `simulate` runs on a clean checkout with no rows in it, so it cannot depend
    on `app.models.Tool`. `from_model()` converts one when there is.
    """

    name: str
    resource_url: str = ""
    description: str = ""
    scheme: Scheme = Scheme.EXACT
    price_atomic: int = 2_000  # $0.002
    max_price_atomic: int | None = None
    meter: str | None = None
    price_per_unit_atomic: int | None = None
    max_timeout_seconds: int = 300
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.resource_url:
            # Matches the SDK's own default in create_payment_wrapper.
            self.resource_url = f"mcp://tool/{self.name}"

    @classmethod
    def from_model(cls, tool: Any) -> ToolSpec:
        return cls(
            name=tool.name,
            resource_url=tool.resource_url,
            description=tool.description or "",
            scheme=Scheme(tool.scheme),
            price_atomic=int(tool.price_atomic),
            max_price_atomic=(
                int(tool.max_price_atomic) if tool.max_price_atomic is not None else None
            ),
            meter=tool.meter,
            price_per_unit_atomic=(
                int(tool.price_per_unit_atomic) if tool.price_per_unit_atomic is not None else None
            ),
            max_timeout_seconds=int(tool.max_timeout_seconds),
            tags=[t for t in (tool.tags or "").split(",") if t],
        )

    @property
    def authorized_atomic(self) -> int:
        """What the agent is asked to authorize.

        Under `exact` that is the price. Under `upto` it is the CEILING -- the
        agent signs for the maximum and only actual consumption is captured,
        which is the entire reason the scheme exists.
        """
        if self.scheme is Scheme.UPTO and self.max_price_atomic is not None:
            return self.max_price_atomic
        return self.price_atomic

    def capture_for(self, units: int | None) -> int:
        """What actually gets charged once the tool has run."""
        if self.scheme is not Scheme.UPTO or self.price_per_unit_atomic is None:
            return self.price_atomic
        metered = self.price_atomic + (units or 0) * self.price_per_unit_atomic
        return min(metered, self.authorized_atomic)


# --------------------------------------------------------------------------
# Wire construction
# --------------------------------------------------------------------------


def build_requirements(
    spec: ToolSpec,
    *,
    amount_atomic: int | None = None,
    pay_to: str | None = None,
    network: str | None = None,
    asset: str | None = None,
) -> PaymentRequirements:
    """One entry of the `accepts` array.

    Two details that the spec's README gets wrong and that break real clients:

    * `amount` is a STRING of atomic units. Not "$0.002", not a float, not a
      JSON number. `PaymentRequirements.amount: str` in the installed schema.
    * `network` is CAIP-2 (`eip155:84532`). The v1 spelling `base-sepolia` will
      not match anything in x402 v2.

    `extra` carries the EIP-712 domain (`name`, `version`) for the asset. The
    client can look it up from the SDK's own table when the asset is a known
    USDC, but advertising it means a client that has never heard of this token
    can still sign -- and it is what makes the payload verifiable offline.
    """
    net = network or settings.x402_network
    token = asset or settings.asset_address
    name, version = _eip712_domain_for(net, token)
    return PaymentRequirements(
        scheme=str(spec.scheme),
        network=net,
        asset=token,
        amount=str(amount_atomic if amount_atomic is not None else spec.authorized_atomic),
        payTo=pay_to or settings.pay_to_address,
        maxTimeoutSeconds=spec.max_timeout_seconds,
        extra={"name": name, "version": version},
    )


def _eip712_domain_for(network: str, asset: str) -> tuple[str, str]:
    """Token name/version for the EIP-712 domain separator.

    Prefer the SDK's registered asset table; fall back to configuration. Getting
    this wrong produces a signature that verifies against the wrong domain and
    is rejected by the token contract with no useful error, so it is worth
    taking from the SDK rather than from memory.
    """
    try:
        info = get_asset_info(network, asset)
        return str(info["name"]), str(info.get("version", "1"))
    except Exception:
        return settings.x402_asset_name, settings.x402_asset_version


def build_challenge(
    accepts: list[PaymentRequirements],
    spec: ToolSpec,
    error: str = "Payment Required",
) -> dict[str, Any]:
    """The 402 body a paid MCP tool returns when no payment is attached.

    Shape is fixed by `x402.mcp.server._create_payment_required_result`:
    `{x402Version, accepts, error, resource}`, camelCase, `exclude_none`. Built
    through `PaymentRequired` so pydantic enforces the field names, and pinned
    against the SDK's own private builder by `app.cli.doctor`.

    Over MCP this travels as the tool result's `structuredContent` with
    `isError=true` -- NOT as an HTTP 402 status and NOT in a header. The status
    code 402 never appears on this transport.
    """
    required = PaymentRequired(
        x402Version=2,
        accepts=accepts,
        error=error,
        resource=ResourceInfo(
            url=spec.resource_url,
            description=spec.description or f"Tool: {spec.name}",
            mimeType="application/json",
            serviceName=settings.app_name,
            tags=spec.tags or None,
        ),
    )
    return required.model_dump(by_alias=True, exclude_none=True)


def tools_call_request(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    request_id: int = 1,
    payment: PaymentPayload | None = None,
) -> dict[str, Any]:
    """The literal JSON-RPC frame an MCP client puts on the wire.

    When `payment` is given it is attached with the SDK's own
    `attach_payment_to_meta`, so the `_meta` key and its `exclude_none`
    serialisation are the SDK's, not ours. That matters: the SDK excludes None
    because strict facilitators reject payloads containing explicit nulls.
    """
    params: dict[str, Any] = {"name": tool_name, "arguments": arguments}
    if payment is not None:
        params = attach_payment_to_meta(params, payment)
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params}


# --------------------------------------------------------------------------
# Signing
# --------------------------------------------------------------------------


def demo_account(seed: str = "eraya-brainwave-demo-agent"):
    """A deterministic throwaway EOA.

    Deterministic so a `simulate` run is reproducible and diffable. Derived from
    a public seed string that is printed alongside it, so it is obvious to
    anyone reading the output that this key is worthless and must never be
    funded. It signs authorizations; it never sends a transaction.
    """
    from eth_account import Account

    private_key = "0x" + hashlib.sha256(seed.encode()).hexdigest()
    return Account.from_key(private_key), private_key


def payment_scheme_for(account) -> ExactEvmScheme:
    """The SDK's `exact` client scheme, wrapping an eth_account signer.

    `ExactEvmScheme` auto-wraps a `LocalAccount` in `EthAccountSigner`, so this
    is genuinely one line -- and the signing path is byte for byte the one a
    live agent uses. There is no JavaScript anywhere in it.
    """
    return ExactEvmScheme(account)


def eip712_hash_of(payload: ExactEIP3009Payload, requirements: PaymentRequirements) -> bytes:
    """The 32-byte EIP-712 digest that was signed."""
    name, version = _eip712_domain_for(str(requirements.network), requirements.asset)
    return hash_eip3009_authorization(
        payload.authorization,
        get_evm_chain_id(str(requirements.network)),
        requirements.asset,
        name,
        version,
    )


# --------------------------------------------------------------------------
# Offline facilitator
# --------------------------------------------------------------------------


@dataclass
class Check:
    """One named verification step, so the CLI can print the reasoning rather
    than a bare pass/fail."""

    name: str
    ok: bool
    detail: str = ""


class OfflineFacilitator:
    """Verify and "settle" without a network or funds.

    Implements the same two methods as `x402.http.HTTPFacilitatorClient`
    (`verify` / `settle`) and returns the same `VerifyResponse` /
    `SettleResponse` models, so swapping the real client in is a constructor
    change and nothing else.

    `settle()` returns a synthetic transaction hash and sets
    `error_reason="offline"`. It never claims a real settlement: `success` is
    true because the *protocol* step succeeded, and every caller in this CLI
    prints "no chain was touched" beside it.
    """

    def __init__(self, label: str = "offline") -> None:
        self.label = label
        #: Replay defence. Mirrors the UNIQUE (network, nonce) constraint on
        #: `call`, so the offline run exercises the same rule the database does.
        self.seen_nonces: set[tuple[str, str]] = set()
        self.checks: list[Check] = []

    # -- verify -------------------------------------------------------------

    def verify(
        self, payload: PaymentPayload, requirements: PaymentRequirements
    ) -> tuple[VerifyResponse, list[Check]]:
        """Cryptographically real signature verification. No I/O."""
        checks: list[Check] = []
        raw = payload.payload

        if "authorization" not in raw:
            checks.append(Check("payload shape", False, "not an EIP-3009 payload"))
            return self._invalid("invalid_payload", "expected an EIP-3009 authorization"), checks
        checks.append(Check("payload shape", True, "EIP-3009 transferWithAuthorization"))

        parsed = ExactEIP3009Payload.from_dict(raw)
        auth = parsed.authorization

        # -- scheme / network / asset must match what we advertised ----------
        matched = (
            payload.accepted.scheme == requirements.scheme
            and str(payload.accepted.network) == str(requirements.network)
            and payload.accepted.asset.lower() == requirements.asset.lower()
        )
        checks.append(
            Check(
                "accepted matches",
                matched,
                f"{payload.accepted.scheme} / {payload.accepted.network}",
            )
        )
        if not matched:
            reason = self._invalid("unsupported_scheme", "payload does not match requirements")
            return reason, checks

        # -- recipient -------------------------------------------------------
        to_ok = auth.to.lower() == requirements.pay_to.lower()
        checks.append(Check("payTo", to_ok, auth.to))
        if not to_ok:
            return self._invalid("invalid_exact_evm_payload_recipient_mismatch", auth.to), checks

        # -- amount ----------------------------------------------------------
        value_ok = int(auth.value) == int(requirements.amount)
        checks.append(
            Check("amount", value_ok, f"{auth.value} vs required {requirements.amount} (atomic)")
        )
        if not value_ok:
            bad = self._invalid("invalid_exact_evm_payload_authorization_value", auth.value)
            return bad, checks

        # -- validity window --------------------------------------------------
        now = int(time.time())
        after_ok = int(auth.valid_after) <= now
        before_ok = int(auth.valid_before) > now
        checks.append(
            Check(
                "validity window",
                after_ok and before_ok,
                f"validAfter={auth.valid_after} now={now} validBefore={auth.valid_before}",
            )
        )
        if not (after_ok and before_ok):
            return self._invalid("invalid_exact_evm_payload_authorization_valid_before", ""), checks

        # -- replay ------------------------------------------------------------
        key = (str(requirements.network), auth.nonce)
        fresh = key not in self.seen_nonces
        checks.append(Check("nonce unused", fresh, auth.nonce))
        if not fresh:
            replayed = self._invalid("invalid_exact_evm_payload_authorization_nonce", auth.nonce)
            return replayed, checks

        # -- signature ---------------------------------------------------------
        if not parsed.signature:
            checks.append(Check("signature present", False, ""))
            return self._invalid("invalid_signature", "missing"), checks

        digest = eip712_hash_of(parsed, requirements)
        try:
            sig_ok = verify_eoa_signature(
                digest, bytes.fromhex(parsed.signature.removeprefix("0x")), auth.from_address
            )
        except ValueError as exc:
            checks.append(Check("signature", False, str(exc)))
            return self._invalid("invalid_signature", str(exc)), checks
        checks.append(Check("EIP-712 signature", sig_ok, f"recovers to {auth.from_address}"))
        if not sig_ok:
            return self._invalid("invalid_signature", "recovery mismatch"), checks

        # Reservation happens on success only, exactly like the UNIQUE index:
        # a rejected payload does not burn its nonce.
        self.seen_nonces.add(key)

        checks.append(
            Check(
                "on-chain balance",
                True,
                "NOT CHECKED -- needs a network; the live facilitator does this",
            )
        )
        return VerifyResponse(isValid=True, payer=auth.from_address), checks

    def _invalid(self, reason: str, message: str) -> VerifyResponse:
        return VerifyResponse(isValid=False, invalidReason=reason, invalidMessage=message)

    # -- settle -------------------------------------------------------------

    def settle(self, payload: PaymentPayload, requirements: PaymentRequirements) -> SettleResponse:
        """Return the shape of a settlement without producing one.

        The transaction hash is `synthetic_tx_hash()` -- prefixed `0xdead` so it
        cannot be mistaken for a real one on a block explorer.
        """
        raw = ExactEIP3009Payload.from_dict(payload.payload)
        return SettleResponse(
            success=True,
            transaction=synthetic_tx_hash(raw.authorization.nonce),
            network=str(requirements.network),
            payer=raw.authorization.from_address,
            amount=requirements.amount,
            errorReason="offline",
            errorMessage="settled by the offline simulator; no chain was touched",
        )


# --------------------------------------------------------------------------
# batch-settlement: channel + cumulative voucher
# --------------------------------------------------------------------------
#
# THE POINT OF THE WHOLE SUBMISSION LIVES IN THIS SECTION, so it is worth being
# exact about the mechanism rather than hand-waving "we batch the payments".
#
# `exact` (EIP-3009) cannot be batched. Each authorization is a distinct
# `transferWithAuthorization` and needs its own transaction; the SDK's
# `multicall.py` is a read-side helper (`tryAggregate` over `eth_call`) and does
# not change that. So per-call settlement really is one on-chain event per call,
# and at a $0.002 price with a $0.001 facilitator fee that really is 50% of
# revenue.
#
# `batch-settlement` is the scheme that fixes it, and it is NOT "sum N
# authorizations". It is a payment channel with a monotonic cumulative voucher:
#
#     deposit   ERC-3009 receiveWithAuthorization (or Permit2) opens the channel
#     voucher   per request, the payer signs a HIGHER cumulative ceiling
#     claim     the server submits the latest voucher per channel   (tx 1)
#     settle    claimed funds are swept to the receiver             (tx 2)
#
# Two consequences the ledger has to model, and does:
#   * `Session.authorized_atomic` is a CEILING that rises, not a running sum.
#   * `Batch` carries `claim_tx_hash` AND `settle_tx_hash`, because a batch whose
#     claim landed and whose sweep did not is a real, recoverable state.
#
# Everything below is the SDK's own code (`compute_channel_id`, `sign_voucher`,
# `ClaimPayload`, `SettlePayload`) and runs entirely offline. Only the deposit
# and the two settlement transactions need a chain.


@dataclass
class SimulatedChannel:
    """A batch-settlement channel, signable offline.

    `withdraw_delay` is the payer's protection: after requesting a withdrawal
    they must wait it out, which gives the receiver time to claim outstanding
    vouchers. It is a channel property, not a payment property.
    """

    payer: str
    receiver: str
    token: str
    network: str
    withdraw_delay: int = 86_400
    salt: str = "0x" + "00" * 32
    deposit_atomic: int = 0
    #: The cumulative ceiling signed so far. Monotonic, by construction.
    cumulative_atomic: int = 0
    config: Any = None
    channel_id: str = ""
    vouchers: list[Any] = field(default_factory=list)

    def open(self) -> str:
        from x402.mechanisms.evm.batch_settlement.types import ChannelConfig
        from x402.mechanisms.evm.batch_settlement.utils import compute_channel_id

        self.config = ChannelConfig(
            payer=self.payer,
            # Zero means "the payer signs its own vouchers"; a delegated
            # authorizer address would go here instead.
            payer_authorizer="0x" + "0" * 40,
            receiver=self.receiver,
            receiver_authorizer=self.receiver,
            token=self.token,
            withdraw_delay=self.withdraw_delay,
            salt=self.salt,
        )
        # channelId = EIP-712 hash of the ChannelConfig, bound to chainId and to
        # the batch-settlement contract. Deterministic, so both sides derive the
        # same id without exchanging it.
        self.channel_id = compute_channel_id(self.config, self.network)
        return self.channel_id

    def sign_next_voucher(self, account, increment_atomic: int):
        """Raise the cumulative ceiling by one call's price and sign it.

        Note what is NOT here: a per-call amount. The voucher only ever says
        "you may claim up to X in total". The server's charge for this call is
        `X_new - X_old`, and the on-chain contract enforces that no claim ever
        exceeds the signed ceiling.
        """
        from x402.mechanisms.evm.batch_settlement.client.voucher import sign_voucher
        from x402.mechanisms.evm.signers import EthAccountSigner

        if not self.channel_id:
            raise RuntimeError("channel not opened")
        self.cumulative_atomic += increment_atomic
        if self.cumulative_atomic > self.deposit_atomic:
            raise ValueError(
                f"cumulative {self.cumulative_atomic} exceeds channel deposit "
                f"{self.deposit_atomic} -- the channel needs a top-up"
            )
        voucher = sign_voucher(
            EthAccountSigner(account), self.channel_id, self.cumulative_atomic, self.network
        )
        self.vouchers.append(voucher)
        return voucher

    def voucher_payload(self, voucher) -> dict[str, Any]:
        """The `type=voucher` inner payload for a subsequent request."""
        from x402.mechanisms.evm.batch_settlement.types import VoucherPayload

        payload = VoucherPayload()
        payload.channel_config = self.config
        payload.voucher = voucher
        return payload.to_dict()

    def claim_payload(self, total_claimed_atomic: int | None = None) -> dict[str, Any]:
        """The `type=claim` payload the SERVER sends to the facilitator at close.

        One `VoucherClaim` per channel, carrying only the LATEST voucher --
        which is the entire saving. N calls produce N vouchers off-chain and
        exactly one claim entry on-chain.
        """
        from x402.mechanisms.evm.batch_settlement.types import ClaimPayload, VoucherClaim

        if not self.vouchers:
            raise RuntimeError("nothing to claim")
        latest = self.vouchers[-1]
        claimed = (
            total_claimed_atomic if total_claimed_atomic is not None else self.cumulative_atomic
        )
        return ClaimPayload(
            claims=[
                VoucherClaim(
                    channel=self.config,
                    max_claimable_amount=latest.max_claimable_amount,
                    signature=latest.signature,
                    total_claimed=str(claimed),
                )
            ]
        ).to_dict()

    def settle_payload(self) -> dict[str, Any]:
        """The `type=settle` sweep payload. Step two of two."""
        from x402.mechanisms.evm.batch_settlement.types import SettlePayload

        return SettlePayload(receiver=self.receiver, token=self.token).to_dict()


def tamper_signature(payload: dict[str, Any]) -> dict[str, Any]:
    """Flip one bit inside `s`. Used by `simulate --fail bad-signature`.

    Deliberately NOT the last byte. A 65-byte ECDSA signature is `r || s || v`,
    and corrupting `v` produces "invalid v value" -- a parse error, which proves
    only that the parser works. Corrupting `s` produces a signature that parses
    perfectly and recovers to a DIFFERENT address, which is what an actual
    forgery looks like and is the failure worth demonstrating.
    """
    signature = payload["signature"]
    raw = bytearray.fromhex(signature.removeprefix("0x"))
    raw[40] ^= 0x01  # inside s, well away from v
    out = dict(payload)
    out["signature"] = "0x" + raw.hex()
    return out


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
