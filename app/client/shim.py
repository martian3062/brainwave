"""THE BUYER SHIM -- a paying MCP client, in pure Python.

Scope statement first, because the honest version is more interesting than the
inflated one:

    THE x402 SDK ALREADY IMPLEMENTS PAID MCP ON BOTH SIDES.
    `x402.mcp.x402MCPClient` does the whole dance: call unpaid, detect the 402
    in the result, create a payment payload, retry with it in the MCP `_meta`
    key `x402/payment`, and read the settlement back out of
    `x402/payment-response`. We did not write that and do not claim to.

This module adds three things the SDK does not have, and one repair it needs.

  1. THE GUARDIAN, wired in at the two points that are provably pre-signature.
  2. Session accounting -- authorized vs captured vs settled, per call -- so the
     buyer can reconcile against the seller's ledger instead of trusting it.
  3. Receipt handling: pull the receipt out of the response, verify it, and
     enforce `require_receipt`.

And the repair:

--------------------------------------------------------------------------
WHY `_ClientSessionAdapter` EXISTS. IT IS NOT DECORATION.
--------------------------------------------------------------------------
The SDK ships two client classes and NEITHER works as documented against a
streamable-HTTP FastMCP server driven by the official `mcp` SDK. All three
findings below were reproduced against the installed wheels
(x402==2.16.0, mcp==1.28.1); each has a test in `tests/test_client.py`.

(a) `x402.mcp.create_x402_mcp_client` -- the factory the SDK's own README and
    module docstring show -- IS SSE-ONLY. Read `x402/mcp/client.py`: it does
    `from mcp.client.sse import sse_client`, appends `/sse` to the URL, and
    yields an `x402MCPSession`. Our gateway speaks streamable HTTP at `/mcp/`
    (see `app/mcp_app.py`), so the documented entry point cannot connect to it
    at all. Separately, `x402MCPSession` exposes NO hooks -- no
    `on_payment_required`, no approval callback -- so even if the transport
    matched, there is no seam to put a spend policy in.

(b) `x402MCPClient` (from `x402/mcp/client_async.py`) DOES have the hooks, but
    it calls the underlying client as `mcp_client.call_tool(params_dict)` --
    one positional dict. `mcp.ClientSession.call_tool` is
    `call_tool(name, arguments=None, ..., *, meta=None)`. Hand it the dict and
    `name` binds to a dict. It cannot drive a real `ClientSession`.

(c) Even bridged, `x402/mcp/utils.py::convert_mcp_result` does
    `getattr(mcp_result, "_meta", {})`. On `mcp.types.CallToolResult` the field
    is NAMED `meta` with `_meta` only as its serialization ALIAS, so that
    getattr always returns the default `{}`. Consequence: the settlement
    response is silently dropped and `MCPToolCallResult.payment_response` is
    always None -- the client pays and then reports no proof of payment.
    (`x402MCPSession._build_result` reads `result.meta` correctly, so the two
    client classes disagree with each other. This is a bug, not a design.)

So the adapter does exactly three things: translate the calling convention,
normalize the result object so the SDK's own extraction works, and nothing
else. Every byte of protocol handling stays in the SDK.

--------------------------------------------------------------------------
WHERE THE GUARDIAN SITS, AND WHY THOSE TWO PLACES
--------------------------------------------------------------------------
    x402MCPClient.call_tool
      +-- call the tool unpaid                      -> server returns the 402
      +-- _payment_required_hooks  <=== PHASE 1: Guardian.screen()
      |     abort here raises PaymentRequiredError
      +-- _on_payment_requested
      +-- _before_payment_hooks
      +-- payment_client.create_payment_payload
            +-- _select_requirements_v2              (which offer we will pay)
            +-- before_payment_creation hooks  <=== PHASE 2: Guardian.authorize()
            |     AbortResult here raises PaymentAbortedError
            +-- ExactEvmScheme.create_payment_payload
                  +-- _sign_authorization
                        +-- signer.sign_typed_data   <=== THE ONLY SIGNATURE
            +-- retry the tool with the payload in _meta

Phase 2 is registered on the PAYMENT CLIENT, not on the MCP client, so it also
covers anyone who bypasses this shim and drives `x402Client` directly.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.client.guardian import Decision, DeclineReason, Guardian, SpendPolicy, Verdict
from app.client.signer import AuditingSigner
from app.money import format_atomic

log = logging.getLogger("brainwave.shim")

__all__ = ["PaidMCPClient", "PaidCall", "ToolQuote", "connect"]


# --------------------------------------------------------------------------
# Per-call context, shared between the two Guardian phases
# --------------------------------------------------------------------------


@dataclass
class _CallContext:
    """What phase 1 learned, so phase 2 (which is given neither the tool name nor
    the arguments) can make a fully informed decision."""

    call_id: str
    tool: str
    arguments: dict[str, Any]
    resource: str = ""
    screen_verdict: Verdict | None = None
    authorize_verdict: Verdict | None = None
    #: The ceiling actually reserved, once phase 2 has selected a requirement.
    reserved_atomic: int = 0
    network: str = ""
    asset: str = ""
    scheme: str = ""


#: Per-task, so concurrent `call_tool`s cannot read each other's context.
_CURRENT: contextvars.ContextVar[_CallContext | None] = contextvars.ContextVar(
    "brainwave_call_context", default=None
)


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------


@dataclass
class ToolQuote:
    """What a tool costs, as the server quoted it. See `PaidMCPClient.quote`."""

    tool: str
    resource: str
    scheme: str
    network: str
    asset: str
    amount_atomic: int
    max_timeout_seconds: int
    pay_to: str
    asset_decimals: int = 6
    offers: int = 1

    @property
    def amount(self) -> str:
        return format_atomic(self.amount_atomic, self.asset_decimals)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "resource": self.resource,
            "scheme": self.scheme,
            "network": self.network,
            "asset": self.asset,
            "amountAtomic": self.amount_atomic,
            "amount": self.amount,
            "payTo": self.pay_to,
            "maxTimeoutSeconds": self.max_timeout_seconds,
            "offers": self.offers,
        }


@dataclass
class PaidCall:
    """One metered tool call, from the buyer's point of view.

    `authorized_atomic` and `captured_atomic` are kept apart deliberately: under
    `upto` they differ, and collapsing them into one "cost" is exactly the
    dishonesty this project exists to remove.
    """

    call_id: str
    tool: str
    arguments: dict[str, Any]
    decision: Decision = Decision.ALLOW
    decline_reason: DeclineReason | None = None
    content: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    is_error: bool = False
    paid: bool = False
    authorized_atomic: int = 0
    captured_atomic: int = 0
    refunded_atomic: int = 0
    signatures_produced: int = 0
    tx_hash: str | None = None
    settle_response: dict[str, Any] | None = None
    receipt: dict[str, Any] | None = None
    receipt_verification: Any = None
    verdicts: list[Verdict] = field(default_factory=list)
    error: str | None = None
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.decision is Decision.ALLOW and not self.is_error and self.error is None

    @property
    def declined(self) -> bool:
        return self.decision is Decision.DENY

    def as_dict(self, decimals: int = 6) -> dict[str, Any]:
        return {
            "callId": self.call_id,
            "tool": self.tool,
            "decision": str(self.decision),
            "declineReason": str(self.decline_reason) if self.decline_reason else None,
            "paid": self.paid,
            "authorized": format_atomic(self.authorized_atomic, decimals),
            "captured": format_atomic(self.captured_atomic, decimals),
            "refunded": format_atomic(self.refunded_atomic, decimals),
            "signatures": self.signatures_produced,
            "txHash": self.tx_hash,
            "hasReceipt": self.receipt is not None,
            "isError": self.is_error,
            "error": self.error,
            "elapsedMs": self.elapsed_ms,
        }

    def trace(self, decimals: int = 6) -> str:
        """The protocol trace, the way the submission shows it."""
        lines = [f"->  tools/call {self.tool} {json.dumps(self.arguments, default=str)[:80]}"]
        if self.authorized_atomic:
            lines.append(
                f"<-  402 Payment Required   max={format_atomic(self.authorized_atomic, decimals)}"
            )
        for verdict in self.verdicts:
            mark = "ok" if verdict.decision is Decision.ALLOW else "XX"
            lines.append(f"[{mark}] guardian/{verdict.phase}: {verdict.decision} {verdict.message}")
        if self.declined:
            lines.append(f"XX  declined ({self.decline_reason}) -- nothing was signed")
            return "\n".join(lines)
        if self.signatures_produced:
            lines.append(
                f"~   signed {self.signatures_produced} EIP-3009 authorization "
                "(no gas, no raw transaction)"
            )
            lines.append("->  retry  _meta['x402/payment'] = <signed payload>")
        if self.paid:
            lines.append(
                f"<-  200 OK   captured={format_atomic(self.captured_atomic, decimals)} of "
                f"{format_atomic(self.authorized_atomic, decimals)} authorized"
            )
        if self.tx_hash:
            lines.append(f"$   settled on-chain {self.tx_hash}")
        elif self.paid:
            lines.append("$   batched -- settles with the session at window close")
        if self.receipt:
            lines.append(f"R   receipt {self.receipt.get('receiptId', '<unnamed>')}")
        if self.error:
            lines.append(f"!   {self.error}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# The adapter (see the module docstring for why every line of it is needed)
# --------------------------------------------------------------------------


class _NormalizedResult:
    """A `CallToolResult` reshaped into what `x402.mcp.utils.convert_mcp_result` reads.

    Two fixes, both forced by real mismatches:

      * `_meta` -- `CallToolResult` names the field `meta` and only aliases it
        to `_meta`, so the SDK's `getattr(result, "_meta", {})` always misses
        and the settlement response vanishes. Exposed here under the name the
        SDK looks for.
      * `content` -- the SDK's fallback 402 extraction does
        `isinstance(item, dict) and item.get("type") == "text"`, but the real
        SDK hands back `TextContent` pydantic models. Dumped to dicts here so
        that fallback path works when a server puts the challenge only in the
        text content.
    """

    __slots__ = ("content", "isError", "structuredContent", "_meta", "raw")

    def __init__(self, result: Any) -> None:
        self.raw = result
        self.isError = bool(getattr(result, "isError", False))
        self.structuredContent = getattr(result, "structuredContent", None)
        meta = getattr(result, "meta", None)
        self._meta = dict(meta) if isinstance(meta, Mapping) else {}
        self.content = [_content_to_dict(item) for item in (getattr(result, "content", None) or [])]


def _content_to_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        try:
            return dump(by_alias=True, exclude_none=True)
        except Exception:
            pass
    return {"type": getattr(item, "type", "text"), "text": str(getattr(item, "text", item))}


class _ClientSessionAdapter:
    """`mcp.ClientSession`, presented under the convention `x402MCPClient` expects."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def call_tool(self, params: dict[str, Any], **kwargs: Any) -> _NormalizedResult:
        meta = params.get("_meta")
        result = await self._session.call_tool(
            name=params["name"],
            arguments=params.get("arguments") or {},
            # `ClientSession` puts this into `CallToolRequestParams._meta`, whose
            # model is `extra="allow"` -- verified, so the non-identifier key
            # "x402/payment" survives the round trip intact.
            meta=meta if isinstance(meta, dict) and meta else None,
            **kwargs,
        )
        return _NormalizedResult(result)

    async def list_tools(self) -> Any:
        return await self._session.list_tools()


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------


