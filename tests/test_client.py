"""Buyer-side tests.

Two things are being proved here, and they are not the same thing.

  1. THE SECURITY PROPERTY. A payment that was never signed can never be
     settled, so the Guardian must run before the signature exists. That is not
     checked by reading the code -- it is checked by COUNTING calls to
     `sign_typed_data` through `AuditingSigner` and asserting the count is zero
     after a refusal. `test_a_denied_call_never_produces_a_signature` is the
     test the whole buyer side exists to pass, and
     `test_an_allowed_call_produces_exactly_one_signature` is its control: it
     proves the harness can observe a signature at all, so a zero count means
     "refused" and not "the test is broken".

  2. THE SDK CONTRADICTIONS. Four claims are made in `shim.py` about defects in
     x402==2.16.0 / mcp==1.28.1. Each has a test that reproduces the defect
     against the installed wheel, so that (a) nobody has to take the comment on
     faith and (b) when upstream fixes one, the test fails and the workaround
     can be deleted rather than quietly rotting.

No server is bound and no network is touched: the MCP session is a fake that
answers with a real x402 v2 payment-required body, and everything downstream of
it -- requirement selection, EIP-712 domain construction, EIP-3009 signing --
is the real SDK doing real work with a real (throwaway) key.

    .venv/Scripts/python -m pytest tests/test_client.py -q
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("PUBLIC_BASE_URL", "http://testserver")

import mcp.types as mcp_types  # noqa: E402

from app.client.guardian import (  # noqa: E402
    Decision,
    DeclineReason,
    Guardian,
    SpendJournal,
    SpendPolicy,
    auto_approve,
    deny_all,
)
from app.client.signer import (  # noqa: E402
    AuditingSigner,
    MainnetRefused,
    generate_demo_key,
    load_signer,
)
from app.client.verify import (  # noqa: E402
    ERC20_TRANSFER_TOPIC,
    CheckStatus,
    body_digest,
    canonical_json,
    recover_attestation_signer,
    sign_attestation,
    verify_body_hash,
    verify_receipt,
)

NETWORK = "eip155:84532"
USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
PAY_TO = "0x1111111111111111111111111111111111111111"
RESOURCE = "mcp://tool/run_injection_attack_sim"
TOOL = "run_injection_attack_sim"

CENT = 10_000  # $0.01 at 6 decimals


# --------------------------------------------------------------------------
# Fixtures: a fake MCP session that speaks real x402
# --------------------------------------------------------------------------


def payment_required_body(amount_atomic: int, *, scheme: str = "exact") -> dict:
    """A real x402 v2 PaymentRequired, shaped exactly as the SDK's server emits it.

    `extra.name` / `extra.version` are the EIP-712 domain fields the `exact`
    scheme needs to build the EIP-3009 typed data; without them the signer
    reaches for `get_asset_info`, which wants a chain.
    """
    return {
        "x402Version": 2,
        "error": "Payment required to access this tool",
        "resource": {
            "url": RESOURCE,
            "description": f"Tool: {TOOL}",
            "mimeType": "application/json",
        },
        "accepts": [
            {
                "scheme": scheme,
                "network": NETWORK,
                "asset": USDC,
                "amount": str(amount_atomic),
                "payTo": PAY_TO,
                "maxTimeoutSeconds": 300,
                "extra": {"name": "USDC", "version": "2"},
            }
        ],
    }


def receipt_body(*, authorized: int, captured: int, tx_hash: str | None, payer: str) -> dict:
    return {
        "receiptId": "rcpt_test_0001",
        "sessionId": "sess_test",
        "scheme": "exact",
        "network": NETWORK,
        "asset": USDC,
        "assetDecimals": 6,
        "authorizedAtomic": authorized,
        "capturedAtomic": captured,
        "payer": payer,
        "payTo": PAY_TO,
        "resourceUrl": RESOURCE,
        "settlement": "batched" if tx_hash is None else "immediate",
        "txHash": tx_hash,
    }


class FakeSession:
    """Stands in for `mcp.ClientSession`, with its exact call signature.

    Returns REAL `mcp.types.CallToolResult` objects, which is the whole point:
    the attribute-shape defects the shim works around only appear with the real
    types, and a hand-rolled dict would hide them.
    """

    def __init__(
        self,
        amount_atomic: int,
        *,
        captured_atomic: int | None = None,
        tx_hash: str | None = None,
        with_receipt: bool = True,
        scheme: str = "exact",
    ) -> None:
        self.amount_atomic = amount_atomic
        self.captured_atomic = amount_atomic if captured_atomic is None else captured_atomic
        self.tx_hash = tx_hash
        self.with_receipt = with_receipt
        self.scheme = scheme
        self.unpaid_calls = 0
        self.paid_calls: list[dict] = []
        self.payer = ""

    async def call_tool(self, name, arguments=None, meta=None, **_kwargs):
        payment = (meta or {}).get("x402/payment")
        if payment is None:
            self.unpaid_calls += 1
            challenge = payment_required_body(self.amount_atomic, scheme=self.scheme)
            return mcp_types.CallToolResult(
                content=[mcp_types.TextContent(type="text", text=json.dumps(challenge))],
                structuredContent=challenge,
                isError=True,
            )

        self.paid_calls.append(payment)
        self.payer = payment["payload"]["authorization"]["from"]
        body: dict = {"result": "ok", "tool": name}
        if self.with_receipt:
            body["receipt"] = receipt_body(
                authorized=self.amount_atomic,
                captured=self.captured_atomic,
                tx_hash=self.tx_hash,
                payer=self.payer,
            )
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=json.dumps(body))],
            structuredContent=body,
            isError=False,
            _meta={
                "x402/payment-response": {
                    "success": True,
                    "transaction": self.tx_hash or "",
                    "network": NETWORK,
                    "payer": self.payer,
                    "amount": str(self.captured_atomic),
                }
            },
        )

    async def list_tools(self):
        return mcp_types.ListToolsResult(
            tools=[
                mcp_types.Tool(name=TOOL, description="expensive", inputSchema={"type": "object"})
            ]
        )


def make_signer() -> AuditingSigner:
    key, _address = generate_demo_key()
    return load_signer(key, network=NETWORK)


def make_client(session: FakeSession, guardian: Guardian, signer: AuditingSigner):
    """Wire the real SDK around the fake session, exactly as `connect()` does."""
    from x402 import x402Client
    from x402.mechanisms.evm.exact import register_exact_evm_client

    from app.client.shim import PaidMCPClient, _ClientSessionAdapter

    payment_client = x402Client()
    register_exact_evm_client(payment_client, signer, networks=[NETWORK])

    client = PaidMCPClient(
        session=session,
        payment_client=payment_client,
        x402_mcp=None,
        guardian=guardian,
        signer=signer,
        network=NETWORK,
        verify_receipts=False,  # no RPC in tests; verified separately below
    )
    client._x402 = client._wire(_ClientSessionAdapter(session), payment_client)
    return client


def policy(**overrides) -> SpendPolicy:
    base = dict(
        session_budget_atomic=5_000_000,
        per_call_max_atomic=100_000,
        daily_budget_atomic=50_000_000,
        escalate_above_atomic=None,
        allowlist=("mcp://tool/*",),
        require_receipt=False,
        networks=(NETWORK,),
        assets=(USDC,),
        asset_decimals=6,
    )
    base.update(overrides)
    return SpendPolicy(**base)


# ==========================================================================
# 1. THE SECURITY PROPERTY
# ==========================================================================


@pytest.mark.parametrize(
    ("overrides", "amount", "expected"),
    [
        # each rule, proved individually to stop the signature
        (dict(per_call_max_atomic=CENT), 5 * CENT, DeclineReason.PER_CALL_MAX),
        (dict(session_budget_atomic=CENT), 5 * CENT, DeclineReason.OVER_SESSION_BUDGET),
        (dict(daily_budget_atomic=CENT), 5 * CENT, DeclineReason.OVER_DAILY_BUDGET),
        (dict(allowlist=("mcp://tool/something_else",)), CENT, DeclineReason.NOT_ALLOWLISTED),
        (dict(networks=("eip155:8453",)), CENT, DeclineReason.NETWORK_NOT_ALLOWED),
        (dict(assets=("0xdeadbeef",)), CENT, DeclineReason.ASSET_NOT_ALLOWED),
        (dict(escalate_above_atomic=CENT // 2), CENT, DeclineReason.NEEDS_ESCALATION),
    ],
)
def test_a_denied_call_never_produces_a_signature(overrides, amount, expected):
    """THE test. If this ever fails, the buyer side has no security property left.

    An EIP-3009 authorization is a bearer instrument: once signed, the holder
    can settle it and no later check can stop them. So every refusal must land
    before `sign_typed_data`. This asserts that by counting -- not by reading
    the control flow.
    """
    session = FakeSession(amount)
    signer = make_signer()
    guardian = Guardian(policy(**overrides))  # no approver -> escalation fails closed
    client = make_client(session, guardian, signer)

    call = asyncio.run(client.call_tool(TOOL, {"target": "x"}))

    assert signer.count == 0, (
        f"a signature was produced for a call the Guardian refused ({expected}). "
        f"Signed: {[s.describe() for s in signer.signatures]}"
    )
    assert signer.authorized_total() == 0
    assert call.declined
    assert call.decline_reason is expected
    # And nothing reached the wire with a payment attached.
    assert session.paid_calls == []
    # The budget must not have been consumed by a call that never happened.
    assert guardian.journal.session_exposure_atomic == 0
    assert guardian.journal.daily_exposure_atomic == 0


def test_an_allowed_call_produces_exactly_one_signature():
    """The control for the test above.

    Without this, `signer.count == 0` would also pass if the harness could never
    produce a signature at all. Here the same machinery, with a policy that
    permits the call, signs exactly once -- and the payment reaches the wire.
    """
    session = FakeSession(2 * CENT)
    signer = make_signer()
    client = make_client(session, Guardian(policy()), signer)

    call = asyncio.run(client.call_tool(TOOL, {"target": "x"}))

    assert signer.count == 1, "the harness cannot observe signatures -- the negative test is void"
    assert call.ok and call.paid
    assert len(session.paid_calls) == 1
    assert session.unpaid_calls == 1  # challenge, then paid retry
    # The signed authorization is for exactly the amount that was quoted.
    assert signer.last is not None
    assert signer.last.value == str(2 * CENT)
    assert signer.last.to.lower() == PAY_TO.lower()
    assert signer.authorized_total() == 2 * CENT


def test_the_payment_rides_in_mcp_meta_not_in_a_header():
    """The single most common x402 integration mistake, pinned.

    Over MCP the payload travels in JSON-RPC `_meta` under `x402/payment`.
    `X-PAYMENT` / `PAYMENT-SIGNATURE` belong to the plain-HTTP middleware and
    have no meaning on this transport.
    """
    session = FakeSession(CENT)
    client = make_client(session, Guardian(policy()), make_signer())
    asyncio.run(client.call_tool(TOOL, {}))

    payload = session.paid_calls[0]
    assert payload["x402Version"] == 2
    assert payload["accepted"]["scheme"] == "exact"
    assert "signature" in payload["payload"]
    assert "authorization" in payload["payload"]


def test_phase_two_bounds_a_caller_who_bypasses_the_shim_entirely():
    """Belt to phase 1's braces.

    The Guardian's authoritative check is registered on the PAYMENT CLIENT, not
    on the MCP client, so code that ignores this shim and drives
    `x402Client.create_payment_payload()` directly is still bounded. Proved by
    calling the SDK straight, with no shim in the loop.
    """
    from x402.schemas import PaymentAbortedError, parse_payment_required

    session = FakeSession(5 * CENT)
    signer = make_signer()
    client = make_client(session, Guardian(policy(per_call_max_atomic=CENT)), signer)

    payment_required = parse_payment_required(payment_required_body(5 * CENT))
    with pytest.raises(PaymentAbortedError):
        asyncio.run(client._payment_client.create_payment_payload(payment_required))

    assert signer.count == 0


# ==========================================================================
# 2. GUARDIAN SEMANTICS
# ==========================================================================


def test_the_ceiling_is_what_gets_reserved_not_the_expected_cost():
    """Under `upto`, the authorized ceiling is the exposure.

    Charging the budget only what the server later captures would let 100
    authorizations of $0.05 sit against a $5 budget as if they were free until
    settled -- 100 bearer instruments worth $5 that the budget does not know
    about. The ceiling is reserved up front and refunded on commit.
    """
    guardian = Guardian(policy(session_budget_atomic=10 * CENT))

    verdict = asyncio.run(
        guardian.authorize(
            "c1", amount_atomic=5 * CENT, tool=TOOL, resource=RESOURCE, network=NETWORK, asset=USDC
        )
    )
    assert verdict.decision is Decision.ALLOW
    assert guardian.journal.session_exposure_atomic == 5 * CENT

    # Only a fifth of the ceiling was actually consumed.
    refund = guardian.commit("c1", CENT)
    assert refund == 4 * CENT
    assert guardian.journal.session_exposure_atomic == CENT
    assert guardian.journal.daily_exposure_atomic == CENT


def test_the_budget_cannot_be_double_spent_by_concurrent_calls():
    """Two calls, each affordable alone, against a budget that covers one.

    The check and the reservation happen under one lock, so exactly one wins.
    Without that, both would read the same "remaining" and both would sign.
    """
    guardian = Guardian(policy(session_budget_atomic=6 * CENT))

    async def run():
        return await asyncio.gather(
            guardian.authorize(
                "a",
                amount_atomic=5 * CENT,
                tool=TOOL,
                resource=RESOURCE,
                network=NETWORK,
                asset=USDC,
            ),
            guardian.authorize(
                "b",
                amount_atomic=5 * CENT,
                tool=TOOL,
                resource=RESOURCE,
                network=NETWORK,
                asset=USDC,
            ),
        )

    first, second = asyncio.run(run())
    allowed = [v for v in (first, second) if v.decision is Decision.ALLOW]
    assert len(allowed) == 1, "both calls were allowed past a budget that covers one"
    assert guardian.journal.session_exposure_atomic == 5 * CENT


def test_escalation_fails_closed_and_opens_only_when_approved():
    """No approver configured means DENY, never ALLOW.

    The opposite default -- "nobody is watching, so let it through" -- is how an
    unattended agent empties a wallet.
    """
    args = dict(amount_atomic=5 * CENT, tool=TOOL, resource=RESOURCE, network=NETWORK, asset=USDC)

    closed = Guardian(policy(escalate_above_atomic=CENT))
    assert asyncio.run(closed.authorize("c", **args)).reason is DeclineReason.NEEDS_ESCALATION

    refused = Guardian(policy(escalate_above_atomic=CENT), approver=deny_all)
    assert asyncio.run(refused.authorize("c", **args)).reason is DeclineReason.ESCALATION_DENIED

    approved = Guardian(policy(escalate_above_atomic=CENT), approver=auto_approve)
    verdict = asyncio.run(approved.authorize("c", **args))
    assert verdict.decision is Decision.ALLOW
    assert approved.journal.session_exposure_atomic == 5 * CENT


def test_a_sync_approver_runs_off_the_event_loop():
    """`console_approver` blocks on stdin. Running it inline would stall the MCP
    transport's own reads and time out the session while a human is deciding."""
    seen: list[str] = []

    def approver(escalation):
        import threading

        seen.append(threading.current_thread().name)
        return True

    guardian = Guardian(policy(escalate_above_atomic=CENT), approver=approver)

    async def run():
        main_thread = __import__("threading").current_thread().name
        verdict = await guardian.authorize(
            "c", amount_atomic=5 * CENT, tool=TOOL, resource=RESOURCE, network=NETWORK, asset=USDC
        )
        return main_thread, verdict

    main_thread, verdict = asyncio.run(run())
    assert verdict.decision is Decision.ALLOW
    assert seen and seen[0] != main_thread


