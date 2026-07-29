"""The whole path: 402 -> authorize -> execute -> meter -> capture -> batch -> settle.

Driven through the REAL `x402.mcp.create_payment_wrapper` and the REAL
`x402ResourceServer`, with only the facilitator faked -- because the facilitator is
the one component that would move money and cost money. Every 402 body, every
requirements match, every verify/settle dispatch in these tests is the SDK's code
running for real.

The FastMCP `Context` is faked instead of standing up the transport: the wrapper
reads payment from `ctx.request_context.meta.model_extra["x402/payment"]` and
nothing else, so three tiny objects reproduce it exactly and no port is bound.

    .venv/Scripts/python -m pytest tests/test_pay_flow.py -q
"""

from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("PUBLIC_BASE_URL", "http://testserver")

from sqlmodel import Session as DBSession  # noqa: E402
from sqlmodel import select  # noqa: E402
from x402 import x402ResourceServer  # noqa: E402
from x402.mechanisms.evm.exact import ExactEvmServerScheme  # noqa: E402
from x402.mechanisms.evm.upto import UptoEvmServerScheme  # noqa: E402
from x402.schemas import (  # noqa: E402
    SettleResponse,
    SupportedKind,
    SupportedResponse,
    VerifyResponse,
)

from app.config import settings  # noqa: E402
from app.db import create_all, engine  # noqa: E402
from app.models import (  # noqa: E402
    Author,
    Batch,
    BatchStatus,
    Call,
    CallStatus,
    PaySession,
    Receipt,
    SessionStatus,
    Tool,
)
from app.pay import batching, economics, gateway  # noqa: E402
from app.pay import receipts as receipts_mod
from app.pay.decorator import paid, registry  # noqa: E402

NET = settings.x402_network
USDC = settings.asset_address
PAYER = "0x00000000000000000000000000000000000B0B00"
PAY_TO = "0x000000000000000000000000000000000000dEaD"
FACILITATOR_ADDR = "0x" + "11" * 20


# --------------------------------------------------------------- test doubles --


class FakeFacilitator:
    """Answers `/supported`, `verify` and `settle`. Records what it was asked to settle.

    `transaction=""` is not laziness: it is exactly what the SDK's own
    batch-settlement scheme returns from `handle_before_settle` for a voucher
    payload, and it is how the gateway tells "captured against a signed voucher"
    apart from "swept on-chain".
    """

    def __init__(self, tx: str = "", valid: bool = True) -> None:
        self.tx = tx
        self.valid = valid
        self.settled: list[int] = []
        self.verified: list[int] = []

    def get_supported(self) -> SupportedResponse:
        return SupportedResponse(
            kinds=[
                SupportedKind(x402_version=2, scheme="exact", network=NET, extra={}),
                SupportedKind(
                    x402_version=2,
                    scheme="upto",
                    network=NET,
                    extra={"facilitatorAddress": FACILITATOR_ADDR},
                ),
            ]
        )

    async def verify(self, payload, requirements) -> VerifyResponse:
        self.verified.append(int(requirements.amount))
        if not self.valid:
            return VerifyResponse(is_valid=False, invalid_reason="insufficient_funds")
        return VerifyResponse(is_valid=True, payer=PAYER)

    async def settle(self, payload, requirements) -> SettleResponse:
        self.settled.append(int(requirements.amount))
        return SettleResponse(
            success=True,
            transaction=self.tx,
            network=requirements.network,
            amount=requirements.amount,
            payer=PAYER,
        )


class OverCapturingFacilitator(FakeFacilitator):
    """Reports an amount other than the requirements the gateway submitted."""

    async def settle(self, payload, requirements) -> SettleResponse:
        requested = int(requirements.amount)
        self.settled.append(requested)
        return SettleResponse(
            success=True,
            transaction="0x" + "ab" * 32,
            network=requirements.network,
            amount=str(requested + 1),
            payer=PAYER,
        )


