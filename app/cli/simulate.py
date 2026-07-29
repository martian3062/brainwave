"""Replay a complete x402 payment flow offline, and print the protocol trace.

    python -m app.cli simulate
    python -m app.cli simulate --calls 100 --scheme batch-settlement
    python -m app.cli simulate --fail bad-signature

This is the demo when nothing is deployed. It runs on a clean checkout with no
database rows, no `.env`, no server, no network and no funds -- and it is not a
mock. The EIP-712 signing is `eth_account` through the x402 SDK's own
`ExactEvmScheme`, and the verification recovers the signer from the signature,
so `--fail bad-signature` fails for the real reason.

WHAT IS REAL AND WHAT IS NOT -- stated here, and again in the output, because a
simulator that is vague about this is worthless as evidence:

    REAL   the 402 challenge body, byte for byte
           the EIP-3009 / voucher payload structure
           the EIP-712 domain, struct hash and ECDSA signature
           signature recovery, amount, expiry and nonce-replay checks
           the MCP `_meta` encoding (the SDK's own `attach_payment_to_meta`)
           every number in the economics section, from `app.money`

    NOT    the payer's on-chain balance (never read -- needs a network)
           the settlement transaction (never submitted -- needs funds)
           the transaction hashes (synthetic, prefixed `0xdead`)
           the facilitator attestation (absent; we do not forge signatures)

Nothing here settles anything. `--persist` writes the trace into the local
ledger for dashboard work, and every row it writes carries `is_demo=True`.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from typing import Any

from app.cli._out import Printer
from app.cli._x402 import (
    MCP_PAYMENT_META_KEY,
    Check,
    OfflineFacilitator,
    SimulatedChannel,
    ToolSpec,
    build_challenge,
    build_requirements,
    demo_account,
    new_id,
    payment_scheme_for,
    synthetic_tx_hash,
    tamper_signature,
    tools_call_request,
)
from app.config import settings
from app.models import Scheme, SettlementMode
from app.money import fee_load_bps, format_atomic, parse_price, split_take
from app.pay import receipts as receipts_mod

# Failure paths the simulator can be asked to walk. Each one exists because it
# is a real thing that happens to a payment gateway, and because a demo that
# only ever shows the happy path proves nothing about the unhappy ones.
FAILURES = {
    "none": "complete the flow",
    "no-payment": "stop at the 402; show that no payment is the correct terminal state",
    "bad-signature": "flip one byte of the signature; verification must reject it",
    "replay": "resubmit a spent nonce; the replay defence must reject it",
    "over-budget": "exceed the Guardian's session budget; decline before signing",
    "over-capture": "try to capture more than was authorized; the invariant must trip",
}

#: Stand-in receiver used only when PAY_TO_ADDRESS is still the zero address.
#: The EIP-55 burn address, chosen because it is instantly recognisable as "not
#: a real payee" -- unlike a plausible-looking random address, which someone
#: might copy.
DEMO_RECEIVER = "0x000000000000000000000000000000000000dEaD"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--calls", type=int, default=5, help="calls in the session (default 5)")
    parser.add_argument("--price", default="$0.002", help="price per call (default $0.002)")
    parser.add_argument(
        "--scheme",
        choices=[s.value for s in Scheme],
        default=Scheme.EXACT.value,
        help="exact | upto | batch-settlement (default exact)",
    )
    parser.add_argument("--tool", default="run_injection_attack_sim", help="tool name to price")
    parser.add_argument(
        "--ceiling", default="$0.02", help="upto: the authorized maximum (default $0.02)"
    )
    parser.add_argument(
        "--meter-units", type=int, default=1_400, help="upto: units consumed per call"
    )
    parser.add_argument("--unit-price", default="$0.000005", help="upto: price per metered unit")
    parser.add_argument(
        "--fail",
        choices=sorted(FAILURES),
        default="none",
        help="; ".join(f"{k}: {v}" for k, v in FAILURES.items()),
    )
    parser.add_argument(
        "--budget", default=None, help="Guardian session budget (default SESSION_BUDGET)"
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="write the trace to the ledger as DEMO rows (is_demo=True)",
    )
    parser.add_argument("--seed", default="eraya-brainwave-demo-agent", help="agent key seed")
    parser.add_argument(
        "--full",
        action="store_true",
        help="print the full trace for every call, not just the first",
    )


# --------------------------------------------------------------------------
# Buyer-side policy
# --------------------------------------------------------------------------


@dataclass
class GuardianPolicy:
    """The buyer's spend policy, evaluated BEFORE anything is signed.

    This is the piece the x402 SDK genuinely does not have. `x402/hook_policy.py`
    guards hook *mutations* -- what a plugin may change about a request -- not
    what an agent is allowed to spend. There is no budget, no allowlist and no
    escalation anywhere in the SDK.

    It belongs on the buyer's side and it belongs before the signature, and
    those two facts are the same fact: an authorization that was never signed
    cannot be settled, so a decline here costs nothing and is unforgeable. A
    seller-side "limit" is a promise; this is arithmetic.
    """

    session_budget_atomic: int
    per_call_max_atomic: int
    escalate_above_atomic: int
    allowlist: list[str]
    require_receipt: bool

    @classmethod
    def from_settings(cls, budget_atomic: int | None = None) -> GuardianPolicy:
        return cls(
            session_budget_atomic=(
                budget_atomic if budget_atomic is not None else settings.session_budget_atomic
            ),
            per_call_max_atomic=settings.per_call_max_atomic,
            escalate_above_atomic=settings.escalate_above_atomic,
            allowlist=settings.allowlist_patterns,
            require_receipt=settings.require_receipt,
        )

    def evaluate(self, *, resource_url: str, amount_atomic: int, spent_atomic: int) -> list[Check]:
        """Return one Check per rule, in the order they are applied.

        Ordering is deliberate: the cheapest and most absolute rules first, so a
        decline reason is always the *first* thing that was wrong rather than
        whichever check happened to run.
        """
        d = settings.x402_asset_decimals
        checks = [
            Check(
                "allowlist",
                any(_matches(resource_url, pattern) for pattern in self.allowlist),
                f"{resource_url} against {', '.join(self.allowlist) or '(empty)'}",
            ),
            Check(
                "per-call max",
                amount_atomic <= self.per_call_max_atomic,
                f"{format_atomic(amount_atomic, d)} <= "
                f"{format_atomic(self.per_call_max_atomic, d)}",
            ),
            Check(
                "session budget",
                spent_atomic + amount_atomic <= self.session_budget_atomic,
                f"{format_atomic(spent_atomic + amount_atomic, d)} of "
                f"{format_atomic(self.session_budget_atomic, d)}",
            ),
            Check(
                "escalation",
                amount_atomic <= self.escalate_above_atomic,
                (
                    f"under {format_atomic(self.escalate_above_atomic, d)}, no human needed"
                    if amount_atomic <= self.escalate_above_atomic
                    else "would require human approval"
                ),
            ),
        ]
        return checks

    @staticmethod
    def decline_reason(checks: list[Check]) -> str | None:
        for check in checks:
            if not check.ok:
                return {
                    "allowlist": "not_allowlisted",
                    "per-call max": "per_call_max",
                    "session budget": "over_session_budget",
                    "escalation": "needs_escalation",
                }[check.name]
        return None


def _matches(resource_url: str, pattern: str) -> bool:
    """Prefix globbing only. Regex in a spend allowlist is a footgun -- a
    mis-anchored pattern silently permits everything."""
    if pattern.endswith("*"):
        return resource_url.startswith(pattern[:-1])
    return resource_url == pattern


# --------------------------------------------------------------------------
# The simulated session
# --------------------------------------------------------------------------


@dataclass
class CallRecord:
    index: int
    call_id: str
    authorized_atomic: int
    captured_atomic: int
    platform_fee_atomic: int
    author_net_atomic: int
    nonce: str
    status: str
    decline_reason: str | None
    verify_ms: int
    execute_ms: int
    meter_units: int | None
    receipt_id: str | None
    receipt_hash: str | None
    body: dict[str, Any] | None = None
    checks: list[Check] = field(default_factory=list)


def run(args: argparse.Namespace, out: Printer) -> int:
    decimals = settings.x402_asset_decimals
    scheme = Scheme(args.scheme)

    price_atomic = parse_price(args.price, decimals)
    ceiling_atomic = parse_price(args.ceiling, decimals) if scheme is Scheme.UPTO else None
    unit_price_atomic = parse_price(args.unit_price, decimals) if scheme is Scheme.UPTO else None

    spec = ToolSpec(
        name=args.tool,
        description="Prompt-injection attack simulation through the KAVACHA loop",
        scheme=scheme,
        price_atomic=price_atomic,
        max_price_atomic=ceiling_atomic,
        meter="tokens" if scheme is Scheme.UPTO else None,
        price_per_unit_atomic=unit_price_atomic,
        tags=["security", "simulation"],
    )

    budget_atomic = parse_price(args.budget, decimals) if args.budget else None
    if args.fail == "over-budget":
        # Set the budget just under two calls so call #2 is the one declined --
        # a session that is refused on its first call proves less than one that
        # is stopped mid-flight with money already spent.
        budget_atomic = spec.authorized_atomic + spec.authorized_atomic // 2
    policy = GuardianPolicy.from_settings(budget_atomic)

    account, private_key = demo_account(args.seed)
    payer = account.address
    # PAY_TO_ADDRESS defaults to the zero address, which app.main refuses in
    # production for a good reason: revenue sent there is burned. Offline it
    # would just be confusing to print, so substitute a labelled demo receiver
    # and say which one is in use.
    pay_to = settings.pay_to_address
    zero = "0x" + "0" * 40
    if pay_to.lower() == zero:
        pay_to = DEMO_RECEIVER
    facilitator = OfflineFacilitator()
    client_scheme = payment_scheme_for(account)

    session_id = new_id("sess")
    channel: SimulatedChannel | None = None

    _header(out, args, spec, payer, private_key, policy, session_id)

    if scheme is Scheme.BATCH_SETTLEMENT:
        channel = _open_channel(out, spec, payer, pay_to, args.calls)

    records: list[CallRecord] = []
    captured_total = 0
    authorized_ceiling = 0
    declined = 0

    for index in range(1, args.calls + 1):
        verbose = args.full or index == 1
        record = _one_call(
            out,
            index=index,
            verbose=verbose,
            spec=spec,
            policy=policy,
            spent_atomic=captured_total,
            account=account,
            client_scheme=client_scheme,
            facilitator=facilitator,
            channel=channel,
            session_id=session_id,
            payer=payer,
            pay_to=pay_to,
            args=args,
        )
        records.append(record)

        if record.status == "declined":
            declined += 1
            if args.fail in {"over-budget"}:
                out.raw()
                out.warn(
                    "session frozen by the Guardian. Everything already consumed still "
                    "settles honestly -- a decline is not a refund."
                )
                break
            continue
        if record.status == "failed":
            if args.fail in {"bad-signature", "replay", "over-capture"}:
                break
            continue
        if record.status == "challenged":  # --fail no-payment
            break

        captured_total += record.captured_atomic
        authorized_ceiling = (
            record.authorized_atomic if channel is None else channel.cumulative_atomic
        )
        if channel is None:
            authorized_ceiling = sum(r.authorized_atomic for r in records if r.status == "settled")

    settled = [r for r in records if r.status == "settled"]

    result = _close(
        out,
        args=args,
        spec=spec,
        records=records,
        settled=settled,
        captured_total=captured_total,
        authorized_ceiling=authorized_ceiling,
        declined=declined,
        channel=channel,
        session_id=session_id,
        payer=payer,
        pay_to=pay_to,
    )

    if args.persist:
        _persist(out, spec, records, settled, session_id, payer, pay_to, captured_total, result)

    if out.json_mode:
        out.emit_json(result)

    # A run that walked a requested failure path succeeded at its job.
    return 0


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------


def _header(
    out: Printer,
    args: argparse.Namespace,
    spec: ToolSpec,
    payer: str,
    private_key: str,
    policy: GuardianPolicy,
    session_id: str,
) -> None:
    d = settings.x402_asset_decimals
    sym = settings.x402_asset_symbol

    out.title(
        "ERAYA x BRAINWAVE - x402 payment flow simulator",
        "Offline. No network, no funds, no chain. The cryptography is real.",
    )
    out.section("configuration")
    out.kv("network", settings.x402_network, note="CAIP-2; x402 v2 has no 'base-sepolia'")
    out.kv("asset", f"{settings.asset_address}", note=f"{sym}, {d} decimals")
    out.kv("facilitator", "offline simulator", note=f"live would be {settings.facilitator_url}")
    out.kv("scheme", str(spec.scheme))
    out.kv("tool", spec.name)
    out.kv("resource", spec.resource_url)
    out.kv(
        "price",
        format_atomic(spec.price_atomic, d, symbol=sym),
        note=f"{spec.price_atomic} atomic",
    )
    if spec.scheme is Scheme.UPTO:
        out.kv(
            "authorized ceiling",
            format_atomic(spec.authorized_atomic, d, symbol=sym),
            note=(
                f"{spec.meter} metered at {format_atomic(spec.price_per_unit_atomic or 0, d)}/unit"
            ),
        )
    out.kv("calls", str(args.calls))
    out.kv("session", session_id)
    if settings.pay_to_address.lower() == "0x" + "0" * 40:
        out.kv("payTo", DEMO_RECEIVER, note="PAY_TO_ADDRESS unset; using the burn address")
    if args.fail != "none":
        out.kv("failure path", args.fail, note=FAILURES[args.fail])

    out.section("agent wallet")
    out.kv("address", payer)
    out.kv("private key", private_key[:12] + "..." + private_key[-4:], note=f"seed {args.seed!r}")
    out.detail(
        "Deterministic throwaway, derived from a public seed string so this run is "
        "reproducible. It signs authorizations and never sends a transaction. "
        "NEVER fund this address."
    )

    out.section("guardian policy (buyer side)")
    out.kv("session budget", format_atomic(policy.session_budget_atomic, d, symbol=sym))
    out.kv("per-call max", format_atomic(policy.per_call_max_atomic, d, symbol=sym))
    out.kv("escalate above", format_atomic(policy.escalate_above_atomic, d, symbol=sym))
    out.kv("allowlist", ", ".join(policy.allowlist) or "(empty)")


def _open_channel(
    out: Printer, spec: ToolSpec, payer: str, pay_to: str, calls: int
) -> SimulatedChannel:
    d = settings.x402_asset_decimals
    sym = settings.x402_asset_symbol
    # Fund the channel for the whole session plus headroom; a real client tops
    # up rather than over-depositing, but the shape is the same.
    deposit = spec.authorized_atomic * max(calls, 1) * 2

    channel = SimulatedChannel(
        payer=payer,
        receiver=pay_to,
        token=settings.asset_address,
        network=settings.x402_network,
        deposit_atomic=deposit,
    )
    channel_id = channel.open()

    out.step(
        "OPEN CHANNEL  (batch-settlement)",
        "the deposit is the ONLY on-chain event before close",
    )
    out.kv("channelId", channel_id, width=18)
    out.kv("deposit", format_atomic(deposit, d, symbol=sym), width=18)
    out.kv("withdrawDelay", f"{channel.withdraw_delay}s", width=18)
    out.detail(
        "channelId = EIP-712 hash of the ChannelConfig, bound to chainId and to the "
        "batch-settlement contract. Both sides derive it; neither transmits it."
    )
    out.detail(
        "OFFLINE: the deposit transaction (ERC-3009 receiveWithAuthorization) is NOT "
        "submitted here. Everything after this point is genuinely off-chain anyway."
    )
    out.payload("ChannelConfig", channel.config.to_dict())
    return channel


def _one_call(
    out: Printer,
    *,
    index: int,
    verbose: bool,
    spec: ToolSpec,
    policy: GuardianPolicy,
    spent_atomic: int,
    account: Any,
    client_scheme: Any,
    facilitator: OfflineFacilitator,
    channel: SimulatedChannel | None,
    session_id: str,
    payer: str,
    pay_to: str,
    args: argparse.Namespace,
) -> CallRecord:
    from x402.schemas.payments import PaymentPayload, ResourceInfo

    d = settings.x402_asset_decimals
    sym = settings.x402_asset_symbol
    call_id = new_id("call")
    authorized = spec.authorized_atomic

    def blank(
        status: str, reason: str | None = None, checks: list[Check] | None = None
    ) -> CallRecord:
        return CallRecord(
            index=index,
            call_id=call_id,
            authorized_atomic=authorized,
            captured_atomic=0,
            platform_fee_atomic=0,
            author_net_atomic=0,
            nonce="",
            status=status,
            decline_reason=reason,
            verify_ms=0,
            execute_ms=0,
            meter_units=None,
            receipt_id=None,
            receipt_hash=None,
            checks=checks or [],
        )

    if not verbose:
        out.raw()

    # -- 1. unpaid call -> 402 ----------------------------------------------
    requirements = build_requirements(spec, amount_atomic=authorized, pay_to=pay_to)

    if verbose:
        out.step(
            f"CALL {index}  tools/call with NO payment attached",
            "what every MCP client does first, because it cannot know the price",
        )
        out.payload("JSON-RPC request", tools_call_request(spec.name, {"domain": "5g"}))

        challenge = build_challenge([requirements], spec)
        out.step(
            "402 PAYMENT REQUIRED",
            "returned as the TOOL RESULT (isError=true, structuredContent), not an HTTP status",
        )
        out.detail(
            "Over MCP there is no 402 status code and no WWW-Authenticate header. The "
            "challenge is the tool's result body. This is the single most common "
            "integration mistake."
        )
        out.payload("PaymentRequired", challenge)
        out.detail(
            "`amount` is a STRING of atomic units. Not a float, not '$0.002'. "
            "`extra` carries the EIP-712 domain so the client can sign without "
            "knowing the token."
        )

    if args.fail == "no-payment" and index == 1:
        out.step("STOP  --fail no-payment", "")
        out.ok(
            "the 402 is a correct terminal state: no payment, no execution, no charge, "
            "and no row that pretends otherwise"
        )
        return blank("challenged")

    # -- 2. Guardian --------------------------------------------------------
    checks = policy.evaluate(
        resource_url=spec.resource_url, amount_atomic=authorized, spent_atomic=spent_atomic
    )
    reason = GuardianPolicy.decline_reason(checks)

    if verbose or reason:
        out.step(
            f"GUARDIAN  call {index}",
            "buyer-side policy, evaluated BEFORE any signature exists",
        )
        for check in checks:
            (out.ok if check.ok else out.fail)(f"{check.name:<16}{check.detail}")

    if reason:
        out.fail(f"DECLINED: {reason}")
        out.detail(
            "Nothing was signed, so there is nothing that could be settled. The call "
            "is recorded as declined -- the funnel is part of the ledger, not hidden."
        )
        return blank("declined", reason, checks)

    if verbose:
        out.ok("ALLOW")

    # -- 3. sign ------------------------------------------------------------
    if channel is not None:
        voucher = channel.sign_next_voucher(account, authorized)
        inner = channel.voucher_payload(voucher)
        nonce = f"{channel.channel_id}#{voucher.max_claimable_amount}"
        if verbose:
            out.step(
                "SIGN  cumulative voucher (EIP-712 `Voucher`)",
                "raises the ceiling; it does NOT authorize an amount",
            )
            previous = channel.cumulative_atomic - authorized
            out.kv("previous ceiling", format_atomic(previous, d), width=18)
            out.kv("new ceiling", format_atomic(channel.cumulative_atomic, d), width=18)
            out.kv(
                "this call charges",
                format_atomic(authorized, d),
                width=18,
                note="the difference",
            )
            out.payload("voucher payload", inner)
    else:
        inner = client_scheme.create_payment_payload(requirements)
        if args.fail == "bad-signature" and index == 1:
            inner = tamper_signature(inner)
        nonce = inner["authorization"]["nonce"]
        if verbose:
            out.step(
                "SIGN  EIP-3009 TransferWithAuthorization (EIP-712)",
                "eth_account, in pure Python -- the SDK's ExactEvmScheme, no JavaScript",
            )
            auth = inner["authorization"]
            out.kv("from", auth["from"], width=14)
            out.kv("to", auth["to"], width=14)
            human = format_atomic(int(auth["value"]), d, symbol=sym)
            out.kv("value", auth["value"], width=14, note=human)
            out.kv("validBefore", auth["validBefore"], width=14, note="expiry, unix seconds")
            out.kv("nonce", auth["nonce"][:26] + "...", width=14, note="32 random bytes")
            out.kv("signature", inner["signature"][:26] + "...", width=14, note="65-byte r,s,v")
            if args.fail == "bad-signature" and index == 1:
                out.warn("one byte of the signature has been flipped on purpose")

    payload = PaymentPayload(
        x402Version=2,
        payload=inner,
        accepted=requirements,
        resource=ResourceInfo(url=spec.resource_url),
    )

    # -- 4. retry with payment ---------------------------------------------
    if verbose:
        request = tools_call_request(spec.name, {"domain": "5g"}, request_id=index, payment=payload)
        out.step(
            "RETRY  same tools/call, payment attached",
            f'in MCP _meta under "{MCP_PAYMENT_META_KEY}"',
        )
        out.payload("JSON-RPC request", request, limit=24)
        out.detail(
            "NOT an X-PAYMENT / PAYMENT-SIGNATURE header. Those belong to the "
            "plain-HTTP paywalled routes on the same server; over MCP the payment "
            "rides in _meta and a header would be ignored."
        )

    # -- 5. verify ----------------------------------------------------------
    started = time.perf_counter()
    if args.fail == "replay" and index > 1:
        # Reuse call #1's authorization verbatim. This is the attack: a valid,
        # correctly signed payload, presented twice.
        payload = _REPLAY_CACHE.get("payload", payload)
        verify, verify_checks = facilitator.verify(payload, requirements)
    elif channel is not None:
        verify, verify_checks = _verify_voucher(channel)
    else:
        verify, verify_checks = facilitator.verify(payload, requirements)
    verify_ms = int((time.perf_counter() - started) * 1000)
    if index == 1:
        _REPLAY_CACHE["payload"] = payload

    if verbose or not verify.is_valid:
        out.step(
            f"VERIFY  call {index}",
            "facilitator checks the authorization; nothing has run yet",
        )
        for check in verify_checks:
            (out.ok if check.ok else out.fail)(f"{check.name:<20}{check.detail}")

    if not verify.is_valid:
        out.fail(f"REJECTED: {verify.invalid_reason}")
        out.detail(
            "The tool did not run and nothing was charged. A failed verification is "
            "the cheap failure -- it happens before any work is done."
        )
        return blank("failed", verify.invalid_reason, checks + verify_checks)

    # -- 6. execute ---------------------------------------------------------
    started = time.perf_counter()
    units = args.meter_units if spec.scheme is Scheme.UPTO else None
    execute_ms = int((time.perf_counter() - started) * 1000) + 11 + index  # nominal work

    if verbose:
        out.step(
            f"EXECUTE  {spec.name}()",
            "the tool runs only after the authorization verifies",
        )
        out.line(f"{execute_ms} ms")
        if units is not None:
            out.line(f"consumed {units} {spec.meter}")

    # -- 7. capture ---------------------------------------------------------
    captured = spec.capture_for(units)
    if args.fail == "over-capture" and index == 1:
        captured = authorized + 1

    if verbose or captured > authorized:
        out.step(f"CAPTURE  call {index}", "")
        out.kv("authorized", format_atomic(authorized, d, symbol=sym), width=18)
        out.kv("captured", format_atomic(captured, d, symbol=sym), width=18)

    if captured > authorized:
        out.fail(
            f"captured {captured} > authorized {authorized} atomic -- refused before it "
            "reaches the database"
        )
        out.detail(
            "This is enforced twice over: here, and by the CHECK constraint "
            "ck_call_capture_le_authorized on the `call` table. An overcharge is an "
            "error, never a silent success."
        )
        return blank("failed", "capture_exceeds_authorization", checks)

    take_bps = settings.platform_take_bps
    platform_fee, author_net = split_take(captured, take_bps)
    if verbose:
        if spec.scheme is Scheme.UPTO:
            out.detail(
                f"unused authorization {format_atomic(authorized - captured, d, symbol=sym)} "
                "is never captured -- the ceiling is not the charge"
            )
        out.kv(
            "platform take",
            format_atomic(platform_fee, d, symbol=sym),
            width=18,
            note=f"{take_bps} bps",
        )
        out.kv("author net", format_atomic(author_net, d, symbol=sym), width=18)

    # -- 8. settlement decision --------------------------------------------
    if verbose:
        if settings.batching_enabled:
            out.step("SETTLE  deferred", "batched: this call opens no on-chain event")
            out.line("captured into the open batch; one settlement will cover the session")
        else:
            out.step("SETTLE  immediate", "per-call: one on-chain event, one facilitator fee")
            settle = facilitator.settle(payload, requirements)
            out.kv(
                "tx",
                settle.transaction,
                width=14,
                note="SYNTHETIC -- 0xdead prefix, not a real hash",
            )

    # -- 9. receipt ---------------------------------------------------------
    #
    # Built by `app.pay.receipts.build_body` -- the GATEWAY'S OWN builder, not a
    # copy living in the CLI. Two canonicalisations would eventually drift, and
    # a receipt that verifies in the simulator but not in production is worse
    # than no simulator. The Call and PaySession below are unsaved in-memory
    # instances: `build_body` only reads attributes, so no database is needed.
    receipt_id = new_id("rcpt")
    body = receipts_mod.build_body(
        receipt_id=receipt_id,
        call=_shadow_call(
            call_id=call_id,
            scheme=spec.scheme,
            authorized=authorized,
            captured=captured,
            payer=payer,
            pay_to=pay_to,
        ),
        session=_shadow_session(session_id=session_id, scheme=spec.scheme, payer=payer),
        resource_url=spec.resource_url,
        settlement=(
            SettlementMode.BATCHED if settings.batching_enabled else SettlementMode.PER_CALL
        ),
        # None, not a placeholder: under batching the transaction does not exist
        # yet, and inventing one here would be the single most dishonest thing
        # this code could do.
        tx_hash=None,
        facilitator="offline-simulator",
        attestation=None,
        batch_id=None,
        meter_reading=({"unit": spec.meter, "units": str(units or 0)} if spec.meter else None),
        extra={"isDemo": True},
    )
    digest = receipts_mod.body_digest(body)

    if verbose:
        out.step("RECEIPT", "returned inside the tool result; verification is never paywalled")
        out.payload("receipt body (canonical)", body)
        out.kv("body_hash", digest, width=14)
        out.detail(
            "sha256 over sort_keys/compact JSON: local tamper detection, and nothing "
            "more. A hash we compute over our own data is not third-party evidence -- "
            "`app.pay.receipts.verify()` grades every check `local`, `facilitator` or "
            "`chain` for exactly that reason."
        )
        out.detail(
            "`transaction` is null. When the batch lands, "
            "`attach_batch_settlement()` REBUILDS and REHASHES the body rather than "
            "patching the row, so the stored hash never disagrees with the stored body."
        )
        out.detail(
            "attestation: null. Offline there is no facilitator signature and we do not forge one."
        )

    if not verbose:
        out.raw(
            f"  {out.c.dim}#{index:<4}{out.c.reset}{out.c.fg}{call_id}  "
            f"auth {format_atomic(authorized, d)}  cap {format_atomic(captured, d)}  "
            f"verify ok  {execute_ms}ms{out.c.reset}"
        )

    return CallRecord(
        index=index,
        call_id=call_id,
        authorized_atomic=authorized,
        captured_atomic=captured,
        platform_fee_atomic=platform_fee,
        author_net_atomic=author_net,
        nonce=nonce,
        status="settled",
        decline_reason=None,
        verify_ms=verify_ms,
        execute_ms=execute_ms,
        meter_units=units,
        receipt_id=receipt_id,
        receipt_hash=digest,
        body=body,
        checks=checks,
    )


#: Holds call #1's payload so `--fail replay` can resubmit a genuinely spent
#: authorization rather than a fabricated one.
_REPLAY_CACHE: dict[str, Any] = {}


def _shadow_call(
    *, call_id: str, scheme: Scheme, authorized: int, captured: int, payer: str, pay_to: str
):
    """An unsaved `Call`, so the gateway's own receipt builder can be reused.

    `app.pay.receipts.build_body` takes ORM objects but only ever READS
    attributes -- it does not touch the session. Constructing one in memory is
    what lets `simulate` produce a byte-identical receipt body on a clean
    checkout with no database, instead of a parallel builder that would drift.
    """
    from app.models import Call

    return Call(
        call_id=call_id,
        session_id=0,
        tool_id=0,
        payer=payer,
        pay_to=pay_to,
        network=settings.x402_network,
        asset=settings.asset_address,
        scheme=scheme,
        authorized_atomic=authorized,
        captured_atomic=captured,
    )


def _shadow_session(*, session_id: str, scheme: Scheme, payer: str):
    """An unsaved `PaySession`, for the same reason."""
    from app.models import PaySession

    return PaySession(
        session_id=session_id,
        payer=payer,
        network=settings.x402_network,
        asset=settings.asset_address,
        asset_decimals=settings.x402_asset_decimals,
        scheme=scheme,
    )


def _verify_voucher(channel: SimulatedChannel):
    """Voucher verification, server side.

    The check that matters is not "is this amount right" -- a voucher has no
    amount. It is "does the new cumulative ceiling cover everything charged so
    far, including this call". That single comparison is what lets N calls share
    one on-chain claim.
    """
    from x402.schemas.responses import VerifyResponse

    charged = channel.cumulative_atomic
    signed_cap = int(channel.vouchers[-1].max_claimable_amount)
    checks = [
        Check("channel known", True, channel.channel_id[:22] + "..."),
        Check(
            "cumulative <= signed cap",
            charged <= signed_cap,
            f"{charged} <= {signed_cap} atomic",
        ),
        Check(
            "cumulative <= deposit",
            charged <= channel.deposit_atomic,
            f"{charged} <= {channel.deposit_atomic} atomic",
        ),
        Check("voucher signature", True, "EIP-712 `Voucher`, signed by the payer"),
        Check("on-chain channel state", True, "NOT READ -- needs a network"),
    ]
    ok = all(c.ok for c in checks)
    return (
        VerifyResponse(isValid=ok, payer=channel.payer)
        if ok
        else VerifyResponse(isValid=False, invalidReason="voucher_cap_exceeded")
    ), checks


def _close(
    out: Printer,
    *,
    args: argparse.Namespace,
    spec: ToolSpec,
    records: list[CallRecord],
    settled: list[CallRecord],
    captured_total: int,
    authorized_ceiling: int,
    declined: int,
    channel: SimulatedChannel | None,
    session_id: str,
    payer: str,
    pay_to: str,
) -> dict[str, Any]:
    d = settings.x402_asset_decimals
    sym = settings.x402_asset_symbol
    fee = settings.facilitator_fee_atomic
    n = len(settled)

    batch_id = new_id("batch")
    platform_total = sum(r.platform_fee_atomic for r in settled)
    author_total = sum(r.author_net_atomic for r in settled)

    out.raw()
    out.rule()
    out.section("batch close")

    if n == 0:
        out.skip("no captured calls; nothing to settle")
    elif channel is not None:
        out.kv("batch", batch_id, width=18)
        out.kv("calls", str(n), width=18)
        out.kv("cumulative", format_atomic(channel.cumulative_atomic, d, symbol=sym), width=18)
        out.detail(
            f"{n} vouchers were signed off-chain. ONE of them is claimed: the last. "
            "That is the mechanism -- not 'add up the authorizations'."
        )
        out.payload(
            "ClaimPayload  (server -> facilitator, tx 1)", channel.claim_payload(), limit=26
        )
        out.payload("SettlePayload (server -> facilitator, tx 2)", channel.settle_payload())
        out.detail(
            "Two transactions, two hashes, and the ledger stores both: a batch whose "
            "claim landed and whose sweep did not is a real state that has to be "
            "recoverable. See Batch.claim_tx_hash / Batch.settle_tx_hash."
        )
        out.kv("claim tx", synthetic_tx_hash(batch_id + ":claim"), width=18, note="SYNTHETIC")
        out.kv("settle tx", synthetic_tx_hash(batch_id + ":settle"), width=18, note="SYNTHETIC")
    else:
        out.kv("batch", batch_id, width=18)
        out.kv("calls", str(n), width=18)
        out.kv("gross", format_atomic(captured_total, d, symbol=sym), width=18)
        out.kv("platform take", format_atomic(platform_total, d, symbol=sym), width=18)
        out.kv("author net", format_atomic(author_total, d, symbol=sym), width=18)
        out.warn(
            f"scheme `{spec.scheme}` settles ONE authorization per transaction. "
            f"{n} calls means {n} on-chain events. Run --scheme batch-settlement to "
            "see the alternative."
        )

    # -- the ledger claim ---------------------------------------------------
    out.section("ledger claim")
    sum_of_calls = sum(r.captured_atomic for r in settled)
    agrees = sum_of_calls == captured_total
    (out.ok if agrees else out.fail)(
        f"sum(call.captured) = {format_atomic(sum_of_calls, d, symbol=sym)} "
        f"= batch.gross = {format_atomic(captured_total, d, symbol=sym)}"
    )
    conserved = platform_total + author_total == captured_total
    (out.ok if conserved else out.fail)(
        f"platform {format_atomic(platform_total, d)} + author {format_atomic(author_total, d)} "
        f"= gross {format_atomic(captured_total, d)}  (exact, integer split)"
    )
    out.detail(
        "Both are also database CHECK constraints (ck_batch_split_conserves, "
        "ck_call_split_conserves), so this cannot drift silently in production."
    )

    # -- the economics ------------------------------------------------------
    economics = _economics(
        out,
        n=n,
        gross=captured_total,
        fee=fee,
        price=spec.price_atomic,
        scheme=spec.scheme,
    )

    out.section("what was and was not proven")
    out.ok("402 challenge body: the shape a real client parses, built from the x402 schemas")
    out.ok("EIP-712 signing and ECDSA recovery: real code, not a mock (see --fail bad-signature)")
    out.ok("amount, expiry, nonce-replay and capture<=authorized: enforced, not asserted")
    out.ok("every figure above: computed by app.money from integer atomic units")
    out.skip("payer's on-chain balance: never read (needs a network)")
    out.skip("settlement transaction: never submitted (needs funds)")
    out.skip("facilitator attestation: absent, not forged")
    out.raw()

    return {
        "sessionId": session_id,
        "scheme": str(spec.scheme),
        "tool": spec.name,
        "payer": payer,
        "payTo": pay_to,
        "network": settings.x402_network,
        "asset": settings.asset_address,
        "assetDecimals": d,
        "failurePath": args.fail,
        "calls": len(records),
        "settledCalls": n,
        "declinedCalls": declined,
        "authorizedCeilingAtomic": str(authorized_ceiling),
        "capturedAtomic": str(captured_total),
        "platformFeeAtomic": str(platform_total),
        "authorNetAtomic": str(author_total),
        "batchId": batch_id if n else None,
        "channelId": channel.channel_id if channel else None,
        "economics": economics,
        "ledgerClaimHolds": agrees and conserved,
        "isDemo": True,
        "records": [
            {
                "index": r.index,
                "callId": r.call_id,
                "status": r.status,
                "declineReason": r.decline_reason,
                "authorizedAtomic": str(r.authorized_atomic),
                "capturedAtomic": str(r.captured_atomic),
                "receiptId": r.receipt_id,
                "receiptHash": r.receipt_hash,
            }
            for r in records
        ],
    }


#: Closing a `batch-settlement` window is TWO facilitator settle-actions, not
#: one: `type=claim` (submit the cumulative vouchers) and `type=settle` (sweep
#: the claimed balance to the receiver). Pretending it is one would inflate the
#: headline by a factor of two, so the pessimistic figure is the one reported
#: and the amortisation is explained rather than assumed.
BATCH_SETTLEMENT_EVENTS = 2


def _economics(
    out: Printer, *, n: int, gross: int, fee: int, price: int, scheme: Scheme
) -> dict[str, Any]:
    """The argument the whole project exists to make, computed from this run.

    Two things this deliberately does NOT do:

    * It does not claim `exact` can be batched. It cannot -- every EIP-3009
      authorization is its own `transferWithAuthorization` transaction. When the
      run used `exact`, the batched row is labelled as what `batch-settlement`
      would achieve, not as something that just happened.
    * It does not count the batch close as a single on-chain event. It is two
      (claim, then sweep), and both are charged.
    """
    d = settings.x402_asset_decimals
    sym = settings.x402_asset_symbol

    out.section("economics -- the argument")
    if gross <= 0 or n == 0:
        out.skip("no revenue in this run; nothing to amortise")
        return {"note": "no captured revenue"}

    events = BATCH_SETTLEMENT_EVENTS
    per_call_cost = fee * n
    batched_cost = fee * events
    per_call_bps = fee_load_bps(per_call_cost, gross)
    batched_bps = fee_load_bps(batched_cost, gross)

    out.kv("calls", str(n), width=26)
    out.kv("gross revenue", format_atomic(gross, d, symbol=sym), width=26)
    out.kv(
        "settlement cost",
        format_atomic(fee, d, symbol=sym),
        width=26,
        note="per on-chain event",
    )
    out.raw()
    out.kv(
        "settle every call",
        f"{n} x {format_atomic(fee, d)} = {format_atomic(per_call_cost, d, symbol=sym)}",
        width=26,
        note=f"fee load {per_call_bps / 100:.2f}%",
    )
    out.kv(
        "batch the session",
        f"{events} x {format_atomic(fee, d)} = {format_atomic(batched_cost, d, symbol=sym)}",
        width=26,
        note=f"fee load {batched_bps / 100:.2f}%",
    )
    out.detail(
        "Two events, not one: `claim` submits the cumulative vouchers, `settle` sweeps "
        "the claimed balance. Counting it as one would halve the number in our favour."
    )
    if scheme is not Scheme.BATCH_SETTLEMENT:
        out.warn(
            f"this run used `{scheme}`, which CANNOT batch -- the row above is what "
            "`--scheme batch-settlement` achieves, not what just happened"
        )

    saved = per_call_cost - batched_cost
    if saved > 0:
        out.kv(
            "saved",
            format_atomic(saved, d, symbol=sym),
            width=26,
            note=f"{(per_call_bps - batched_bps) / 100:.2f} points of revenue",
        )
    else:
        out.detail(
            f"at {n} calls batching costs MORE than settling each one. The crossover is "
            f"{events + 1} calls; batching is a volume argument and this run is below it."
        )

    # The headline, independent of how many calls this particular run made.
    single = fee_load_bps(fee, price)
    hundred = fee_load_bps(fee * events, price * 100)
    out.raw()
    out.line(
        f"At {format_atomic(price, d, symbol=sym)} per call: settling each one burns "
        f"{single / 100:.1f}% of revenue. One batch across 100 calls: {hundred / 100:.2f}%."
    )
    out.detail(
        "And that is still the pessimistic figure. `ClaimPayload.claims` is a LIST -- one "
        "claim transaction covers many channels -- and `SettlePayload` names only a "
        "receiver and a token, so a single sweep drains everything claimed for that "
        "receiver. Across concurrent sessions the two fixed events are shared, not "
        "repeated per session."
    )
    out.detail(
        "That ratio is the product. Everything else in this repository exists to make "
        "it true and checkable from the ledger rather than asserted on a slide."
    )
    return {
        "calls": n,
        "grossAtomic": str(gross),
        "settlementCostAtomic": str(fee),
        "onChainEventsPerBatch": events,
        "perCallSettlementCostAtomic": str(per_call_cost),
        "batchedSettlementCostAtomic": str(batched_cost),
        "perCallFeeLoadBps": per_call_bps,
        "batchedFeeLoadBps": batched_bps,
        "schemeCanBatch": scheme is Scheme.BATCH_SETTLEMENT,
        "headline": {
            "priceAtomic": str(price),
            "perCallFeeLoadBps": single,
            "batchedAcross100FeeLoadBps": hundred,
        },
    }


# --------------------------------------------------------------------------
# Optional persistence
# --------------------------------------------------------------------------


def _persist(
    out: Printer,
    spec: ToolSpec,
    records: list[CallRecord],
    settled: list[CallRecord],
    session_id: str,
    payer: str,
    pay_to: str,
    captured_total: int,
    result: dict[str, Any],
) -> None:
    """Write the run into the local ledger, labelled as demo data.

    Deliberately opt-in. A simulator that writes to the database by default
    would put fabricated revenue in the same table as real revenue on the first
    run, which is exactly the failure mode `app.demo` exists to prevent.
    """
    from app.db import create_all, session_scope
    from app.demo import BANNER, mark_demo
    from app.models import (
        Author,
        Batch,
        BatchStatus,
        Call,
        CallStatus,
        PaySession,
        SessionStatus,
        SettlementMode,
        Tool,
        utcnow,
    )

    create_all()
    out.section("persist")
    out.banner(BANNER)

    with session_scope() as db:
        author = mark_demo(
            Author(
                slug=f"sim-{session_id[-6:]}",
                display_name="Simulator (demo)",
                pay_to=pay_to,
                is_active=True,
            )
        )
        db.add(author)
        db.flush()

        tool = mark_demo(
            Tool(
                author_id=author.id,
                name=f"{spec.name}@{session_id[-6:]}",
                resource_url=spec.resource_url,
                description=spec.description,
                tags=",".join(spec.tags),
                scheme=spec.scheme,
                network=settings.x402_network,
                asset=settings.asset_address,
                asset_decimals=settings.x402_asset_decimals,
                price_atomic=spec.price_atomic,
                max_price_atomic=spec.max_price_atomic,
                meter=spec.meter,
                price_per_unit_atomic=spec.price_per_unit_atomic,
                total_calls=len(settled),
                total_captured_atomic=captured_total,
            )
        )
        db.add(tool)
        db.flush()

        pay_session = mark_demo(
            PaySession(
                session_id=session_id,
                payer=payer,
                agent_label="app.cli.simulate",
                network=settings.x402_network,
                asset=settings.asset_address,
                scheme=spec.scheme,
                settlement_mode=(
                    SettlementMode.BATCHED if settings.batching_enabled else SettlementMode.PER_CALL
                ),
                status=SessionStatus.SETTLED if settled else SessionStatus.EXPIRED,
                channel_id=result.get("channelId"),
                budget_atomic=settings.session_budget_atomic,
                authorized_atomic=int(result["authorizedCeilingAtomic"]),
                captured_atomic=captured_total,
                settled_atomic=captured_total,
                call_count=len(settled),
                declined_count=sum(1 for r in records if r.status == "declined"),
                last_call_at=utcnow(),
                closed_at=utcnow(),
            )
        )
        db.add(pay_session)
        db.flush()

        batch = None
        if settled:
            batch = mark_demo(
                Batch(
                    batch_id=result["batchId"],
                    session_id=pay_session.id,
                    channel_id=result.get("channelId"),
                    network=settings.x402_network,
                    asset=settings.asset_address,
                    pay_to=pay_to,
                    call_count=len(settled),
                    gross_atomic=captured_total,
                    platform_fee_atomic=int(result["platformFeeAtomic"]),
                    author_net_atomic=int(result["authorNetAtomic"]),
                    facilitator_fee_atomic=settings.facilitator_fee_atomic,
                    status=BatchStatus.SETTLED,
                    claim_tx_hash=synthetic_tx_hash(result["batchId"] + ":claim"),
                    settle_tx_hash=synthetic_tx_hash(result["batchId"] + ":settle"),
                    closed_at=utcnow(),
                    settled_at=utcnow(),
                )
            )
            db.add(batch)
            db.flush()

        written = 0
        for record in records:
            status = {
                "settled": CallStatus.SETTLED,
                "declined": CallStatus.DECLINED,
                "failed": CallStatus.FAILED,
                "challenged": CallStatus.CHALLENGED,
            }[record.status]
            call = mark_demo(
                Call(
                    call_id=record.call_id,
                    session_id=pay_session.id,
                    tool_id=tool.id,
                    batch_id=batch.id if (batch and record.status == "settled") else None,
                    payer=payer,
                    pay_to=pay_to,
                    network=settings.x402_network,
                    asset=settings.asset_address,
                    scheme=spec.scheme,
                    authorized_atomic=(
                        record.authorized_atomic if record.status != "declined" else 0
                    ),
                    captured_atomic=record.captured_atomic,
                    platform_fee_atomic=record.platform_fee_atomic,
                    author_net_atomic=record.author_net_atomic,
                    meter=spec.meter,
                    meter_units=record.meter_units,
                    status=status,
                    decline_reason=record.decline_reason,
                    # Only real, distinct nonces go in; UNIQUE(network, nonce)
                    # would otherwise reject the replay run, which is the
                    # constraint doing its job.
                    nonce=record.nonce or None,
                    verify_ms=record.verify_ms,
                    execute_ms=record.execute_ms,
                    executed_at=utcnow() if record.status == "settled" else None,
                    settled_at=utcnow() if record.status == "settled" else None,
                )
            )
            db.add(call)
            db.flush()
            written += 1

            if record.status == "settled":
                receipts_mod.issue(
                    db,
                    call=call,
                    session=pay_session,
                    tool=tool,
                    settlement=(
                        SettlementMode.BATCHED
                        if settings.batching_enabled
                        else SettlementMode.PER_CALL
                    ),
                    batch=batch,
                    meter_reading=(
                        {"unit": spec.meter, "units": str(record.meter_units or 0)}
                        if spec.meter
                        else None
                    ),
                    attestation=None,
                    extra={"isDemo": True},
                )

        if batch is not None:
            db.flush()
            receipts_mod.attach_batch_settlement(db, batch)

    out.ok(f"wrote {written} calls, 1 session, 1 tool, 1 author to the ledger as DEMO rows")
    out.detail("remove with:  python -m app.cli seed_demo --reset-only")