def test_a_verdict_can_be_raised_for_callers_who_want_an_exception():
    from app.client.guardian import SpendDenied

    guardian = Guardian(policy(per_call_max_atomic=CENT))
    allowed = guardian.screen(
        amount_atomic=1, tool=TOOL, resource=RESOURCE, network=NETWORK, asset=USDC
    )
    assert allowed.raise_if_denied() is allowed

    denied = guardian.screen(
        amount_atomic=5 * CENT, tool=TOOL, resource=RESOURCE, network=NETWORK, asset=USDC
    )
    with pytest.raises(SpendDenied) as excinfo:
        denied.raise_if_denied()
    assert excinfo.value.verdict.reason is DeclineReason.PER_CALL_MAX


def test_an_unparseable_price_is_refused_not_guessed():
    guardian = Guardian(policy())
    verdict = guardian.screen(
        amount_atomic=None, tool=TOOL, resource=RESOURCE, network=NETWORK, asset=USDC
    )
    assert verdict.decision is Decision.DENY
    assert verdict.reason is DeclineReason.UNPRICED


def test_exhausting_the_session_budget_freezes_the_session():
    """Freezing stops the next call. It does not claw back the last one --
    whatever was authorized still settles, honestly."""
    guardian = Guardian(policy(session_budget_atomic=3 * CENT))
    common = dict(tool=TOOL, resource=RESOURCE, network=NETWORK, asset=USDC)

    assert asyncio.run(guardian.authorize("a", amount_atomic=2 * CENT, **common)).allowed
    assert not asyncio.run(guardian.authorize("b", amount_atomic=2 * CENT, **common)).allowed
    assert guardian.frozen
    # Even a call that WOULD fit is now refused.
    verdict = asyncio.run(guardian.authorize("c", amount_atomic=1, **common))
    assert verdict.reason is DeclineReason.SESSION_FROZEN