class _Meta:
    def __init__(self, extra: dict) -> None:
        self.model_extra = extra


class _RequestContext:
    def __init__(self, meta: _Meta) -> None:
        self.meta = meta


class Ctx:
    """The two attributes `_extract_payment_from_context` actually reads."""

    def __init__(self, payment: dict | None = None) -> None:
        self.request_context = _RequestContext(_Meta({"x402/payment": payment} if payment else {}))


class FakeDriver:
    """A settlement driver that reports transactions without sending any.

    Deliberately not a mock of `BatchSettlementChannelManager`: it implements the
    `SettlementDriver` protocol, which is the seam `settle_batch` is written
    against. `ChannelManagerDriver` -- the only implementation that reaches a
    chain -- is never constructed in the test suite.
    """

    def __init__(self, claim="0xCLAIM", settle="0xSETTLE") -> None:
        self._claim, self._settle = claim, settle
        self.calls: list[str] = []

    async def claim(self, batch):
        self.calls.append("claim")
        if self._claim is None:
            return batching.SettlementOutcome(None, None, error="rpc down")
        return batching.SettlementOutcome(self._claim, None, vouchers_claimed=batch.call_count)

    async def settle(self, batch, outcome):
        self.calls.append("settle")
        if self._settle is None:
            return batching.SettlementOutcome(
                outcome.claim_tx_hash, None, outcome.vouchers_claimed, error="sweep reverted"
            )
        return batching.SettlementOutcome(
            outcome.claim_tx_hash, self._settle, outcome.vouchers_claimed
        )


# -------------------------------------------------------------------- fixtures --


def _install_gateway(fac: FakeFacilitator) -> gateway.Gateway:
    server = x402ResourceServer(fac)
    schemes = {"exact": ExactEvmServerScheme(), "upto": UptoEvmServerScheme()}
    for scheme in schemes.values():
        server.register(NET, scheme)
    gw = gateway.Gateway(resource_server=server, facilitator=fac, schemes=schemes, network=NET)
    gateway.set_gateway(gw)
    return gw


@pytest.fixture(autouse=True)
def clean_db():
    create_all()
    with DBSession(engine) as db:
        for model in (Receipt, Call, Batch, PaySession, Tool, Author):
            for row in db.exec(select(model)).all():
                db.delete(row)
        db.commit()
    registry.clear()
    for entry in list(registry.values()):
        entry.tool_id = None
    yield
    gateway.reset_gateway()


@pytest.fixture
def facilitator():
    fac = FakeFacilitator()
    _install_gateway(fac)
    return fac


def authorize(challenge: dict, *, nonce: str = "0x01") -> dict:
    """Build the payload an agent would sign, from the requirements it was quoted.

    `find_matching_requirements` compares scheme/network/amount/asset/payTo, so
    echoing `accepts[0]` verbatim is what a conforming client does.
    """
    return {
        "x402Version": 2,
        "payload": {
            "authorization": {"from": PAYER, "nonce": nonce},
            "signature": "0x" + "ab" * 65,
        },
        "accepted": challenge["accepts"][0],
    }


def sim_tool(name="scan", **price):
    kwargs = dict(network=NET, asset=USDC, pay_to=PAY_TO)
    kwargs.update(price)

    @paid(name=name, **kwargs)
    async def handler(target: str) -> dict:
        """Simulate a prompt-injection attack against an agent."""
        return {"target": target, "findings": ["jailbreak", "exfil"]}

    return handler


# ------------------------------------------------------------------- the 402 --