def _largest_amount(payment_required: Any) -> tuple[int | None, Any]:
    """The worst-case offer, for phase-1 screening.

    The payment client has not chosen a requirement yet, so the only safe number
    to judge is the largest one on the table. Returns `(None, req)` when an
    amount will not parse -- the Guardian turns that into a DENY rather than
    letting an unparseable price through as zero.
    """
    worst: int | None = None
    worst_req: Any = None
    unparseable = False
    for requirement in getattr(payment_required, "accepts", None) or []:
        raw = getattr(requirement, "amount", None)
        if raw is None and hasattr(requirement, "get_amount"):
            raw = requirement.get_amount()
        try:
            value = int(str(raw))
        except (TypeError, ValueError):
            unparseable = True
            continue
        if worst is None or value > worst:
            worst, worst_req = value, requirement
    if unparseable and worst is None:
        return None, None
    return worst, worst_req


def _resource_url(payment_required: Any, tool: str) -> str:
    resource = getattr(payment_required, "resource", None)
    url = getattr(resource, "url", None) if resource is not None else None
    return str(url) if url else f"mcp://tool/{tool}"


def _settle_amount(settle_response: Any) -> int | None:
    """`SettleResponse.amount` is an optional string of atomic units."""
    raw = getattr(settle_response, "amount", None)
    if raw is None and isinstance(settle_response, Mapping):
        raw = settle_response.get("amount")
    if raw is None:
        return None
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