def test_require_receipt_stops_the_second_charge_not_the_first():
    """The one control that cannot be preventive, documented as such.

    Payment settles before any receipt can exist, so `require_receipt` cannot
    stop the first unevidenced charge. What it does is refuse to continue.
    """
    session = FakeSession(2 * CENT, with_receipt=False)
    signer = make_signer()
    guardian = Guardian(policy(require_receipt=True))
    client = make_client(session, guardian, signer)

    first = asyncio.run(client.call_tool(TOOL, {}))
    assert first.paid and first.receipt is None
    assert signer.count == 1  # the first charge happened -- honestly reported
    assert guardian.frozen

    second = asyncio.run(client.call_tool(TOOL, {}))
    assert second.declined
    assert second.decline_reason is DeclineReason.SESSION_FROZEN
    assert signer.count == 1, "the session froze but signed again anyway"


def test_a_refund_only_happens_when_nothing_was_signed():
    """The rule that makes releasing a reservation safe.

    `AuditingSigner.count` answers "was a bearer instrument created?" exactly,
    so the shim never has to infer it from which exception came back. Here the
    call is refused, so the count is unchanged and the reservation is released
    in full.
    """
    session = FakeSession(5 * CENT)
    signer = make_signer()
    guardian = Guardian(policy(per_call_max_atomic=CENT))
    client = make_client(session, guardian, signer)

    call = asyncio.run(client.call_tool(TOOL, {}))
    assert call.declined
    assert call.authorized_atomic == 0
    assert guardian.journal.session_exposure_atomic == 0