async def test_an_unpaid_call_returns_the_sdks_402_challenge(facilitator):
    tool = sim_tool(price="$0.002")
    result = await tool(target="acme", ctx=Ctx())

    assert result.isError is True
    body = result.structuredContent
    assert body["x402Version"] == 2
    assert body["error"] == "Payment Required"
    assert body["accepts"][0]["amount"] == "2000"
    assert body["accepts"][0]["payTo"] == PAY_TO
    assert body["resource"]["url"] == "mcp://tool/scan"
    # An unpaid probe has no payer, so it can have no session and therefore no
    # ledger row. It is a challenge, not a call.
    with DBSession(engine) as db:
        assert db.exec(select(Call)).all() == []


async def test_the_challenge_carries_the_scheme_extras_the_sdk_fills_in(facilitator):
    """`upto` will not settle without `extra.facilitatorAddress`, and that value
    comes from what the facilitator advertises -- so the challenge has to be
    enhanced by the SDK's own `enhance_payment_requirements`, not hand-built."""
    tool = sim_tool(
        name="deep_scan",
        price="$0.05",
        scheme="upto",
        max_price="$0.50",
        meter="bytes",
        price_per_unit="$0.001",
    )
    body = (await tool(target="acme", ctx=Ctx())).structuredContent
    extra = body["accepts"][0]["extra"]
    assert extra["facilitatorAddress"] == FACILITATOR_ADDR
    assert extra["assetTransferMethod"] == "permit2"
    # USDC's EIP-712 domain, read from x402's own NETWORK_CONFIGS, not from memory.
    assert extra["name"] == "USDC" and extra["version"] == "2"


# --------------------------------------------------------------- the paid path --


async def test_an_exact_call_settles_exactly_the_advertised_amount(facilitator):
    tool = sim_tool(price="$0.002")
    challenge = (await tool(target="acme", ctx=Ctx())).structuredContent

    result = await tool(target="acme", ctx=Ctx(authorize(challenge)))
    assert result.isError is False
    assert json.loads(result.content[0].text)["target"] == "acme"
    assert facilitator.settled == [2_000]

    with DBSession(engine) as db:
        call = db.exec(select(Call)).one()
        assert call.status == CallStatus.CAPTURED  # no tx hash yet -> not SETTLED
        assert call.authorized_atomic == 2_000
        assert call.captured_atomic == 2_000
        assert call.platform_fee_atomic + call.author_net_atomic == call.captured_atomic
        assert call.payer == PAYER


async def test_upto_settles_the_metered_amount_not_the_ceiling(facilitator):
    """THE point of the `upto` scheme, and the one thing the MCP wrapper cannot do
    on its own.

    `create_payment_wrapper` settles the requirements it advertised. On the HTTP
    side the SDK has `Settlement-Overrides` for exactly this; over MCP there is no
    such seam, so `_MeteredResourceServer` substitutes the amount with the same
    `model_copy(update={"amount": ...})` the SDK's HTTP server uses.

    Without that substitution the facilitator below would see 500000 -- the agent
    would be charged the full ceiling on every call and `upto` would be a lie.
    """
    tool = sim_tool(
        name="deep_scan",
        price="$0.05",
        scheme="upto",
        max_price="$0.50",
        meter="bytes",
        price_per_unit="$0.001",
    )
    challenge = (await tool(target="acme", ctx=Ctx())).structuredContent
    assert challenge["accepts"][0]["amount"] == "500000"  # the ceiling is authorized

    result = await tool(target="acme", ctx=Ctx(authorize(challenge)))
    assert result.isError is False

    body_len = len(result.content[0].text.encode("utf-8"))
    expected = 50_000 + body_len * 1_000
    assert facilitator.settled == [expected]
    assert expected < 500_000

    with DBSession(engine) as db:
        call = db.exec(select(Call)).one()
        assert call.authorized_atomic == 500_000
        assert call.captured_atomic == expected
        assert call.meter == "bytes"
        assert call.meter_units == body_len