def _extract_receipt(result: Any) -> dict[str, Any] | None:
    """Find the receipt the gateway returned inside a successful tool response.

    In practice the TEXT path is the one that fires: `x402MCPClient.call_tool`
    returns an `MCPToolCallResult`, which carries only `content`, `is_error`,
    `payment_response` and `payment_made` -- `structuredContent` does not
    survive that hop. The structured branch is kept for callers that hand us a
    result straight off the wire, and because it costs one `isinstance`.

    A receipt is recognised by carrying `receiptId`, the field
    `app.models.Receipt` makes unique. Accepted either at the top level or
    nested under a `receipt` key, because whether a tool returns a bare receipt
    or wraps it beside its own output is the tool author's choice, not ours.
    """
    structured = getattr(result, "structured_content", None) or getattr(
        result, "structuredContent", None
    )
    for candidate in (
        structured,
        *(_json_of(item) for item in (getattr(result, "content", None) or [])),
    ):
        if not isinstance(candidate, Mapping):
            continue
        if "receiptId" in candidate:
            return dict(candidate)
        inner = candidate.get("receipt")
        if isinstance(inner, Mapping) and "receiptId" in inner:
            return dict(inner)
    return None


def _json_of(item: Any) -> Any:
    text = item.get("text") if isinstance(item, Mapping) else getattr(item, "text", None)
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _text_of(content: list[dict[str, Any]]) -> str:
    return "\n".join(str(c.get("text", "")) for c in content if c.get("type") == "text").strip()