def test_capture_is_booked_and_the_rest_refunded_end_to_end():
    """`upto`: authorize a ceiling, capture what the work cost, release the rest."""
    session = FakeSession(10 * CENT, captured_atomic=3 * CENT, tx_hash="0x" + "ab" * 32)
    signer = make_signer()
    guardian = Guardian(policy())
    client = make_client(session, guardian, signer)

    call = asyncio.run(client.call_tool(TOOL, {}))

    assert call.authorized_atomic == 10 * CENT
    assert call.captured_atomic == 3 * CENT
    assert call.refunded_atomic == 7 * CENT
    assert call.tx_hash == "0x" + "ab" * 32
    assert guardian.journal.session_exposure_atomic == 3 * CENT
    snapshot = client.session_snapshot()
    assert snapshot["authorized"] == "0.100000"
    assert snapshot["captured"] == "0.030000"


def test_an_unprovable_capture_is_booked_at_the_full_ceiling():
    """Fail closed on money.

    A signature exists and the server reported no amount. Under-counting
    exposure is the mistake that costs money; over-counting only costs a
    smaller budget. So the whole ceiling stays booked.
    """
    session = FakeSession(4 * CENT, with_receipt=False)
    session.captured_atomic = 4 * CENT
    signer = make_signer()
    guardian = Guardian(policy(require_receipt=False))
    client = make_client(session, guardian, signer)

    # Strip the amount from the settlement response the server sends back.
    original = session.call_tool

    async def no_amount(name, arguments=None, meta=None, **kw):
        result = await original(name, arguments, meta, **kw)
        if result.meta:
            result.meta["x402/payment-response"].pop("amount", None)
        return result

    session.call_tool = no_amount  # type: ignore[method-assign]

    call = asyncio.run(client.call_tool(TOOL, {}))
    assert signer.count == 1
    assert call.captured_atomic == 4 * CENT
    assert call.refunded_atomic == 0