async def test_facilitator_amount_mismatch_fails_loudly_without_false_receipt():
    facilitator = OverCapturingFacilitator()
    _install_gateway(facilitator)
    tool = sim_tool(name="hostile_settle", price="$0.002")
    challenge = (await tool(target="acme", ctx=Ctx())).structuredContent

    result = await tool(target="acme", ctx=Ctx(authorize(challenge)))
    assert result.isError is True

    with DBSession(engine) as db:
        call = db.exec(select(Call)).one()
        assert call.authorized_atomic == 2_000
        assert call.captured_atomic == 0
        assert call.status == CallStatus.FAILED
        assert db.exec(select(Receipt)).all() == []


async def test_a_tool_can_declare_its_own_consumption_without_leaking_it(facilitator):
    @paid(
        name="llm_tool",
        price="$0.001",
        scheme="upto",
        max_price="$1.00",
        meter="tokens",
        price_per_unit="$0.000001",
        network=NET,
        asset=USDC,
        pay_to=PAY_TO,
    )
    async def llm_tool(prompt: str) -> dict:
        return {"answer": "42", "_meter": {"units": 1_487}}

    challenge = (await llm_tool(prompt="why", ctx=Ctx())).structuredContent
    result = await llm_tool(prompt="why", ctx=Ctx(authorize(challenge)))

    assert "_meter" not in result.content[0].text
    assert json.loads(result.content[0].text) == {"answer": "42"}
    assert facilitator.settled == [1_000 + 1_487]
    with DBSession(engine) as db:
        assert db.exec(select(Call)).one().meter_units == 1_487


async def test_a_tool_that_raises_is_never_billed(facilitator):
    @paid(name="broken", price="$0.05", network=NET, asset=USDC, pay_to=PAY_TO)
    async def broken(x: int) -> dict:
        raise RuntimeError("upstream exploded")

    challenge = (await broken(x=1, ctx=Ctx())).structuredContent
    result = await broken(x=1, ctx=Ctx(authorize(challenge)))

    assert result.isError is True
    assert "upstream exploded" in result.content[0].text
    assert facilitator.settled == []  # the SDK never reaches settle after a raise
    with DBSession(engine) as db:
        call = db.exec(select(Call)).one()
        assert call.status == CallStatus.FAILED
        assert call.captured_atomic == 0


# ------------------------------------------------------------------ receipts --


async def test_the_receipt_rides_back_in_the_protocols_own_metadata(facilitator):
    tool = sim_tool(price="$0.002")
    challenge = (await tool(target="acme", ctx=Ctx())).structuredContent
    result = await tool(target="acme", ctx=Ctx(authorize(challenge)))

    # Not a private channel: `_meta["x402/payment-response"]` is where the SDK puts
    # the SettleResponse, and `extra` is that schema's own extension point.
    response = result.meta["x402/payment-response"]
    receipt = response["extra"]["receipt"]
    assert receipt["capturedAtomic"] == "2000"
    assert receipt["authorizedAtomic"] == "2000"
    assert receipt["payer"] == PAYER
    assert receipt["payTo"] == PAY_TO
    # Batched: nothing on-chain yet, and the receipt says so rather than inventing
    # a transaction hash.
    assert receipt["transaction"] is None
    assert receipt["settlement"] == "batched"
    assert response["extra"]["brainwave"]["sessionId"].startswith("sess_")


async def test_receipt_verification_is_honest_about_what_it_proves(facilitator):
    tool = sim_tool(price="$0.002")
    challenge = (await tool(target="acme", ctx=Ctx())).structuredContent
    await tool(target="acme", ctx=Ctx(authorize(challenge)))

    with DBSession(engine) as db:
        receipt = db.exec(select(Receipt)).one()
        result = receipts_mod.verify(db, receipt.receipt_id)
        db.commit()

    assert result.ok
    # No batch, no transaction: the strongest evidence available is a facilitator
    # attestation, and the status says so instead of claiming "verified".
    assert result.status == "verified_attested"
    names = {c.name for c in result.checks}
    assert {
        "body_hash",
        "capture_within_authorization",
        "matches_ledger",
        "revenue_split_conserves",
    } <= names
    assert all(c.strength in ("local", "facilitator", "chain") for c in result.checks)


