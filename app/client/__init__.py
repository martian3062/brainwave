"""THE BUYER SIDE -- a paying MCP client with a spend policy. Pure Python.

    from app.client import Guardian, SpendPolicy, PaidMCPClient, load_signer

    guardian = Guardian(SpendPolicy.from_prices(
        session_budget="$5.00",
        per_call_max="$0.10",
        daily_budget="$50.00",
        escalate_above="$1.00",
        allowlist=["mcp://tool/*"],
        require_receipt=True,
    ))

    async with PaidMCPClient.connect(
        "http://localhost:8000/mcp/",
        guardian=guardian,
        signer=load_signer(),
    ) as client:
        call = await client.call_tool("run_injection_attack_sim", {"target": "..."})
        print(call.trace())
        print(client.session_snapshot())

--------------------------------------------------------------------------
WHAT IS OURS, IN ONE PARAGRAPH
--------------------------------------------------------------------------
The x402 Python SDK already implements paid MCP on both sides. `x402.mcp`
issues the 402 challenge, creates and attaches the payment payload in the MCP
`_meta` key `x402/payment`, and reads the settlement back from
`x402/payment-response`. `x402.mechanisms.evm.signers.EthAccountSigner` signs
EIP-3009 in pure Python -- which is why there is no TypeScript, no Node and no
npm anywhere in this project, buyer side included. None of that is ours and we
do not claim it.

What is ours is the four things the SDK does not have:

  * `guardian.py` -- a stateful buyer-side SPEND policy: session and daily
    budgets, per-call ceilings, resource and network allowlists, human
    escalation, and receipt enforcement. `x402/hook_policy.py` sounds like this
    and is not: it governs which extension may mutate which hook. The SDK's own
    `max_amount` policy is a stateless requirement filter. Nothing upstream can
    say "this call is affordable but it is the last $0.02 of the budget".
  * `verify.py` -- receipt canonicalization, digest, attestation recovery and
    on-chain transfer verification. x402 v2 standardizes `SettleResponse` and
    nothing more; there is no receipt object or attestation format upstream.
  * `shim.py` -- session accounting (authorized vs captured vs settled), and
    the small adapter that makes the SDK's own async MCP client work against a
    streamable-HTTP server at all. Three concrete SDK defects made that
    necessary; all three are documented in `shim.py` and pinned by tests.
  * `__main__.py` -- a CLI, and a stdio proxy so Claude Desktop (which speaks
    stdio and cannot hold a wallet) can use paid remote tools.

--------------------------------------------------------------------------
THE ONE PROPERTY WORTH REMEMBERING
--------------------------------------------------------------------------
An EIP-3009 authorization is a bearer instrument. Policy is therefore evaluated
BEFORE any signature exists -- a payment that was never signed can never be
settled. That ordering is the security property, and
`tests/test_client.py::test_a_denied_call_never_produces_a_signature` fails if
it is ever broken, by counting actual calls to `sign_typed_data`.
"""

from __future__ import annotations

from app.client.guardian import (
    Decision,
    DeclineReason,
    Escalation,
    Guardian,
    SpendDenied,
    SpendJournal,
    SpendPolicy,
    Verdict,
    auto_approve,
    console_approver,
    deny_all,
)
from app.client.signer import AuditingSigner, SignatureRecord, generate_demo_key, load_signer
from app.client.verify import (
    Check,
    CheckStatus,
    ReceiptVerification,
    body_digest,
    canonical_json,
    verify_body_hash,
    verify_receipt,
)

__all__ = [
    # policy
    "Guardian",
    "SpendPolicy",
    "SpendJournal",
    "SpendDenied",
    "Verdict",
    "Decision",
    "DeclineReason",
    "Escalation",
    "auto_approve",
    "deny_all",
    "console_approver",
    # signing
    "AuditingSigner",
    "SignatureRecord",
    "load_signer",
    "generate_demo_key",
    # receipts
    "verify_receipt",
    "ReceiptVerification",
    "Check",
    "CheckStatus",
    "canonical_json",
    "body_digest",
    "verify_body_hash",
    # transport
    "PaidMCPClient",
    "PaidCall",
    "ToolQuote",
]


def __getattr__(name: str):
    """`shim` is imported lazily.

    Importing it pulls in the `mcp` client transport and `web3` through x402's
    EVM schemes, which is a second or so of import time and a hard dependency on
    the transport stack. `guardian`, `signer` and `verify` are useful without
    any of that -- the gateway itself imports `verify.canonical_json` to hash
    receipts, and should not drag an MCP client into the server process to do it.
    """
    if name in {"PaidMCPClient", "PaidCall", "ToolQuote"}:
        from app.client import shim

        return getattr(shim, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
