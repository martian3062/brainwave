"""`python -m app.client` -- the buyer-side CLI, and the Claude Desktop bridge.

    python -m app.client policy
    python -m app.client economics --price '$0.002' --calls 100
    python -m app.client info      --url http://localhost:8000/mcp/
    python -m app.client tools     --url http://localhost:8000/mcp/
    python -m app.client quote     --url ... --tool run_injection_attack_sim
    python -m app.client call      --url ... --tool run_injection_attack_sim --args '{}'
    python -m app.client simulate  --url ... --tool run_injection_attack_sim --calls 20
    python -m app.client verify    --receipt receipt.json --rpc https://sepolia.base.org
    python -m app.client proxy     --url ...        # stdio MCP server, for Claude Desktop

`policy` and `economics` need no network and no key: `economics` is the whole
argument of the submission reduced to arithmetic you can check by hand.

There is no npx package in this design and there does not need to be. The buyer
side is Python because the signing is Python, and `proxy` is what carries that
into an MCP host that only speaks stdio. See README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from app.client.guardian import Guardian, SpendJournal, SpendPolicy, auto_approve, console_approver
from app.money import fee_load_bps, format_atomic, parse_price

log = logging.getLogger("brainwave.cli")


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def _policy_from_args(args: argparse.Namespace) -> SpendPolicy:
    from app.config import settings

    decimals = settings.x402_asset_decimals

    def _p(value: str | None, fallback: int | None) -> int | None:
        if value is None:
            return fallback
        if value.lower() in {"none", "off", "unlimited"}:
            return None
        return parse_price(value, decimals)

    return SpendPolicy(
        session_budget_atomic=_p(args.session_budget, settings.session_budget_atomic),
        per_call_max_atomic=_p(args.per_call_max, settings.per_call_max_atomic),
        daily_budget_atomic=_p(args.daily_budget, settings.daily_budget_atomic),
        escalate_above_atomic=_p(args.escalate_above, settings.escalate_above_atomic),
        allowlist=tuple(args.allow) if args.allow else tuple(settings.allowlist_patterns),
        require_receipt=not args.no_require_receipt,
        networks=(args.network or settings.x402_network,),
        assets=(settings.asset_address,) if settings.asset_address else (),
        asset_decimals=decimals,
    )


def _guardian_from_args(args: argparse.Namespace) -> Guardian:
    approvers = {"auto": auto_approve, "ask": console_approver, "deny": None}
    return Guardian(
        _policy_from_args(args),
        approver=approvers.get(args.approve),
        journal=SpendJournal(args.journal),
    )


def _signer_from_args(args: argparse.Namespace) -> Any:
    from app.client.signer import generate_demo_key, load_signer

    if args.ephemeral_key:
        key, address = generate_demo_key()
        print(
            f"# using an EPHEMERAL key {address} -- it holds nothing, so every "
            "settlement will fail at the facilitator. Useful only to watch the "
            "protocol and the Guardian.",
            file=sys.stderr,
        )
        return load_signer(key, network=args.network or "", allow_mainnet=args.allow_mainnet)
    return load_signer(
        args.key or None, network=args.network or "", allow_mainnet=args.allow_mainnet
    )


def _client_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    from app.config import settings

    return {
        "guardian": _guardian_from_args(args),
        "signer": _signer_from_args(args),
        "network": args.network or settings.x402_network,
        "agent_label": args.label,
        "rpc_url": args.rpc or None,
        "attestor": args.attestor or None,
        "verify_receipts": not args.no_verify_receipts,
    }


# --------------------------------------------------------------------------
# Commands that need nothing
# --------------------------------------------------------------------------


def cmd_policy(args: argparse.Namespace) -> int:
    guardian = _guardian_from_args(args)
    print(json.dumps(guardian.snapshot(), indent=2))
    return 0


def cmd_economics(args: argparse.Namespace) -> int:
    """The argument, as arithmetic. No network, no key, no server.

    Per-call settlement pays the facilitator once per call. Batched settlement
    pays it once per session. At micro prices those are not two points on a
    curve, they are two different businesses.
    """
    decimals = args.decimals
    price = parse_price(args.price, decimals)
    fee = parse_price(args.fee, decimals)
    calls = args.calls

    gross = price * calls
    per_call_fees = fee * calls
    batched_fees = fee

    rows = [
        ("tool price", format_atomic(price, decimals)),
        ("calls in the session", str(calls)),
        ("gross revenue", format_atomic(gross, decimals)),
        ("", ""),
        ("PER-CALL settlement", ""),
        ("  facilitator fees", format_atomic(per_call_fees, decimals)),
        ("  author keeps", format_atomic(gross - per_call_fees, decimals)),
        ("  fee load", f"{fee_load_bps(per_call_fees, gross) / 100:.2f}%"),
        ("", ""),
        ("BATCHED settlement (N authorizations, 1 settlement)", ""),
        ("  facilitator fees", format_atomic(batched_fees, decimals)),
        ("  author keeps", format_atomic(gross - batched_fees, decimals)),
        ("  fee load", f"{fee_load_bps(batched_fees, gross) / 100:.2f}%"),
    ]
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        print(f"{label.ljust(width)}  {value}" if label or value else "")

    saved = per_call_fees - batched_fees
    print()
    print(
        f"Batching keeps {format_atomic(saved, decimals)} of {format_atomic(gross, decimals)} "
        f"gross that per-call settlement burns -- "
        f"{(fee_load_bps(per_call_fees, gross) - fee_load_bps(batched_fees, gross)) / 100:.2f} "
        "percentage points of revenue."
    )
    if per_call_fees >= gross:
        print(
            "At this price, per-call settlement costs more than the tool earns. "
            "That is not a tuning problem; it is why the naive design cannot ship."
        )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    from app.client.verify import verify_receipt

    with open(args.receipt, encoding="utf-8") as fh:
        receipt = json.load(fh)
    result = verify_receipt(
        receipt,
        expected_attestor=args.attestor or None,
        rpc_url=args.rpc or None,
        expected_payer=args.payer or None,
    )
    if args.json:
        print(json.dumps(result.as_dict(), indent=2))
    else:
        print(result.summary())
    return 0 if result.verified else 1


# --------------------------------------------------------------------------
# Commands that talk to a gateway
# --------------------------------------------------------------------------


async def _info(args: argparse.Namespace) -> int:
    from app.client.shim import PaidMCPClient

    async with PaidMCPClient.connect(args.url, **_client_kwargs(args)) as client:
        tools = await client.list_tools()
        free = [t for t in tools if t["name"] == "gateway_info"]
        if free:
            result = await client._session.call_tool(name="gateway_info", arguments={})
            for item in result.content:
                text = getattr(item, "text", None)
                if text:
                    print(json.dumps(json.loads(text), indent=2))
        print(f"\n{len(tools)} tools advertised")
    return 0


async def _tools(args: argparse.Namespace) -> int:
    from app.client.shim import PaidMCPClient

    async with PaidMCPClient.connect(args.url, **_client_kwargs(args)) as client:
        for tool in await client.list_tools():
            print(f"{tool['name']}\n    {tool['description'][:160]}")
    return 0


async def _quote(args: argparse.Namespace) -> int:
    from app.client.shim import PaidMCPClient

    async with PaidMCPClient.connect(args.url, **_client_kwargs(args)) as client:
        quote = await client.quote(args.tool, json.loads(args.args))
        if quote is None:
            print(f"{args.tool} is free (it returned no payment requirements)")
            return 0
        print(json.dumps(quote.as_dict(), indent=2))
    return 0


async def _call(args: argparse.Namespace) -> int:
    from app.client.shim import PaidMCPClient

    async with PaidMCPClient.connect(args.url, **_client_kwargs(args)) as client:
        call = await client.call_tool(args.tool, json.loads(args.args))
        print(call.trace(client.guardian.policy.asset_decimals))
        if call.receipt_verification is not None:
            print()
            print(call.receipt_verification.summary())
        print()
        print(json.dumps(client.session_snapshot(), indent=2))
    return 0 if call.ok else 1


async def _simulate(args: argparse.Namespace) -> int:
    """N calls through one session. Prints the trace, then the reconciliation."""
    from app.client.shim import PaidMCPClient

    arguments = json.loads(args.args)
    async with PaidMCPClient.connect(args.url, **_client_kwargs(args)) as client:
        decimals = client.guardian.policy.asset_decimals
        for index in range(args.calls):
            call = await client.call_tool(args.tool, arguments)
            print(f"--- call {index + 1}/{args.calls} " + "-" * 40)
            print(call.trace(decimals))
            if client.guardian.frozen:
                print(f"\nGuardian froze the session: {client.guardian.frozen_reason}")
                break

        snapshot = client.session_snapshot()
        print("\n=== session " + "=" * 50)
        print(json.dumps(snapshot, indent=2))

        # The buyer's independent audit of its own agent: the Guardian's books
        # against the signer's tally of bearer instruments actually produced.
        signed = client.signer.authorized_total()
        authorized = sum(c.authorized_atomic for c in client.calls)
        print(
            f"\nreconciliation: guardian authorized {format_atomic(authorized, decimals)}, "
            f"signer produced {client.signer.count} authorizations totalling "
            f"{format_atomic(signed, decimals)} -- "
            + ("AGREE" if signed == authorized else "DISAGREE (trust the signer)")
        )
    return 0


async def _proxy(args: argparse.Namespace) -> int:
    """A stdio MCP server that re-exposes a paid remote gateway.

    This is how a paid gateway reaches Claude Desktop, Cursor or any other MCP
    host. Those hosts speak stdio and hold no wallet, so they cannot answer a
    402 themselves. This process can: it holds the key, enforces the Guardian,
    pays, and hands the host back an ordinary tool result.

    Written with `mcp.server.lowlevel.Server` rather than FastMCP on purpose --
    the remote tools' `inputSchema`s are passed through VERBATIM. FastMCP
    derives a schema from a Python signature, which would mean inventing a
    signature for every remote tool and silently reshaping its contract.
    """
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    from app.client.shim import PaidMCPClient

    async with PaidMCPClient.connect(args.url, **_client_kwargs(args)) as client:
        server: Server = Server(
            "eraya-brainwave-proxy",
            instructions=(
                "Paid MCP tools, proxied from a remote x402 gateway. Every call is "
                "metered and settled in USDC on Base under a local spend policy. "
                "Tool results carry the payment receipt in _meta['x402/payment-response']."
            ),
        )

        @server.list_tools()
        async def list_tools() -> list[types.Tool]:
            remote = await client.list_tools()
            return [
                types.Tool(
                    name=tool["name"],
                    description=tool["description"],
                    inputSchema=tool["inputSchema"],
                )
                for tool in remote
            ]

        @server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
            call = await client.call_tool(name, arguments)
            if call.declined:
                # A policy refusal is reported to the host as a tool error with
                # the reason, not as a transport failure: the agent should be
                # able to read "over_session_budget" and choose a cheaper plan.
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "error": "payment_declined_by_local_policy",
                                    "reason": str(call.decline_reason),
                                    "detail": call.verdicts[-1].message if call.verdicts else "",
                                    "guardian": client.guardian.snapshot(),
                                }
                            ),
                        )
                    ],
                    isError=True,
                )
            return types.CallToolResult(
                content=[
                    types.TextContent(type="text", text=str(item.get("text", "")))
                    for item in call.content
                    if item.get("type") == "text"
                ]
                or [types.TextContent(type="text", text="")],
                isError=call.is_error,
                _meta={
                    "x402/payment-response": call.settle_response,
                    "eraya/call": call.as_dict(client.guardian.policy.asset_decimals),
                },
            )

        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    return 0


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser, *, needs_url: bool = True) -> None:
    if needs_url:
        parser.add_argument(
            "--url", required=True, help="gateway MCP endpoint, e.g. https://host/mcp/"
        )
    parser.add_argument("--key", default="", help="agent private key (default: AGENT_WALLET_KEY)")
    parser.add_argument(
        "--ephemeral-key",
        action="store_true",
        help="mint a throwaway key: watch the protocol and the Guardian without funds",
    )
    parser.add_argument("--network", default="", help="CAIP-2 id, e.g. eip155:84532")
    parser.add_argument(
        "--allow-mainnet",
        action="store_true",
        help="permit signing on a mainnet chain id (off by default, deliberately)",
    )
    parser.add_argument("--label", default="brainwave-cli", help="agent label for the ledger")
    parser.add_argument("--session-budget", default=None, help="e.g. '$5.00', or 'none'")
    parser.add_argument("--per-call-max", default=None, help="e.g. '$0.10'")
    parser.add_argument("--daily-budget", default=None, help="e.g. '$50.00'")
    parser.add_argument("--escalate-above", default=None, help="e.g. '$1.00'")
    parser.add_argument(
        "--allow", action="append", default=None, help="allowlist pattern (repeatable)"
    )
    parser.add_argument(
        "--approve",
        choices=["auto", "ask", "deny"],
        default="deny",
        help="how to resolve escalations (default: deny -- fail closed)",
    )
    parser.add_argument(
        "--journal", default=None, help="path for the persistent daily-spend journal"
    )
    parser.add_argument("--no-require-receipt", action="store_true")
    parser.add_argument("--no-verify-receipts", action="store_true")
    parser.add_argument("--rpc", default="", help="RPC URL, to verify settlements on-chain")
    parser.add_argument("--attestor", default="", help="expected receipt attestor address")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.client",
        description="TRAPPIST x BRAINWAVE -- the buyer side: a paying MCP client with a spend policy.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("policy", help="print the effective spend policy and budget state")
    _add_common(p, needs_url=False)
    p.set_defaults(func=cmd_policy, is_async=False)

    p = sub.add_parser("economics", help="the fee-load argument, as arithmetic (no network)")
    p.add_argument("--price", default="$0.002", help="tool price per call")
    p.add_argument("--fee", default="$0.001", help="facilitator fee per settlement")
    p.add_argument("--calls", type=int, default=100)
    p.add_argument("--decimals", type=int, default=6)
    p.set_defaults(func=cmd_economics, is_async=False)

    p = sub.add_parser("verify", help="verify a receipt file (local, attested, on-chain)")
    p.add_argument("--receipt", required=True)
    p.add_argument("--rpc", default="")
    p.add_argument("--attestor", default="")
    p.add_argument("--payer", default="")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_verify, is_async=False)

    p = sub.add_parser("info", help="gateway_info (free) plus the tool count")
    _add_common(p)
    p.set_defaults(func=_info, is_async=True)

    p = sub.add_parser("tools", help="list the gateway's tools (free)")
    _add_common(p)
    p.set_defaults(func=_tools, is_async=True)

    p = sub.add_parser("quote", help="read a tool's price from its 402, without paying")
    _add_common(p)
    p.add_argument("--tool", required=True)
    p.add_argument("--args", default="{}")
    p.set_defaults(func=_quote, is_async=True)

    p = sub.add_parser("call", help="call one tool, paying for it, and print the protocol trace")
    _add_common(p)
    p.add_argument("--tool", required=True)
    p.add_argument("--args", default="{}")
    p.set_defaults(func=_call, is_async=True)

    p = sub.add_parser("simulate", help="N calls in one session, then the reconciliation")
    _add_common(p)
    p.add_argument("--tool", required=True)
    p.add_argument("--args", default="{}")
    p.add_argument("--calls", type=int, default=10)
    p.set_defaults(func=_simulate, is_async=True)

    p = sub.add_parser("proxy", help="stdio MCP server bridging a paid gateway into Claude Desktop")
    _add_common(p)
    p.set_defaults(func=_proxy, is_async=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        # stderr, always: `proxy` speaks JSON-RPC on stdout and one stray log
        # line there corrupts the stream.
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        if getattr(args, "is_async", False):
            return asyncio.run(args.func(args))
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