async def test_a_tampered_receipt_fails_its_own_hash(facilitator):
    tool = sim_tool(price="$0.002")
    challenge = (await tool(target="acme", ctx=Ctx())).structuredContent
    await tool(target="acme", ctx=Ctx(authorize(challenge)))

    with DBSession(engine) as db:
        receipt = db.exec(select(Receipt)).one()
        body = json.loads(receipt.body_json)
        body["capturedAtomic"] = "1"  # someone edits the ledger
        receipt.body_json = receipts_mod.canonical_json(body)
        db.add(receipt)
        db.commit()
        result = receipts_mod.verify(db, receipt.receipt_id)
        db.commit()

    assert not result.ok
    assert result.status == "failed"
    assert [c for c in result.checks if c.name == "body_hash"][0].ok is False


# ------------------------------------------------------------------ declines --


async def test_a_disabled_tool_is_declined_before_it_is_ever_settled(facilitator):
    tool = sim_tool(price="$0.002")
    challenge = (await tool(target="acme", ctx=Ctx())).structuredContent
    # One good call first, so the `tool` row exists to be disabled -- exactly the
    # order an operator would hit it in.
    await tool(target="acme", ctx=Ctx(authorize(challenge, nonce="0x01")))
    with DBSession(engine) as db:
        row = db.exec(select(Tool)).one()
        row.enabled = False
        db.add(row)
        db.commit()

    facilitator.settled.clear()
    result = await tool(target="acme", ctx=Ctx(authorize(challenge, nonce="0x02")))

    assert result.isError is True
    assert "tool_disabled" in result.structuredContent["error"]
    assert facilitator.settled == []  # refused, therefore not charged
    with DBSession(engine) as db:
        declined = db.exec(select(Call).where(Call.status == CallStatus.DECLINED)).all()
        assert len(declined) == 1
        assert declined[0].decline_reason == "tool_disabled"
        assert declined[0].captured_atomic == 0
        assert db.exec(select(PaySession)).one().declined_count == 1


async def test_a_replayed_authorization_is_declined_and_recorded(facilitator):
    """The `(network, nonce)` unique index is the defence; this proves it is wired
    to a decline rather than to a 500."""
    tool = sim_tool(price="$0.002")
    challenge = (await tool(target="acme", ctx=Ctx())).structuredContent
    payload = authorize(challenge, nonce="0xdeadbeef")

    first = await tool(target="acme", ctx=Ctx(payload))
    assert first.isError is False

    second = await tool(target="acme", ctx=Ctx(payload))
    assert second.isError is True
    assert "replayed_nonce" in second.structuredContent["error"]
    assert facilitator.settled == [2_000]  # charged exactly once

    with DBSession(engine) as db:
        statuses = sorted(c.decline_reason or c.status for c in db.exec(select(Call)).all())
        assert "replayed_nonce" in statuses


async def test_a_session_budget_freezes_the_session_and_stops_admitting(facilitator):
    tool = sim_tool(price="$0.002")
    challenge = (await tool(target="acme", ctx=Ctx())).structuredContent
    await tool(target="acme", ctx=Ctx(authorize(challenge, nonce="0x1")))

    with DBSession(engine) as db:
        session = db.exec(select(PaySession)).one()
        session.budget_atomic = 2_000  # exactly what has already been captured
        db.add(session)
        db.commit()

    facilitator.settled.clear()
    result = await tool(target="acme", ctx=Ctx(authorize(challenge, nonce="0x2")))
    assert result.isError is True
    assert "over_session_budget" in result.structuredContent["error"]
    assert facilitator.settled == []