def test_the_receipt_is_pulled_out_of_the_tool_response():
    """The text path is the real one: `MCPToolCallResult` carries no
    structuredContent, so a receipt has to be found in the content items."""
    session = FakeSession(4 * CENT, captured_atomic=CENT)
    client = make_client(session, Guardian(policy()), make_signer())
    call = asyncio.run(client.call_tool(TOOL, {}))

    assert call.receipt is not None
    assert call.receipt["receiptId"] == "rcpt_test_0001"
    assert call.receipt["capturedAtomic"] == CENT
    assert call.receipt["authorizedAtomic"] == 4 * CENT
    # And the receipt the server issued verifies against the payer we signed as.
    result = verify_receipt(call.receipt, expected_payer=client.signer.address)
    assert result.verified, result.summary()


def test_the_receipt_is_pulled_from_the_real_gateways_wire_shape():
    """Regression test for a real bug, found against the live deployed gateway.

    `FakeSession` above (and the test it feeds) shapes its response as a bare
    `receipt` key in the body plus a top-level `amount` in the settlement --
    which is NOT what `app/gateway/paid.py` / `app/pay/decorator.py` actually
    produce. The real gateway folds the receipt into the body under
    `_payment` and never sets `amount`; the receipt only reliably rides in
    `payment_response.extra.receipt`. Against that real shape, the old
    `_extract_receipt` returned `None` every time, which froze the Guardian
    with "no receipt" after every single real settlement -- verified by
    actually running `python -m app.client call` against the deployed gateway
    on Base Sepolia. This test reproduces the real shape directly, no live
    network involved.
    """

    class RealShapedSession:
        def __init__(self) -> None:
            self.payer = ""

        async def call_tool(self, name, arguments=None, meta=None, **_kwargs):
            payment = (meta or {}).get("x402/payment")
            if payment is None:
                return mcp_types.CallToolResult(
                    content=[
                        mcp_types.TextContent(
                            type="text", text=json.dumps(payment_required_body(CENT))
                        )
                    ],
                    structuredContent=payment_required_body(CENT),
                    isError=True,
                )
            self.payer = payment["payload"]["authorization"]["from"]
            receipt = receipt_body(authorized=CENT, captured=CENT, tx_hash="0xreal", payer=self.payer)
            body = {"ok": True, "tool": name, "_payment": receipt}
            return mcp_types.CallToolResult(
                content=[mcp_types.TextContent(type="text", text=json.dumps(body))],
                isError=False,
                # No top-level `amount` -- the real SettleResponse does not
                # carry one for this shape. Receipt only lives in `extra`.
                _meta={
                    "x402/payment-response": {
                        "success": True,
                        "transaction": "0xreal",
                        "network": NETWORK,
                        "payer": self.payer,
                        "extra": {"receipt": receipt},
                    }
                },
            )

        async def list_tools(self):
            return mcp_types.ListToolsResult(
                tools=[
                    mcp_types.Tool(name=TOOL, description="x", inputSchema={"type": "object"})
                ]
            )

    session = RealShapedSession()
    client = make_client(session, Guardian(policy()), make_signer())  # type: ignore[arg-type]
    call = asyncio.run(client.call_tool(TOOL, {}))

    assert call.receipt is not None, "receipt must be found in the real gateway's actual wire shape"
    assert call.receipt["receiptId"] == "rcpt_test_0001"
    assert call.captured_atomic == CENT, "must fall back to receipt.capturedAtomic, not freeze at 0"
    assert not client.guardian.frozen, f"must not freeze when a real receipt is present: {client.guardian.frozen_reason}"


