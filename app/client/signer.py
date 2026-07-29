"""The agent's key, and the audit trail around it. Pure Python -- no JavaScript.

The spec assumed a Next.js/TypeScript buyer shim because that is how the x402
examples are usually written. It is unnecessary. `x402/mechanisms/evm/signers.py`
ships `EthAccountSigner`, which signs EIP-712 (and therefore EIP-3009
`transferWithAuthorization`) on top of `eth-account`. There is no JS, no Node and
no npm anywhere in this project, buyer side included.

--------------------------------------------------------------------------
WHY THERE IS A WRAPPER AROUND THE SDK'S SIGNER
--------------------------------------------------------------------------
`AuditingSigner` adds nothing to the cryptography. It exists because of one
fact about x402's EVM schemes:

    every EIP-3009 authorization this agent will ever produce passes through
    exactly one method -- `ClientEvmSigner.sign_typed_data` --
    exactly once, in `ExactEvmScheme._sign_authorization`
    (and `UptoEvmScheme` via the Permit2 path).

That makes a counter on that method an EXACT count of the bearer instruments
this process has created. Which in turn makes two things possible that are
otherwise a matter of trust:

  1. `tests/test_client.py::test_a_denied_call_never_produces_a_signature`
     can assert `signer.count == 0` after a Guardian denial. Not "the code
     looks like it returns early" -- an actual count of signatures.

  2. `shim.py` can decide whether releasing a budget reservation is SAFE. A
     reservation may only be cancelled if no signature was produced; comparing
     the count before and after payload creation answers that exactly, instead
     of guessing from which exception came back.

It deliberately does NOT forward `read_contract` / `sign_transaction`. Those
methods are how `x402.mechanisms.evm.signer`'s runtime-checkable Protocols
(`ClientEvmSignerWithReadContract`, `ClientEvmSignerWithSignTransaction`) decide
that a signer can participate in Permit2 gas-sponsoring extensions -- and
`isinstance` against a runtime-checkable Protocol is a `hasattr` check, so a
`__getattr__` passthrough would silently re-acquire those capabilities without
the audit wrapper understanding what it was signing. An auditing wrapper that
quietly grows powers is worse than none. Pass `EthAccountSignerWithRPC` through
`AuditingSigner(..., allow_rpc_capabilities=True)` if you genuinely need them.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("brainwave.signer")

__all__ = [
    "AuditingSigner",
    "SignatureRecord",
    "load_signer",
    "generate_demo_key",
    "MainnetRefused",
]

#: CAIP-2 ids that move real money.
MAINNET_NETWORKS = frozenset({"eip155:1", "eip155:8453", "eip155:137", "eip155:42161"})

_HEX_KEY = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")


class MainnetRefused(RuntimeError):
    """Raised rather than sign on mainnet by accident."""


@dataclass(frozen=True, slots=True)
class SignatureRecord:
    """One EIP-712 signature, as it was requested.

    The signature bytes themselves are NOT stored. They are a bearer instrument;
    a debug log that contains one is a debug log that can be replayed. The
    fields kept here are the ones needed to say what was authorized, to whom,
    and for how much.
    """

    at: datetime
    primary_type: str
    domain_name: str
    chain_id: int | None
    verifying_contract: str
    to: str
    value: str
    valid_before: str
    nonce: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "primaryType": self.primary_type,
            "domain": self.domain_name,
            "chainId": self.chain_id,
            "asset": self.verifying_contract,
            "to": self.to,
            "value": self.value,
            "validBefore": self.valid_before,
            "nonce": self.nonce,
        }

    def describe(self) -> str:
        return (
            f"{self.primary_type} value={self.value} to={self.to} "
            f"asset={self.verifying_contract} chain={self.chain_id}"
        )


class AuditingSigner:
    """A `ClientEvmSigner` that records every authorization it produces.

    Implements the SDK's `ClientEvmSigner` protocol exactly: an `address`
    property and `sign_typed_data`. Nothing else, on purpose (see module doc).
    """

    def __init__(
        self,
        inner: Any,
        *,
        on_sign: Callable[[SignatureRecord], None] | None = None,
        allow_rpc_capabilities: bool = False,
    ) -> None:
        self._inner = inner
        self._on_sign = on_sign
        self._signatures: list[SignatureRecord] = []
        if allow_rpc_capabilities:
            # Opt-in only, and loud, because it widens what this key can be
            # asked to sign beyond payment authorizations.
            for name in ("read_contract", "sign_transaction", "get_transaction_count"):
                if hasattr(inner, name):
                    setattr(self, name, getattr(inner, name))
            log.warning(
                "AuditingSigner is forwarding RPC capabilities: this key can now be "
                "asked to sign ERC-20 approvals and raw transactions, not just payments"
            )

    # -------------------------------------------------------------- protocol

    @property
    def address(self) -> str:
        return self._inner.address

    def sign_typed_data(
        self,
        domain: Any,
        types: dict[str, Any],
        primary_type: str,
        message: dict[str, Any],
    ) -> bytes:
        """THE choke point. Every authorization this agent creates comes through here."""
        record = _record_of(domain, primary_type, message)
        signature = self._inner.sign_typed_data(domain, types, primary_type, message)
        # Record only after the inner signer succeeded: a failed signing attempt
        # produces no bearer instrument and must not count as one.
        self._signatures.append(record)
        log.info("SIGNED %s", record.describe())
        if self._on_sign is not None:
            try:
                self._on_sign(record)
            except Exception:
                log.exception("on_sign observer raised; the signature stands")
        return signature

    # --------------------------------------------------------------- audit --

    @property
    def count(self) -> int:
        """How many authorizations this signer has produced. Exact."""
        return len(self._signatures)

    @property
    def signatures(self) -> list[SignatureRecord]:
        return list(self._signatures)

    @property
    def last(self) -> SignatureRecord | None:
        return self._signatures[-1] if self._signatures else None

    def authorized_total(self) -> int:
        """Sum of every `value` ever signed, in atomic units.

        This is the true upper bound on what this process can be made to pay --
        independent of the Guardian's own bookkeeping, and therefore the number
        to compare the Guardian's against when you want to know whether the
        Guardian is telling the truth.
        """
        total = 0
        for record in self._signatures:
            try:
                total += int(record.value)
            except (TypeError, ValueError):
                continue
        return total

    def reset(self) -> None:
        self._signatures.clear()

    def __repr__(self) -> str:  # never, ever include the key
        return f"<AuditingSigner address={self.address} signatures={self.count}>"


def _record_of(domain: Any, primary_type: str, message: dict[str, Any]) -> SignatureRecord:
    """Normalize the two shapes x402 passes as a domain (dataclass or dict)."""
    if isinstance(domain, dict):
        name = domain.get("name", "")
        chain_id = domain.get("chainId")
        verifying = domain.get("verifyingContract", "")
    else:
        name = getattr(domain, "name", "") or ""
        chain_id = getattr(domain, "chain_id", None)
        verifying = getattr(domain, "verifying_contract", "") or ""

    def _s(key: str, *alts: str) -> str:
        for k in (key, *alts):
            if k in message and message[k] is not None:
                v = message[k]
                return "0x" + v.hex() if isinstance(v, (bytes, bytearray)) else str(v)
        return ""

    return SignatureRecord(
        at=datetime.now(UTC),
        primary_type=primary_type,
        domain_name=str(name),
        chain_id=int(chain_id) if chain_id is not None else None,
        verifying_contract=str(verifying),
        to=_s("to", "spender"),
        # `exact`/EIP-3009 calls it `value`; Permit2 nests it under `permitted`.
        value=_s("value", "amount"),
        valid_before=_s("validBefore", "deadline", "sigDeadline"),
        nonce=_s("nonce"),
    )


# --------------------------------------------------------------------------
# Loading a key
# --------------------------------------------------------------------------


def load_signer(
    private_key: str | None = None,
    *,
    network: str = "",
    allow_mainnet: bool = False,
    on_sign: Callable[[SignatureRecord], None] | None = None,
) -> AuditingSigner:
    """Load the agent's key and wrap it for audit.

    `private_key` defaults to `AGENT_WALLET_KEY` from the environment / .env via
    `app.config`. The key is validated for shape before `eth_account` sees it so
    that a truncated paste fails with a sentence instead of a stack trace, and
    it is never logged, never repr'd, and never returned.

    Signing on a mainnet CAIP-2 id requires `allow_mainnet=True`. The default is
    to refuse, because the most expensive bug in this whole design is an agent
    that was pointed at Base mainnet by a stale environment variable and
    happily signed real USDC away.
    """
    if private_key is None:
        from app.config import settings

        private_key = settings.agent_wallet_key
        if not network:
            network = settings.x402_network

    if not private_key:
        raise ValueError(
            "no agent key: set AGENT_WALLET_KEY (a Base Sepolia test key -- never a "
            "funded mainnet key) or pass private_key=... explicitly"
        )

    key = private_key.strip()
    if not _HEX_KEY.match(key):
        raise ValueError(
            "AGENT_WALLET_KEY is not a 32-byte hex private key "
            f"(got {len(key)} characters). Expected 64 hex digits, optionally 0x-prefixed."
        )
    if not key.startswith("0x"):
        key = "0x" + key

    if network in MAINNET_NETWORKS and not allow_mainnet:
        raise MainnetRefused(
            f"refusing to load a signer for {network}: that is mainnet and real USDC is "
            "at risk. Pass allow_mainnet=True if you genuinely mean it."
        )

    from eth_account import Account
    from x402.mechanisms.evm.signers import EthAccountSigner

    account = Account.from_key(key)
    signer = AuditingSigner(EthAccountSigner(account), on_sign=on_sign)
    log.info("agent wallet %s loaded for network %s", signer.address, network or "<unset>")
    return signer


def generate_demo_key() -> tuple[str, str]:
    """A throwaway keypair for tests and demos. Returns `(private_key, address)`.

    Deliberately not seeded and deliberately not persisted: an ephemeral key
    cannot be reused for anything, and a demo that mints one on the fly cannot
    accidentally ship with a real one baked into it.
    """
    from eth_account import Account

    account = Account.create()
    return account.key.hex(), account.address