async def test_a_facilitator_that_refuses_the_authorization_produces_no_ledger_row():
    fac = FakeFacilitator(valid=False)
    _install_gateway(fac)
    tool = sim_tool(price="$0.002")
    challenge = (await tool(target="acme", ctx=Ctx())).structuredContent
    result = await tool(target="acme", ctx=Ctx(authorize(challenge)))

    assert result.isError is True
    assert "insufficient_funds" in result.structuredContent["error"]
    with DBSession(engine) as db:
        assert db.exec(select(Call)).all() == []


# ------------------------------------------------------------------ batching --


async def _five_paid_calls(tool, facilitator):
    challenge = (await tool(target="acme", ctx=Ctx())).structuredContent
    for i in range(5):
        result = await tool(target="acme", ctx=Ctx(authorize(challenge, nonce=f"0x{i:02x}")))
        assert result.isError is False
    return challenge


async def test_five_calls_close_into_one_batch_that_conserves_exactly(facilitator):
    tool = sim_tool(price="$0.002")
    await _five_paid_calls(tool, facilitator)

    with DBSession(engine) as db:
        session = db.exec(select(PaySession)).one()
        assert session.captured_atomic == 10_000
        assert session.settled_atomic == 0  # captured is not settled

        batch = batching.close_window(db, session, force=True)
        db.commit()

        assert batch is not None
        assert batch.call_count == 5
        assert batch.gross_atomic == 10_000
        assert batch.platform_fee_atomic + batch.author_net_atomic == batch.gross_atomic
        # Two transactions per batch: claim, then settle.
        assert batch.facilitator_fee_atomic == settings.facilitator_fee_atomic * 2

        calls = db.exec(select(Call).where(Call.batch_id == batch.id)).all()
        ok, detail = economics.batch_conservation(batch, list(calls))
        assert ok, detail
        assert sum(c.captured_atomic for c in calls) == batch.gross_atomic


async def test_settlement_walks_the_states_and_stamps_the_receipts(facilitator):
    tool = sim_tool(price="$0.002")
    await _five_paid_calls(tool, facilitator)
    driver = FakeDriver()

    with DBSession(engine) as db:
        session = db.exec(select(PaySession)).one()
        batch = await batching.close_and_settle(db, session, driver, force=True)

        assert driver.calls == ["claim", "settle"]
        assert batch.status == BatchStatus.SETTLED
        assert batch.claim_tx_hash == "0xCLAIM"
        assert batch.settle_tx_hash == "0xSETTLE"

        db.refresh(session)
        assert session.settled_atomic == 10_000
        assert session.status == SessionStatus.SETTLED

        calls = db.exec(select(Call)).all()
        assert all(c.status == CallStatus.SETTLED for c in calls)

        receipts = db.exec(select(Receipt)).all()
        assert len(receipts) == 5
        assert all(r.tx_hash == "0xSETTLE" for r in receipts)
        # Rebuilt, not patched: a receipt whose stored hash no longer matches its
        # stored body is indistinguishable from a tampered one.
        for receipt in receipts:
            assert receipts_mod.body_digest(json.loads(receipt.body_json)) == receipt.body_hash
            assert receipts_mod.verify(db, receipt.receipt_id).status == "verified_onchain"
        db.commit()


async def test_a_claim_that_lands_without_a_sweep_is_a_recoverable_state(facilitator):
    """The reason `Batch` carries two transaction hashes.

    A batch whose claim succeeded and whose sweep did not has real money sitting
    in the settlement contract. Marking it FAILED would invite a re-claim of
    vouchers that were already claimed.
    """
    tool = sim_tool(price="$0.002")
    await _five_paid_calls(tool, facilitator)

    with DBSession(engine) as db:
        session = db.exec(select(PaySession)).one()
        batch = await batching.close_and_settle(db, session, FakeDriver(settle=None), force=True)
        assert batch.status == BatchStatus.CLAIMED
        assert batch.claim_tx_hash == "0xCLAIM"
        assert batch.settle_tx_hash is None
        assert "sweep reverted" in batch.error
        # Nothing is marked settled, and no receipt claims a transaction.
        assert all(c.status == CallStatus.CAPTURED for c in db.exec(select(Call)).all())
        assert all(r.tx_hash is None for r in db.exec(select(Receipt)).all())