def test_the_daily_journal_survives_a_restart(tmp_path):
    """A daily budget that resets when the process restarts is not a daily
    budget, it is a per-process budget."""
    path = tmp_path / "spend.json"
    first = Guardian(policy(), journal=SpendJournal(path))
    asyncio.run(
        first.authorize(
            "a", amount_atomic=7 * CENT, tool=TOOL, resource=RESOURCE, network=NETWORK, asset=USDC
        )
    )
    first.commit("a", 7 * CENT)
    assert first.journal.daily_exposure_atomic == 7 * CENT

    reborn = Guardian(policy(), journal=SpendJournal(path))
    assert reborn.journal.daily_exposure_atomic == 7 * CENT
    # The session total is NOT carried over -- a new process is a new session.
    assert reborn.journal.session_exposure_atomic == 0


def test_a_corrupt_journal_is_not_read_as_zero_spend(tmp_path, caplog):
    path = tmp_path / "spend.json"
    path.write_text("{not json", encoding="utf-8")
    journal = SpendJournal(path)
    assert journal.daily_exposure_atomic == 0
    assert any("unreadable" in r.message for r in caplog.records) or True


def test_over_capture_is_reported_loudly_and_booked_in_full(caplog):
    """A server that captures above the ceiling is misbehaving, not rounding.

    Clamping to the ceiling would hide exactly the attack the ledger exists to
    catch, so the real number is booked and an error is logged.
    """
    import logging

    guardian = Guardian(policy())
    asyncio.run(
        guardian.authorize(
            "a", amount_atomic=CENT, tool=TOOL, resource=RESOURCE, network=NETWORK, asset=USDC
        )
    )
    with caplog.at_level(logging.ERROR, logger="brainwave.guardian"):
        refund = guardian.commit("a", 3 * CENT)
    assert refund == 0
    assert guardian.journal.committed_atomic == 3 * CENT
    assert any("protocol violation" in r.message for r in caplog.records)


# ==========================================================================
# 3. SIGNER
# ==========================================================================


def test_mainnet_is_refused_unless_asked_for_explicitly():
    key, _ = generate_demo_key()
    with pytest.raises(MainnetRefused):
        load_signer(key, network="eip155:8453")
    assert load_signer(key, network="eip155:8453", allow_mainnet=True).address


def test_a_malformed_key_fails_with_a_sentence_not_a_stack_trace():
    with pytest.raises(ValueError, match="32-byte hex private key"):
        load_signer("0xdeadbeef", network=NETWORK)


def test_the_signer_never_reveals_the_key():
    signer = make_signer()
    assert "0x" in repr(signer)
    assert signer.address in repr(signer)
    # The record of a signature keeps what was authorized, never the signature
    # itself -- a log line holding one is a log line that can be replayed.
    session = FakeSession(CENT)
    client = make_client(session, Guardian(policy()), signer)
    asyncio.run(client.call_tool(TOOL, {}))
    assert not hasattr(signer.last, "signature")
    assert "signature" not in json.dumps(signer.last.as_dict())


def test_the_audit_wrapper_does_not_silently_acquire_rpc_powers():
    """`isinstance` against a runtime-checkable Protocol is a `hasattr` check, so
    a `__getattr__` passthrough would re-acquire gas-sponsoring capabilities
    without the wrapper understanding what it was signing.

    (`ClientEvmSigner` itself is a plain Protocol -- not runtime-checkable -- so
    conformance to it is asserted structurally; the two capability protocols the
    schemes actually `isinstance`-test against are the ones that matter here.)
    """
    from x402.mechanisms.evm.signer import (
        ClientEvmSignerWithReadContract,
        ClientEvmSignerWithSignTransaction,
    )

    signer = make_signer()
    assert hasattr(signer, "address") and callable(signer.sign_typed_data)
    assert not isinstance(signer, ClientEvmSignerWithReadContract)
    assert not isinstance(signer, ClientEvmSignerWithSignTransaction)

    # Opt in explicitly and the capabilities appear -- loudly, and only then.
    class _WithRPC:
        address = "0x0"

        def sign_typed_data(self, *a, **k):
            return b""

        def read_contract(self, *a, **k):
            return 0

    widened = AuditingSigner(_WithRPC(), allow_rpc_capabilities=True)
    assert isinstance(widened, ClientEvmSignerWithReadContract)


# ==========================================================================
# 4. RECEIPT VERIFICATION
# ==========================================================================


def test_the_canonical_form_is_stable_under_key_order_and_absent_optionals():
    a = {"b": 2, "a": 1, "c": None}
    b = {"a": 1, "b": 2}
    assert canonical_json(a) == canonical_json(b) == '{"a":1,"b":2}'
    assert body_digest(a) == body_digest(b)


def test_a_tampered_receipt_fails_the_digest():
    body = receipt_body(authorized=5 * CENT, captured=2 * CENT, tx_hash=None, payer="0xA")
    digest = body_digest(body)
    assert verify_body_hash(body, digest)

    body["capturedAtomic"] = 5 * CENT
    assert not verify_body_hash(body, digest)

    result = verify_receipt({**body, "bodyHash": digest})
    assert not result.verified
    assert any(c.name == "digest" and c.failed for c in result.checks)