class PaidMCPClient:
    """An MCP client that pays for tools, under a spend policy, in pure Python.

    Construct with `PaidMCPClient.connect(...)`, which is an async context
    manager owning the transport, the MCP session and the payment client:

        async with PaidMCPClient.connect(url, guardian=g, signer=s) as client:
            call = await client.call_tool("run_injection_attack_sim", {...})
            print(call.trace())
    """

    def __init__(
        self,
        *,
        session: Any,
        payment_client: Any,
        x402_mcp: Any,
        guardian: Guardian,
        signer: AuditingSigner,
        network: str,
        agent_label: str = "brainwave-agent",
        verify_receipts: bool = True,
        attestor: str | None = None,
        rpc_url: str | None = None,
    ) -> None:
        self._session = session
        self._payment_client = payment_client
        self._x402 = x402_mcp
        self.guardian = guardian
        self.signer = signer
        self.network = network
        self.agent_label = agent_label
        self.verify_receipts = verify_receipts
        self._attestor = attestor
        self._rpc_url = rpc_url
        self.session_id = f"sess_{uuid.uuid4().hex[:20]}"
        self.calls: list[PaidCall] = []

    # ------------------------------------------------------------ lifecycle

    @classmethod
    @contextlib.asynccontextmanager
    async def connect(
        cls,
        url: str,
        *,
        guardian: Guardian | None = None,
        signer: AuditingSigner | None = None,
        network: str = "",
        agent_label: str = "brainwave-agent",
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        verify_receipts: bool = True,
        attestor: str | None = None,
        rpc_url: str | None = None,
    ) -> AsyncIterator[PaidMCPClient]:
        """Open a paying session against a streamable-HTTP MCP gateway.

        `url` is the gateway's MCP endpoint including the trailing slash --
        `https://host/mcp/`. The bare `/mcp` also works because the gateway
        answers it with a 307 (see `app/main.py`), but naming the canonical one
        saves a round trip.
        """
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        from x402 import x402Client
        from x402.mechanisms.evm.exact import register_exact_evm_client

        from app.config import settings

        network = network or settings.x402_network
        guardian = guardian or Guardian(SpendPolicy.from_settings())
        if signer is None:
            from app.client.signer import load_signer

            signer = load_signer(network=network)

        async with contextlib.AsyncExitStack() as stack:
            read, write, _get_session_id = await stack.enter_async_context(
                streamablehttp_client(url, headers=headers or {}, timeout=timeout)
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()

            payment_client = x402Client()
            # Register ONLY the intended network. `register_exact_evm_client`
            # defaults to the wildcard "eip155:*", which would make the client
            # willing to sign on any EVM chain a server names.
            register_exact_evm_client(payment_client, signer, networks=[network])
            _register_upto(payment_client, signer, network)

            client = cls(
                session=session,
                payment_client=payment_client,
                x402_mcp=None,
                guardian=guardian,
                signer=signer,
                network=network,
                agent_label=agent_label,
                verify_receipts=verify_receipts,
                attestor=attestor,
                rpc_url=rpc_url,
            )
            client._x402 = client._wire(_ClientSessionAdapter(session), payment_client)
            log.info(
                "paying session %s open against %s as %s",
                client.session_id,
                url,
                signer.address,
            )
            yield client

    def _wire(self, adapter: Any, payment_client: Any) -> Any:
        """Attach the SDK's MCP payment client and put the Guardian in both phases."""
        from x402.mcp import wrap_mcp_client_with_payment

        x402_mcp = wrap_mcp_client_with_payment(adapter, payment_client, auto_payment=True)
        x402_mcp.on_payment_required(self._phase1_screen)
        x402_mcp.on_after_payment(self._after_payment)
        payment_client.on_before_payment_creation(self._phase2_authorize)
        return x402_mcp

    # -------------------------------------------------------- Guardian hooks

    async def _phase1_screen(self, context: Any) -> Any:
        """`x402MCPClient.on_payment_required` -- runs before any payload exists."""
        from x402.mcp.types import PaymentRequiredHookResult

        call = _CURRENT.get()
        payment_required = context.payment_required
        amount, requirement = _largest_amount(payment_required)
        resource = _resource_url(payment_required, context.tool_name)
        if call is not None:
            call.resource = resource

        verdict = self.guardian.screen(
            amount_atomic=amount,
            tool=context.tool_name,
            resource=resource,
            network=str(getattr(requirement, "network", "") or ""),
            asset=str(getattr(requirement, "asset", "") or ""),
        )
        if call is not None:
            call.screen_verdict = verdict

        if verdict.decision is Decision.DENY:
            # abort=True makes the SDK raise PaymentRequiredError from inside
            # call_tool, before `create_payment_payload` is ever reached.
            return PaymentRequiredHookResult(abort=True)
        # ESCALATE is not resolved here: asking the human belongs in phase 2,
        # where the amount is the exact one we would actually pay.
        return None

    async def _phase2_authorize(self, context: Any) -> Any:
        """`x402Client.on_before_payment_creation` -- the last thing before signing.

        Returning `AbortResult` raises `PaymentAbortedError` from step 3 of
        `_create_payment_payload_v2_core`; step 5, the only call site of
        `signer.sign_typed_data`, is never reached.
        """
        from x402.schemas import AbortResult

        call = _CURRENT.get()
        requirement = context.selected_requirements
        try:
            amount: int | None = int(str(requirement.amount))
        except (TypeError, ValueError, AttributeError):
            amount = None

        resource = _resource_url(context.payment_required, call.tool if call else "")
        call_id = call.call_id if call else f"call_{uuid.uuid4().hex[:16]}"

        verdict = await self.guardian.authorize(
            call_id,
            amount_atomic=amount,
            tool=call.tool if call else "",
            resource=resource,
            network=str(getattr(requirement, "network", "") or ""),
            asset=str(getattr(requirement, "asset", "") or ""),
            arguments=call.arguments if call else {},
        )
        if call is not None:
            call.authorize_verdict = verdict
            call.network = str(getattr(requirement, "network", "") or "")
            call.asset = str(getattr(requirement, "asset", "") or "")
            call.scheme = str(getattr(requirement, "scheme", "") or "")
            if verdict.decision is Decision.ALLOW:
                call.reserved_atomic = amount or 0

        if verdict.decision is not Decision.ALLOW:
            return AbortResult(
                reason=str(verdict.reason or DeclineReason.PER_CALL_MAX),
                message=verdict.message,
            )
        return None

    async def _after_payment(self, context: Any) -> None:
        """Observation only. The money is already gone by the time this runs."""
        settle = context.settle_response
        if settle is not None:
            log.info(
                "settled %s tx=%s",
                context.tool_name,
                getattr(settle, "transaction", None),
            )

    # ------------------------------------------------------------- calling --

    async def list_tools(self) -> list[dict[str, Any]]:
        """Free. Tool discovery is never paywalled."""
        result = await self._session.list_tools()
        return [
            {
                "name": tool.name,
                "description": tool.description or "",
                "inputSchema": tool.inputSchema,
            }
            for tool in result.tools
        ]

    async def quote(self, tool: str, arguments: dict[str, Any] | None = None) -> ToolQuote | None:
        """Read a tool's price from its 402 challenge, without paying.

        WARNING, and it is the SDK's warning too: the only way to see a tool's
        payment requirements is to CALL it. A well-built paid tool answers an
        unpaid call with the challenge and does no work -- that is what
        `create_payment_wrapper` does -- but a badly built one may have side
        effects before it checks for payment. Quoting is not free of risk, only
        free of charge.
        """
        adapter = _ClientSessionAdapter(self._session)
        result = await adapter.call_tool({"name": tool, "arguments": arguments or {}})

        from x402.mcp.utils import convert_mcp_result, extract_payment_required_from_result

        payment_required = extract_payment_required_from_result(convert_mcp_result(result))
        if payment_required is None:
            return None
        amount, requirement = _largest_amount(payment_required)
        if requirement is None:
            return None
        return ToolQuote(
            tool=tool,
            resource=_resource_url(payment_required, tool),
            scheme=str(getattr(requirement, "scheme", "")),
            network=str(getattr(requirement, "network", "")),
            asset=str(getattr(requirement, "asset", "")),
            amount_atomic=amount or 0,
            max_timeout_seconds=int(getattr(requirement, "max_timeout_seconds", 0) or 0),
            pay_to=str(getattr(requirement, "pay_to", "")),
            asset_decimals=self.guardian.policy.asset_decimals,
            offers=len(getattr(payment_required, "accepts", []) or []),
        )

    async def call_tool(self, tool: str, arguments: dict[str, Any] | None = None) -> PaidCall:
        """Call a tool, paying for it if the Guardian allows.

        Never raises for a policy refusal: a denied call is a normal outcome of
        an agent run, not an exception, and returning it as data is what lets a
        caller keep going with the next tool. Transport failures still raise.
        """
        from x402.mcp.types import PaymentRequiredError
        from x402.schemas import PaymentAbortedError

        arguments = arguments or {}
        context = _CallContext(
            call_id=f"call_{uuid.uuid4().hex[:16]}", tool=tool, arguments=arguments
        )
        token = _CURRENT.set(context)
        signatures_before = self.signer.count
        started = time.perf_counter()

        call = PaidCall(call_id=context.call_id, tool=tool, arguments=arguments)
        try:
            result = await self._x402.call_tool(tool, arguments)
            call.content = list(result.content or [])
            call.text = _text_of(call.content)
            call.is_error = bool(result.is_error)
            call.paid = bool(result.payment_made)
            settle = result.payment_response
            if settle is not None:
                call.settle_response = (
                    settle.model_dump(by_alias=True, exclude_none=True)
                    if hasattr(settle, "model_dump")
                    else dict(settle)
                )
                call.tx_hash = call.settle_response.get("transaction") or None
            call.receipt = _extract_receipt(result)

        except PaymentRequiredError as exc:
            # Phase 1 aborted, or the SDK could not pay. Either way: unpaid.
            call.decision = Decision.DENY
            verdict = context.screen_verdict
            call.decline_reason = verdict.reason if verdict else None
            call.error = str(exc)
        except PaymentAbortedError as exc:
            # Phase 2 aborted -- inside create_payment_payload, before signing.
            call.decision = Decision.DENY
            verdict = context.authorize_verdict
            call.decline_reason = verdict.reason if verdict else None
            call.error = str(exc)
        except Exception as exc:  # transport, protocol, tool crash
            call.error = f"{type(exc).__name__}: {exc}"
            call.is_error = True
            log.exception("call_tool(%s) failed", tool)
        finally:
            _CURRENT.reset(token)
            call.elapsed_ms = int((time.perf_counter() - started) * 1000)
            call.signatures_produced = self.signer.count - signatures_before
            call.authorized_atomic = context.reserved_atomic
            call.verdicts = [
                v for v in (context.screen_verdict, context.authorize_verdict) if v is not None
            ]
            self._resolve_reservation(context, call)

        if call.paid:
            self._check_receipt(context, call)

        self.calls.append(call)
        return call

    # --------------------------------------------------------- bookkeeping --

    def _resolve_reservation(self, context: _CallContext, call: PaidCall) -> None:
        """Close out the budget reservation, failing closed on every ambiguity.

        The rule that makes this safe: a reservation may only be RELEASED if no
        signature was produced. `AuditingSigner` counts signatures exactly, so
        this is a fact and not an inference from which exception came back --
        and if `create_payment_payload` ever fails AFTER signing, the money
        stays booked instead of being silently refunded.
        """
        if not context.reserved_atomic:
            return

        if call.signatures_produced == 0:
            released = self.guardian.release(context.call_id)
            call.refunded_atomic = released
            call.authorized_atomic = 0
            log.debug("call %s released %d (no signature was produced)", context.call_id, released)
            return

        captured = _settle_amount(call.settle_response)
        if captured is None and call.receipt is not None:
            try:
                captured = int(str(call.receipt.get("capturedAtomic")))
            except (TypeError, ValueError):
                captured = None
        if captured is None:
            # A signature exists and we cannot prove what was taken. Book the
            # whole ceiling: under-counting exposure is the failure that costs
            # money, over-counting only costs a smaller budget.
            captured = context.reserved_atomic
            if call.paid:
                log.warning(
                    "call %s paid but reported no captured amount -- charging the full "
                    "authorized ceiling %d against the budget",
                    context.call_id,
                    captured,
                )

        call.captured_atomic = captured
        call.refunded_atomic = self.guardian.commit(context.call_id, captured)

    def _check_receipt(self, context: _CallContext, call: PaidCall) -> None:
        """`require_receipt`, plus verification of whatever receipt did arrive."""
        from app.client.verify import verify_receipt

        if call.receipt is not None and self.verify_receipts:
            call.receipt_verification = verify_receipt(
                call.receipt,
                expected_attestor=self._attestor,
                rpc_url=self._rpc_url,
                expected_payer=self.signer.address,
            )
            if not call.receipt_verification.verified:
                failures = "; ".join(c.detail for c in call.receipt_verification.failures)
                log.error("receipt for %s did not verify: %s", call.tool, failures)
                self.guardian.freeze(f"unverifiable receipt from {call.tool}: {failures}")

        verdict = self.guardian.record_receipt(
            call_id=context.call_id,
            has_receipt=call.receipt is not None,
            tool=call.tool,
            resource=context.resource,
        )
        if verdict is not None:
            call.verdicts.append(verdict)

    # ------------------------------------------------------------- readouts

    def session_snapshot(self) -> dict[str, Any]:
        """Everything the buyer knows about this session. Mirrors the seller's
        ledger closely enough to be diffed against it -- which is the point:
        reconciliation, not trust."""
        decimals = self.guardian.policy.asset_decimals
        authorized = sum(c.authorized_atomic for c in self.calls)
        captured = sum(c.captured_atomic for c in self.calls)
        settled = sum(c.captured_atomic for c in self.calls if c.tx_hash)
        return {
            "sessionId": self.session_id,
            "agentLabel": self.agent_label,
            "payer": self.signer.address,
            "network": self.network,
            "calls": len(self.calls),
            "paid": sum(1 for c in self.calls if c.paid),
            "declined": sum(1 for c in self.calls if c.declined),
            "authorized": format_atomic(authorized, decimals),
            "captured": format_atomic(captured, decimals),
            "settledOnChain": format_atomic(settled, decimals),
            "pendingBatch": format_atomic(captured - settled, decimals),
            # The independent check: the signer's own tally of every bearer
            # instrument it produced. If this disagrees with `authorized`, the
            # Guardian's books are wrong and the signer's are right.
            "signatures": self.signer.count,
            "signedTotal": format_atomic(self.signer.authorized_total(), decimals),
            "guardian": self.guardian.snapshot(),
        }


def _register_upto(payment_client: Any, signer: Any, network: str) -> None:
    """Register the `upto` scheme too, when the SDK exposes a client for it.

    `upto` is the scheme that makes metered tools honest -- authorize a ceiling,
    capture actual consumption -- so a buyer that only speaks `exact` cannot pay
    for the most interesting half of the catalogue. There is no
    `register_upto_evm_client` helper upstream (unlike `exact`), so it is
    registered by hand.
    """
    try:
        from x402.mechanisms.evm.upto import UptoEvmClientScheme
    except ImportError:  # pragma: no cover
        log.debug("x402 has no upto client scheme; exact only")
        return
    try:
        payment_client.register(network, UptoEvmClientScheme(signer))
    except Exception:
        log.exception("could not register the upto scheme; exact only")


#: Module-level alias so a caller can `from app.client.shim import connect`.
connect = PaidMCPClient.connect