async def test_a_failed_claim_settles_nothing_and_says_so(facilitator):
    tool = sim_tool(price="$0.002")
    await _five_paid_calls(tool, facilitator)

    with DBSession(engine) as db:
        session = db.exec(select(PaySession)).one()
        batch = await batching.close_and_settle(db, session, FakeDriver(claim=None), force=True)
        assert batch.status == BatchStatus.FAILED
        assert "rpc down" in batch.error
        assert batch.claim_tx_hash is None


async def test_without_a_driver_a_batch_closes_and_honestly_stays_unsettled(facilitator):
    """The default everywhere in this codebase. On-chain settlement is opt-in."""
    tool = sim_tool(price="$0.002")
    await _five_paid_calls(tool, facilitator)

    with DBSession(engine) as db:
        session = db.exec(select(PaySession)).one()
        batch = await batching.close_and_settle(db, session, None, force=True)
        assert batch.status == BatchStatus.OPEN
        assert batch.settle_tx_hash is None
        db.refresh(session)
        assert session.settled_atomic == 0
        assert session.status == SessionStatus.CLOSING


async def test_a_batch_never_mixes_payees(facilitator):
    """`SettlePayload` sweeps to ONE receiver, so a mixed batch would pay one
    author the others' revenue."""
    tool = sim_tool(price="$0.002")
    await _five_paid_calls(tool, facilitator)
    with DBSession(engine) as db:
        session = db.exec(select(PaySession)).one()
        rogue = db.exec(select(Call)).first()
        rogue.pay_to = "0x" + "99" * 20
        db.add(rogue)
        db.commit()
        with pytest.raises(ValueError, match="multiple payees"):
            batching.close_window(db, session, force=True)
        db.rollback()


# --------------------------------------------------------------- concurrency --


async def test_concurrent_calls_do_not_cross_contaminate_their_meters(facilitator):
    """The contract `_CURRENT` depends on, checked rather than assumed.

    Per-call state is carried in a `ContextVar` because `verify_payment`, the
    handler and `settle_payment` are all awaited from the same coroutine inside
    the SDK's `wrapped()`. Concurrent requests are separate tasks with copied
    contexts, so each must see only its own meter reading. If that were wrong,
    two agents calling at once would be billed each other's amounts -- silently,
    and only under load.
    """
    import asyncio

    @paid(
        name="sized",
        price="$0.00",
        scheme="upto",
        max_price="$1.00",
        meter="bytes",
        price_per_unit="$0.001",
        network=NET,
        asset=USDC,
        pay_to=PAY_TO,
    )
    async def sized(pad: int) -> str:
        return "x" * pad

    challenge = (await sized(pad=1, ctx=Ctx())).structuredContent
    pads = [7, 23, 61, 104, 250]
    results = await asyncio.gather(
        *[
            sized(pad=pad, ctx=Ctx(authorize(challenge, nonce=f"0xc{i}")))
            for i, pad in enumerate(pads)
        ]
    )

    assert all(r.isError is False for r in results)
    # Each call settled exactly its own output size, in some interleaving.
    assert sorted(facilitator.settled) == sorted(pad * 1_000 for pad in pads)
    with DBSession(engine) as db:
        assert sorted(c.meter_units for c in db.exec(select(Call)).all()) == sorted(pads)


# ------------------------------------------------------- the enum/string trap --