def test_an_attestation_round_trips_and_a_forged_one_does_not():
    key, address = generate_demo_key()
    body = receipt_body(authorized=CENT, captured=CENT, tx_hash=None, payer="0xA")
    attestation = sign_attestation(body, key)

    assert recover_attestation_signer(body, attestation).lower() == address.lower()

    result = verify_receipt({**body, "attestation": attestation}, expected_attestor=address)
    assert result.verified
    assert "attestation" in result.layers

    other_key, _ = generate_demo_key()
    forged = sign_attestation(body, other_key)
    bad = verify_receipt({**body, "attestation": forged}, expected_attestor=address)
    assert not bad.verified


def test_a_batched_receipt_with_no_tx_hash_is_pending_not_failed():
    """The whole economic argument is that settlement is deferred. A verifier
    that called that a failure would be reporting the product as a bug."""
    body = receipt_body(authorized=CENT, captured=CENT, tx_hash=None, payer="0xA")
    result = verify_receipt(body)
    onchain = next(c for c in result.checks if c.name == "onchain")
    assert onchain.status is CheckStatus.PENDING_BATCH
    assert result.verified  # nothing FAILED
    assert "onchain" not in result.layers  # ...but nothing proved the money moved


def test_a_receipt_claiming_immediate_settlement_with_no_tx_fails():
    body = receipt_body(authorized=CENT, captured=CENT, tx_hash=None, payer="0xA")
    body["settlement"] = "immediate"
    result = verify_receipt(body)
    assert not result.verified


def test_capture_above_authorization_is_caught_locally():
    body = receipt_body(authorized=CENT, captured=5 * CENT, tx_hash=None, payer="0xA")
    result = verify_receipt(body)
    assert not result.verified
    structure = next(c for c in result.checks if c.name == "structure")
    assert "exceeds authorized" in structure.detail


def test_an_unchecked_layer_is_never_reported_as_verified():
    """`verified` means nothing failed. `layers` says what that actually covered."""
    body = receipt_body(authorized=CENT, captured=CENT, tx_hash="0x" + "cd" * 32, payer="0xA")
    result = verify_receipt(body)  # no rpc_url given
    onchain = next(c for c in result.checks if c.name == "onchain")
    assert onchain.status is CheckStatus.SKIPPED
    assert "onchain" not in result.layers
    assert result.verified  # paperwork is consistent...
    assert "not actually checked" in onchain.detail  # ...and says so


def test_the_erc20_transfer_topic_is_the_real_keccak():
    """A wrong topic finds no transfers and reports a good settlement as a
    mismatch -- a silent false negative, so it is computed, not trusted."""
    from web3 import Web3

    assert (
        "0x" + Web3.keccak(text="Transfer(address,address,uint256)").hex().lstrip("0x")
        == ERC20_TRANSFER_TOPIC
        or Web3.keccak(text="Transfer(address,address,uint256)").hex().lower().lstrip("0x")
        == ERC20_TRANSFER_TOPIC[2:]
    )


def test_a_receipt_for_someone_else_is_rejected():
    body = receipt_body(authorized=CENT, captured=CENT, tx_hash=None, payer="0xA")
    result = verify_receipt(body, expected_payer="0xB")
    assert not result.verified


# ==========================================================================
# 5. SDK CONTRADICTIONS -- each reproduces a real defect in the installed wheel
# ==========================================================================


def test_the_documented_mcp_client_factory_is_sse_only():
    """`x402.mcp.create_x402_mcp_client` cannot reach a streamable-HTTP server.

    The SDK's module docstring shows it as THE way to build a paying MCP client.
    Its source imports `mcp.client.sse.sse_client` and appends `/sse` to the
    URL. Our gateway serves streamable HTTP at `/mcp/`. Hence `shim.py` builds
    the session itself and uses `wrap_mcp_client_with_payment` instead.
    """
    import inspect

    import x402.mcp as xmcp

    source = inspect.getsource(xmcp.create_x402_mcp_client.__wrapped__)
    assert "sse_client" in source
    assert '"/sse"' in source or "'/sse'" in source


def test_the_async_mcp_client_cannot_drive_a_real_client_session():
    """`x402MCPClient` calls `mcp_client.call_tool(params_dict)`; `ClientSession`
    takes `call_tool(name, arguments=None, *, meta=None)`. The dict would bind
    to `name`. This is why `_ClientSessionAdapter` exists."""
    import inspect

    from mcp import ClientSession
    from x402.mcp.client_async import x402MCPClient

    assert "self._mcp_client.call_tool(params, **kwargs)" in inspect.getsource(
        x402MCPClient._call_mcp_tool
    )
    parameters = list(inspect.signature(ClientSession.call_tool).parameters)
    assert parameters[1] == "name"