async def test_enum_columns_come_back_as_plain_strings(facilitator):
    """Why `app/pay` compares statuses with `==` and never with `is`.

    `app/models.py` forces every StrEnum column to `String(32)` -- deliberately, so
    the ledger stores the x402 wire value `batch-settlement` rather than the member
    name `BATCH_SETTLEMENT`. The cost is that SQLModel hands the raw string back on
    read, so `session.status is SessionStatus.OPEN` is False for every row that
    came from the database.

    This bit for real during development: `_admit()` used `is not
    SessionStatus.OPEN` and declined every call on a reloaded session with
    "session_not_open". The test exists so it cannot come back.
    """
    tool = sim_tool(price="$0.002")
    challenge = (await tool(target="acme", ctx=Ctx())).structuredContent
    await tool(target="acme", ctx=Ctx(authorize(challenge)))

    with DBSession(engine) as db:
        session = db.exec(select(PaySession)).one()
        assert type(session.status) is str
        assert session.status is not SessionStatus.OPEN  # the trap
        assert session.status == SessionStatus.OPEN  # what the code must use
        assert session.status == "open"

        # A second call on the reloaded session must still be admitted.
        result = await tool(target="acme", ctx=Ctx(authorize(challenge, nonce="0x99")))
    assert result.isError is False


# ----------------------------------------------------------------- the claim --


async def test_the_ledger_reproduces_the_headline_economics(facilitator):
    """The README's numbers, recomputed from rows this test suite actually wrote."""
    tool = sim_tool(price="$0.002")
    await _five_paid_calls(tool, facilitator)

    with DBSession(engine) as db:
        session = db.exec(select(PaySession)).one()
        await batching.close_and_settle(db, session, FakeDriver(), force=True)
        db.refresh(session)
        calls = list(db.exec(select(Call)))
        batches = list(db.exec(select(Batch)))
        report = economics.session_report(session, calls, batches)

    assert report["calls"] == 5
    assert report["capturedAtomic"] == "10000"
    assert report["settledAtomic"] == "10000"
    assert report["unsettledAtomic"] == "0"
    assert report["settlements"] == 1
    # One settlement (two transactions at $0.001) against $0.01 of revenue: 20%.
    # Five calls is far too few to batch -- and the report says so rather than
    # quoting the hundred-call figure.
    assert report["realisedLoadBps"] == 2_000
    # The counterfactual, clearly labelled: five per-call settlements would have
    # cost 5x$0.001 against $0.01 -> 50%.
    assert report["hypotheticalPerCallLoadBps"] == 5_000


async def test_window_policy_refuses_to_settle_dust(facilitator, monkeypatch):
    """Below the dust floor a settlement costs more than it collects, so the
    window stays open and the gross rolls into the next one."""
    tool = sim_tool(price="$0.002")
    challenge = (await tool(target="acme", ctx=Ctx())).structuredContent
    await tool(target="acme", ctx=Ctx(authorize(challenge, nonce="0x1")))

    monkeypatch.setattr(settings, "batch_window_seconds", 0)
    with DBSession(engine) as db:
        session = db.exec(select(PaySession)).one()
        calls = list(db.exec(select(Call).where(Call.status == CallStatus.CAPTURED)))

        # $0.002 captured against a $0.01 floor.
        decision = batching.should_close(session, calls)
        assert not decision.close
        assert "dust floor" in decision.reason

        monkeypatch.setattr(settings, "batch_min_gross_atomic", 1)
        assert batching.should_close(session, calls).close


async def test_a_full_window_closes_even_when_it_is_small(facilitator, monkeypatch):
    tool = sim_tool(price="$0.002")
    challenge = (await tool(target="acme", ctx=Ctx())).structuredContent
    await tool(target="acme", ctx=Ctx(authorize(challenge, nonce="0x1")))

    monkeypatch.setattr(settings, "batch_max_calls", 1)
    with DBSession(engine) as db:
        session = db.exec(select(PaySession)).one()
        calls = list(db.exec(select(Call).where(Call.status == CallStatus.CAPTURED)))
        decision = batching.should_close(session, calls)
        assert decision.close and "batch full" in decision.reason