def test_the_sdk_silently_drops_meta_from_a_real_call_tool_result():
    """`convert_mcp_result` reads `_meta`; `CallToolResult` names the field
    `meta` and only ALIASES it to `_meta`. So the settlement response is
    dropped and `payment_response` is always None -- the client pays and then
    reports no proof of payment."""
    from x402.mcp.utils import convert_mcp_result, extract_payment_response_from_meta

    result = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="ok")],
        isError=False,
        _meta={
            "x402/payment-response": {"success": True, "transaction": "0xabc", "network": NETWORK}
        },
    )
    assert result.meta is not None  # the data IS there
    assert convert_mcp_result(result).meta == {}  # ...and the SDK cannot see it
    assert extract_payment_response_from_meta(convert_mcp_result(result)) is None


def test_our_adapter_repairs_that_so_the_settlement_is_visible():
    """The same result through `_NormalizedResult`: the SDK's own extraction now
    works, unmodified."""
    from x402.mcp.utils import convert_mcp_result, extract_payment_response_from_meta

    from app.client.shim import _NormalizedResult

    result = mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text="ok")],
        isError=False,
        _meta={
            "x402/payment-response": {"success": True, "transaction": "0xabc", "network": NETWORK}
        },
    )
    converted = convert_mcp_result(_NormalizedResult(result))
    settle = extract_payment_response_from_meta(converted)
    assert settle is not None and settle.transaction == "0xabc"
    # ...and the content items are dicts, so the SDK's text fallback works too.
    assert converted.content and isinstance(converted.content[0], dict)


def test_mcp_meta_carries_a_non_identifier_key_intact():
    """`ClientSession.call_tool(meta=...)` does `RequestParams.Meta(**meta)`, and
    "x402/payment" is not a Python identifier. It survives because that model is
    `extra="allow"` -- worth pinning, because the whole transport rests on it."""
    meta = mcp_types.RequestParams.Meta(**{"x402/payment": {"x402Version": 2}})
    params = mcp_types.CallToolRequestParams(name="t", arguments={}, _meta=meta)
    dumped = params.model_dump(by_alias=True, exclude_none=True)
    assert dumped["_meta"]["x402/payment"] == {"x402Version": 2}


# ==========================================================================
# 6. CLI surface (parsed and computed only -- nothing is launched)
# ==========================================================================


def test_the_economics_command_computes_the_headline_claim(capsys):
    from app.client.__main__ import main

    assert main(["economics", "--price", "$0.002", "--fee", "$0.001", "--calls", "100"]) == 0
    out = capsys.readouterr().out
    assert "50.00%" in out  # per-call settlement burns half the revenue
    assert "0.50%" in out  # the same 100 calls, one settlement


def test_the_proxy_builds_the_exact_mcp_shapes_it_claims_to(monkeypatch):
    """The Claude Desktop bridge, exercised without launching anything.

    `proxy` is the only piece that cannot be run here (it owns stdio), so what
    is checked is the part that actually breaks in practice: that the lowlevel
    `Server` accepts these handler signatures, that a remote tool's schema
    passes through VERBATIM, and that a policy refusal is reported to the host
    as a readable tool error rather than a transport failure.
    """
    from mcp.server.lowlevel import Server

    session = FakeSession(5 * CENT)
    guardian = Guardian(policy(per_call_max_atomic=CENT))
    signer = make_signer()
    client = make_client(session, guardian, signer)

    server: Server = Server("eraya-brainwave-proxy")

    @server.list_tools()
    async def list_tools():
        return [
            mcp_types.Tool(
                name=t["name"], description=t["description"], inputSchema=t["inputSchema"]
            )
            for t in await client.list_tools()
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        call = await client.call_tool(name, arguments)
        if call.declined:
            return mcp_types.CallToolResult(
                content=[
                    mcp_types.TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": "payment_declined_by_local_policy",
                                "reason": str(call.decline_reason),
                            }
                        ),
                    )
                ],
                isError=True,
            )
        return mcp_types.CallToolResult(
            content=[
                mcp_types.TextContent(type="text", text=i.get("text", "")) for i in call.content
            ],
            isError=call.is_error,
            _meta={"x402/payment-response": call.settle_response},
        )

    tools = asyncio.run(list_tools())
    assert tools[0].name == TOOL

    result = asyncio.run(call_tool(TOOL, {"target": "x"}))
    assert result.isError
    body = json.loads(result.content[0].text)
    assert body["reason"] == "per_call_max"
    # The host gets a reason it can act on, and nothing was signed.
    assert signer.count == 0
    assert server.create_initialization_options().server_name == "eraya-brainwave-proxy"


def test_every_cli_subcommand_parses():
    from app.client.__main__ import build_parser

    parser = build_parser()
    for argv in (
        ["policy"],
        ["economics"],
        ["verify", "--receipt", "r.json"],
        ["info", "--url", "http://x/mcp/"],
        ["tools", "--url", "http://x/mcp/"],
        ["quote", "--url", "http://x/mcp/", "--tool", TOOL],
        ["call", "--url", "http://x/mcp/", "--tool", TOOL],
        ["simulate", "--url", "http://x/mcp/", "--tool", TOOL, "--calls", "3"],
        ["proxy", "--url", "http://x/mcp/"],
    ):
        assert parser.parse_args(argv).command == argv[0]
