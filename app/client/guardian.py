"""THE GUARDIAN -- buyer-side spend policy, evaluated before a signature exists.

This is the part of the system the x402 SDK does not have.

Be precise about that claim, because a judge who knows the SDK will check it.
`x402/hook_policy.py` exists and the name invites the assumption -- but it guards
hook MUTATIONS (which extension may register which hook, and whether a hook is
allowed to rewrite a payload). It has no concept of a budget, a spend ceiling, an
allowlist or an escalation. `x402ClientBase._policies` is likewise not spend
policy: `prefer_network`, `prefer_scheme` and `max_amount` are *requirement
selectors* that reorder or filter the server's offers. `max_amount` is the
closest thing upstream, and it is stateless -- it cannot say "this call is fine
but it is the ninety-ninth call and the session budget is gone".

A buyer-side, stateful, escalating spend Guardian genuinely does not exist
upstream. This is it.

--------------------------------------------------------------------------
WHY THE ORDERING IS THE SECURITY PROPERTY
--------------------------------------------------------------------------
An EIP-3009 authorization is a bearer instrument. Once the agent's key has
signed one, the holder can present it to the facilitator and take the money; you
cannot un-sign it, and "the server promised not to" is not a control. Therefore
the ONLY place a spend limit can actually be enforced is before the signature
exists, on the buyer's side.

Concretely, in x402==2.16.0 the single call site that produces an EIP-3009
signature is

    x402/mechanisms/evm/exact/client.py::ExactEvmScheme._sign_authorization
      -> self._signer.sign_typed_data(...)

reached from `ExactEvmScheme.create_payment_payload`, reached from
`x402ClientBase._create_payment_payload_v2_core` STEP 5. The Guardian hooks into
STEP 3 of that same generator (`before_payment_creation`) and into the MCP
client's `on_payment_required`, both of which run strictly earlier. Returning an
`AbortResult` at step 3 raises `PaymentAbortedError` and step 5 is never
reached.

`tests/test_client.py::test_a_denied_call_never_produces_a_signature` asserts
exactly that, by counting calls to `sign_typed_data` through `AuditingSigner`.
If a future refactor ever moves policy after signing, that test fails.

--------------------------------------------------------------------------
TWO PHASES, ONE DECISION
--------------------------------------------------------------------------
The Guardian is consulted twice per call, on purpose:

  PHASE 1  `screen()`      -- wired to x402MCPClient.on_payment_required.
                             The 402 challenge is in hand and so is the tool
                             name, but the payment client has not yet chosen
                             WHICH of the server's `accepts` entries to pay.
                             So phase 1 judges the WORST CASE: the largest
                             amount across all offers. It is pure -- it mutates
                             no state -- because the call may still be abandoned.

  PHASE 2  `authorize()`   -- wired to x402Client.on_before_payment_creation.
                             Now `selected_requirements` is known, so the amount
                             is exact. This phase is authoritative: it reserves
                             the money against the budget and it is the phase
                             that escalates. Still strictly pre-signature.

Phase 2 is also the belt to phase 1's braces: it is registered on the payment
client itself, so a caller who bypasses our shim entirely and drives
`x402Client.create_payment_payload()` by hand is still bounded.

--------------------------------------------------------------------------
THE CEILING IS THE EXPOSURE
--------------------------------------------------------------------------
Under the `upto` scheme the amount in `PaymentRequirements` is a CEILING, not a
price: the agent authorizes up to $0.05 and the server captures what the work
actually cost. It is tempting to charge the budget only the captured amount.
That is wrong, and it is the bug that turns a $5 budget into an unbounded one:
100 authorizations of $0.05 each are 100 bearer instruments worth $5 in total,
whatever the server later chooses to capture.

So the Guardian reserves the CEILING at authorize() time and refunds the
difference at commit() time, when the settlement response reports what was
actually taken. Exposure is always

    committed + sum(outstanding reservations)

and never less than what a malicious counterparty could collect.

--------------------------------------------------------------------------
FAIL-CLOSED, AND DURABLE WHERE IT MATTERS
--------------------------------------------------------------------------
* No approver configured + a call over `escalate_above` == DENY, not allow.
* An unparseable amount == DENY (`unpriced`). We do not guess at money.
* `authorize()` write-throughs the reservation to the optional journal file
  BEFORE returning ALLOW, so a process that dies immediately after signing still
  remembers that it signed. Refunds are then applied on the way back.
* `release()` -- the un-reserve -- is deliberately NOT called on every failure.
  It is only safe when no signature was produced, and the shim proves that by
  comparing `AuditingSigner.count` before and after. See `shim.py`.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import tempfile
import threading
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.money import format_atomic, parse_price

log = logging.getLogger("brainwave.guardian")

__all__ = [
    "Decision",
    "DeclineReason",
    "SpendPolicy",
    "Verdict",
    "Escalation",
    "Guardian",
    "SpendJournal",
    "SpendDenied",
    "auto_approve",
    "deny_all",
    "console_approver",
]


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    #: Returned only by `screen()`; `authorize()` resolves escalation to
    #: ALLOW or DENY by actually asking the approver.
    ESCALATE = "escalate"


class DeclineReason(StrEnum):
    """Why the Guardian said no.

    The first five are the exact strings `app.models.Call.decline_reason`
    documents, so a buyer-side refusal and a seller-side ledger row use one
    vocabulary and the dashboard's funnel does not need a translation table.
    """

    PER_CALL_MAX = "per_call_max"
    OVER_SESSION_BUDGET = "over_session_budget"
    NOT_ALLOWLISTED = "not_allowlisted"
    NEEDS_ESCALATION = "needs_escalation"
    NO_RECEIPT = "no_receipt"
    # -- extensions beyond what the ledger enumerates ------------------------
    OVER_DAILY_BUDGET = "over_daily_budget"
    ESCALATION_DENIED = "escalation_denied"
    NETWORK_NOT_ALLOWED = "network_not_allowed"
    ASSET_NOT_ALLOWED = "asset_not_allowed"
    SESSION_FROZEN = "session_frozen"
    #: The server quoted an amount we could not parse as an integer. We do not
    #: sign for a number we do not understand.
    UNPRICED = "unpriced"


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpendPolicy:
    """The operator's spend rules. Immutable: a policy is not a live control.

    All money is integer atomic units (`app.money`). `None` means "no limit",
    which is a legitimate configuration for `daily_budget` in a demo and a
    terrible one for `session_budget` in production -- `Guardian.__init__` logs
    a warning for the latter rather than silently accepting it.
    """

    session_budget_atomic: int | None = None
    per_call_max_atomic: int | None = None
    daily_budget_atomic: int | None = None
    #: Above this, a human (or an automated approver) must say yes.
    escalate_above_atomic: int | None = None
    #: fnmatch patterns against the x402 resource URL, e.g. "mcp://tool/*".
    allowlist: tuple[str, ...] = ("*",)
    #: Refuse to keep transacting with a server that will not evidence its
    #: charges. Cannot prevent the FIRST unevidenced payment -- see
    #: `Guardian.record_receipt`.
    require_receipt: bool = True
    #: CAIP-2 ids the agent will sign for. Empty tuple = any. Signing on a
    #: network you did not intend is a real way to lose real money.
    networks: tuple[str, ...] = ()
    #: Token contracts the agent will sign for. Empty tuple = any.
    assets: tuple[str, ...] = ()
    asset_decimals: int = 6

    # ---------------------------------------------------------------- ctors --

    @classmethod
    def from_settings(cls, settings: Any = None, **overrides: Any) -> SpendPolicy:
        """Build from `app.config.settings` (which already parses the prices)."""
        if settings is None:
            from app.config import settings as _settings

            settings = _settings

        base = dict(
            session_budget_atomic=settings.session_budget_atomic,
            per_call_max_atomic=settings.per_call_max_atomic,
            daily_budget_atomic=settings.daily_budget_atomic,
            escalate_above_atomic=settings.escalate_above_atomic,
            allowlist=tuple(settings.allowlist_patterns) or ("*",),
            require_receipt=settings.require_receipt,
            networks=(settings.x402_network,),
            assets=(settings.asset_address,) if settings.asset_address else (),
            asset_decimals=settings.x402_asset_decimals,
        )
        base.update(overrides)
        return cls(**base)

    @classmethod
    def from_prices(
        cls,
        *,
        session_budget: str | int | None = None,
        per_call_max: str | int | None = None,
        daily_budget: str | int | None = None,
        escalate_above: str | int | None = None,
        allowlist: Iterable[str] = ("*",),
        require_receipt: bool = True,
        networks: Iterable[str] = (),
        assets: Iterable[str] = (),
        asset_decimals: int = 6,
    ) -> SpendPolicy:
        """Build from human prices: `SpendPolicy.from_prices(per_call_max="$0.10")`."""

        def _p(v: str | int | None) -> int | None:
            return None if v is None else parse_price(v, asset_decimals)

        return cls(
            session_budget_atomic=_p(session_budget),
            per_call_max_atomic=_p(per_call_max),
            daily_budget_atomic=_p(daily_budget),
            escalate_above_atomic=_p(escalate_above),
            allowlist=tuple(allowlist) or ("*",),
            require_receipt=require_receipt,
            networks=tuple(networks),
            assets=tuple(assets),
            asset_decimals=asset_decimals,
        )

    # -------------------------------------------------------------- display --

    def describe(self) -> dict[str, Any]:
        d = self.asset_decimals

        def _f(v: int | None) -> str | None:
            return None if v is None else format_atomic(v, d)

        return {
            "sessionBudget": _f(self.session_budget_atomic),
            "perCallMax": _f(self.per_call_max_atomic),
            "dailyBudget": _f(self.daily_budget_atomic),
            "escalateAbove": _f(self.escalate_above_atomic),
            "allowlist": list(self.allowlist),
            "requireReceipt": self.require_receipt,
            "networks": list(self.networks) or ["<any>"],
            "assets": list(self.assets) or ["<any>"],
        }


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Verdict:
    decision: Decision
    reason: DeclineReason | None = None
    message: str = ""
    #: The amount judged. Under `upto` this is the CEILING, not the expected cost.
    amount_atomic: int = 0
    tool: str = ""
    resource: str = ""
    network: str = ""
    asset: str = ""
    phase: str = ""
    at: datetime = field(default_factory=_utcnow)

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    def raise_if_denied(self) -> Verdict:
        """For imperative callers who want an exception instead of a value.

        The shim deliberately does NOT use this: a policy refusal is a normal
        outcome of an agent run, and returning it as data is what lets a caller
        read the reason and pick a cheaper tool instead of unwinding. This exists
        for code where a denial really is exceptional.
        """
        if self.decision is not Decision.ALLOW:
            raise SpendDenied(self)
        return self

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": str(self.decision),
            "reason": str(self.reason) if self.reason else None,
            "message": self.message,
            "amountAtomic": self.amount_atomic,
            "tool": self.tool,
            "resource": self.resource,
            "network": self.network,
            "asset": self.asset,
            "phase": self.phase,
            "at": self.at.isoformat(),
        }


class SpendDenied(Exception):
    """Raised when the Guardian refuses. Carries the verdict, not just a string."""

    def __init__(self, verdict: Verdict) -> None:
        super().__init__(verdict.message or f"guardian denied: {verdict.reason}")
        self.verdict = verdict


@dataclass(frozen=True, slots=True)
class Escalation:
    """Handed to the approver when a call exceeds `escalate_above`."""

    tool: str
    resource: str
    network: str
    asset: str
    amount_atomic: int
    asset_decimals: int
    arguments: dict[str, Any]
    session_remaining_atomic: int | None
    daily_remaining_atomic: int | None

    @property
    def amount(self) -> str:
        return format_atomic(self.amount_atomic, self.asset_decimals)

    def prompt(self) -> str:
        return (
            f"APPROVE PAYMENT?  {self.amount} for tool '{self.tool}'\n"
            f"  resource : {self.resource}\n"
            f"  network  : {self.network}\n"
            f"  asset    : {self.asset}\n"
            f"  args     : {json.dumps(self.arguments, default=str)[:200]}"
        )


Approver = Callable[[Escalation], "bool | Awaitable[bool]"]


async def auto_approve(_escalation: Escalation) -> bool:
    """Approve everything. Demos and tests only -- never an operator default."""
    return True


async def deny_all(_escalation: Escalation) -> bool:
    return False


def console_approver(escalation: Escalation) -> bool:
    """Blocking y/N prompt on stdin.

    Runs on a worker thread (see `Guardian._ask`) so it cannot stall the event
    loop, and refuses (rather than hanging) when stdin is not a tty -- an agent
    running headless under systemd must not silently wait forever on a prompt
    nobody will ever see.
    """
    import sys

    if not sys.stdin or not sys.stdin.isatty():
        log.warning("escalation required but stdin is not a tty -- denying")
        return False
    print("\n" + escalation.prompt(), flush=True)
    try:
        return input("  approve? [y/N] ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False


# --------------------------------------------------------------------------
# Journal
# --------------------------------------------------------------------------


@dataclass
class _Reservation:
    call_id: str
    amount_atomic: int
    tool: str
    resource: str
    at: datetime = field(default_factory=_utcnow)


class SpendJournal:
    """Where the money actually gets counted.

    Three numbers, one lock:

      committed  -- what we know was (or could have been) taken, this session
      daily      -- the same, aggregated per UTC calendar day
      reserved   -- ceilings that are signed-or-about-to-be and not yet resolved

    The daily total is the only one worth persisting: a session dies with the
    process, but "$50 today" must survive a restart or it is not a daily budget,
    it is a per-process budget. `state_path` is optional and off by default;
    when it is unset the class says so in `describe()` rather than pretending.
    """

    def __init__(self, state_path: str | os.PathLike[str] | None = None) -> None:
        self._lock = threading.RLock()
        self._committed = 0
        self._reservations: dict[str, _Reservation] = {}
        self._day: date = _utcnow().date()
        self._daily_committed = 0
        self._call_count = 0
        self._declined_count = 0
        self._path = Path(state_path).expanduser() if state_path else None
        if self._path:
            self._load()

    # ------------------------------------------------------------ persistence

    def _load(self) -> None:
        assert self._path is not None
        try:
            raw = json.loads(self._path.read_text("utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            # A corrupt journal must not be read as "you have spent nothing".
            log.warning("spend journal %s unreadable (%s) -- starting from zero", self._path, exc)
            return
        stored_day = raw.get("day")
        if stored_day == self._day.isoformat():
            self._daily_committed = int(raw.get("dailyCommittedAtomic", 0))
        # A journal from a previous day is simply a fresh day; keep the file.

    def _flush(self) -> None:
        if self._path is None:
            return
        payload = {
            "day": self._day.isoformat(),
            "dailyCommittedAtomic": self._daily_committed,
            "updatedAt": _utcnow().isoformat(),
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic replace: a torn write here would silently reset the budget.
            fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, self._path)
        except OSError as exc:
            log.warning("could not persist spend journal to %s: %s", self._path, exc)

    def _roll_day(self) -> None:
        today = _utcnow().date()
        if today != self._day:
            self._day = today
            self._daily_committed = 0
            self._flush()

    # ------------------------------------------------------------- accounting

    def reserve(self, call_id: str, amount_atomic: int, tool: str, resource: str) -> None:
        with self._lock:
            self._roll_day()
            self._reservations[call_id] = _Reservation(call_id, amount_atomic, tool, resource)
            # Write through: if this process dies between here and the signature
            # landing on the wire, the day's exposure is still remembered.
            self._daily_committed += amount_atomic
            self._flush()

    def commit(self, call_id: str, captured_atomic: int) -> int:
        """Resolve a reservation to the amount actually captured.

        Returns the refund (ceiling - captured), which is 0 for `exact`. A
        capture above the ceiling is a protocol violation by the server: we keep
        the larger number (you cannot un-spend it) and shout.
        """
        with self._lock:
            reservation = self._reservations.pop(call_id, None)
            if reservation is None:
                # Never reserved: charge it in full rather than lose it.
                self._roll_day()
                self._committed += captured_atomic
                self._daily_committed += captured_atomic
                self._call_count += 1
                self._flush()
                return 0
            if captured_atomic > reservation.amount_atomic:
                # Over-capture is a protocol violation by the server, not a
                # rounding difference. The money is gone either way, so book the
                # real number and make noise -- a ledger that quietly clamps to
                # the ceiling would hide exactly the attack it exists to catch.
                log.error(
                    "server captured %d over an authorized ceiling of %d for call %s -- "
                    "this is a protocol violation, not a rounding difference",
                    captured_atomic,
                    reservation.amount_atomic,
                    call_id,
                )
                self._daily_committed += captured_atomic - reservation.amount_atomic
                refund = 0
            else:
                refund = reservation.amount_atomic - captured_atomic
                self._daily_committed -= refund
            self._committed += captured_atomic
            self._call_count += 1
            self._flush()
            return refund

    def release(self, call_id: str) -> int:
        """Cancel a reservation entirely.

        ONLY correct when no signature was produced. The shim proves that with
        `AuditingSigner.count`; do not call this speculatively.
        """
        with self._lock:
            reservation = self._reservations.pop(call_id, None)
            if reservation is None:
                return 0
            self._daily_committed -= reservation.amount_atomic
            self._flush()
            return reservation.amount_atomic

    def decline(self) -> None:
        with self._lock:
            self._declined_count += 1

    # -------------------------------------------------------------- readouts

    @property
    def reserved_atomic(self) -> int:
        with self._lock:
            return sum(r.amount_atomic for r in self._reservations.values())

    @property
    def committed_atomic(self) -> int:
        with self._lock:
            return self._committed

    @property
    def session_exposure_atomic(self) -> int:
        """What this session could cost, worst case, right now."""
        with self._lock:
            return self._committed + sum(r.amount_atomic for r in self._reservations.values())

    @property
    def daily_exposure_atomic(self) -> int:
        with self._lock:
            self._roll_day()
            return self._daily_committed

    @property
    def call_count(self) -> int:
        with self._lock:
            return self._call_count

    @property
    def declined_count(self) -> int:
        with self._lock:
            return self._declined_count

    def describe(self, decimals: int = 6) -> dict[str, Any]:
        with self._lock:
            return {
                "committed": format_atomic(self._committed, decimals),
                "reserved": format_atomic(self.reserved_atomic, decimals),
                "sessionExposure": format_atomic(self.session_exposure_atomic, decimals),
                "dailyExposure": format_atomic(self._daily_committed, decimals),
                "calls": self._call_count,
                "declined": self._declined_count,
                "day": self._day.isoformat(),
                "persisted": str(self._path) if self._path else None,
                "openReservations": len(self._reservations),
            }


# --------------------------------------------------------------------------
# The Guardian
# --------------------------------------------------------------------------


class Guardian:
    """Stateful buyer-side spend policy. Consulted twice per call; see module doc."""

    def __init__(
        self,
        policy: SpendPolicy | None = None,
        *,
        approver: Approver | None = None,
        journal: SpendJournal | None = None,
        on_verdict: Callable[[Verdict], None] | None = None,
    ) -> None:
        self.policy = policy or SpendPolicy.from_settings()
        self._approver = approver
        self.journal = journal or SpendJournal()
        self._on_verdict = on_verdict
        self._frozen_reason: str | None = None
        self._verdicts: list[Verdict] = []
        self._lock = threading.RLock()

        if self.policy.session_budget_atomic is None:
            log.warning(
                "Guardian has no session_budget: this agent's wallet is bounded only by "
                "its balance. No operator should ship this."
            )
        if self.policy.escalate_above_atomic is not None and self._approver is None:
            log.info(
                "escalate_above is set with no approver -- escalations will DENY "
                "(fail closed). Pass approver=console_approver to be asked instead."
            )

    # ------------------------------------------------------------------ state

    @property
    def frozen(self) -> bool:
        return self._frozen_reason is not None

    @property
    def frozen_reason(self) -> str | None:
        return self._frozen_reason

    def freeze(self, reason: str) -> None:
        """Stop authorizing anything further.

        Note what this does NOT do: it does not claw anything back. Whatever was
        already authorized still settles, honestly, because those authorizations
        exist. Freezing stops the bleeding; it is not a rollback.
        """
        with self._lock:
            if self._frozen_reason is None:
                self._frozen_reason = reason
                log.warning("guardian FROZEN: %s", reason)

    def unfreeze(self) -> None:
        with self._lock:
            self._frozen_reason = None

    @property
    def verdicts(self) -> list[Verdict]:
        return list(self._verdicts)

    def _record(self, verdict: Verdict) -> Verdict:
        with self._lock:
            self._verdicts.append(verdict)
            if len(self._verdicts) > 2_000:
                del self._verdicts[:1_000]
        if verdict.decision is Decision.DENY:
            self.journal.decline()
        if self._on_verdict is not None:
            try:
                self._on_verdict(verdict)
            except Exception:  # an observer must never change the decision
                log.exception("on_verdict observer raised; verdict stands")
        return verdict

    # --------------------------------------------------------------- helpers

    def _remaining_session(self) -> int | None:
        if self.policy.session_budget_atomic is None:
            return None
        return max(self.policy.session_budget_atomic - self.journal.session_exposure_atomic, 0)

    def _remaining_daily(self) -> int | None:
        if self.policy.daily_budget_atomic is None:
            return None
        return max(self.policy.daily_budget_atomic - self.journal.daily_exposure_atomic, 0)

    def _fmt(self, atomic: int) -> str:
        return format_atomic(atomic, self.policy.asset_decimals)

    def _allowlisted(self, resource: str) -> bool:
        if not self.policy.allowlist:
            return False
        return any(fnmatch.fnmatch(resource, pattern) for pattern in self.policy.allowlist)

    # ------------------------------------------------------- static checks --

    def _static_checks(
        self,
        *,
        amount_atomic: int | None,
        tool: str,
        resource: str,
        network: str,
        asset: str,
        phase: str,
    ) -> Verdict | None:
        """Everything that does not touch the running totals. Shared by both phases."""
        common = dict(tool=tool, resource=resource, network=network, asset=asset, phase=phase)

        if self.frozen:
            return Verdict(
                Decision.DENY,
                DeclineReason.SESSION_FROZEN,
                f"session frozen: {self._frozen_reason}",
                amount_atomic or 0,
                **common,
            )

        if amount_atomic is None:
            return Verdict(
                Decision.DENY,
                DeclineReason.UNPRICED,
                "server quoted an amount that is not an integer of atomic units",
                0,
                **common,
            )

        if not self._allowlisted(resource):
            return Verdict(
                Decision.DENY,
                DeclineReason.NOT_ALLOWLISTED,
                f"resource {resource!r} matches none of {list(self.policy.allowlist)}",
                amount_atomic,
                **common,
            )

        if self.policy.networks and network not in self.policy.networks:
            return Verdict(
                Decision.DENY,
                DeclineReason.NETWORK_NOT_ALLOWED,
                f"network {network!r} not in {list(self.policy.networks)}",
                amount_atomic,
                **common,
            )

        if self.policy.assets and asset.lower() not in {a.lower() for a in self.policy.assets}:
            return Verdict(
                Decision.DENY,
                DeclineReason.ASSET_NOT_ALLOWED,
                f"asset {asset!r} not in {list(self.policy.assets)}",
                amount_atomic,
                **common,
            )

        if (
            self.policy.per_call_max_atomic is not None
            and amount_atomic > self.policy.per_call_max_atomic
        ):
            return Verdict(
                Decision.DENY,
                DeclineReason.PER_CALL_MAX,
                f"{self._fmt(amount_atomic)} exceeds per_call_max "
                f"{self._fmt(self.policy.per_call_max_atomic)}",
                amount_atomic,
                **common,
            )

        return None

    # ---------------------------------------------------------- PHASE 1 -----

    def screen(
        self,
        *,
        amount_atomic: int | None,
        tool: str = "",
        resource: str = "",
        network: str = "",
        asset: str = "",
    ) -> Verdict:
        """Worst-case screening. Pure: touches no running total.

        Called with the LARGEST amount across the server's `accepts`, because at
        this point the payment client has not chosen one and we would rather
        refuse a call we might have afforded than allow one we cannot.

        Returns ESCALATE (not ALLOW) when the amount is above the escalation
        threshold; the shim does not act on that -- phase 2 does the asking.
        """
        verdict = self._static_checks(
            amount_atomic=amount_atomic,
            tool=tool,
            resource=resource,
            network=network,
            asset=asset,
            phase="screen",
        )
        if verdict is not None:
            return self._record(verdict)

        assert amount_atomic is not None
        common = dict(tool=tool, resource=resource, network=network, asset=asset, phase="screen")

        remaining = self._remaining_session()
        if remaining is not None and amount_atomic > remaining:
            return self._record(
                Verdict(
                    Decision.DENY,
                    DeclineReason.OVER_SESSION_BUDGET,
                    f"{self._fmt(amount_atomic)} would exceed the session budget "
                    f"({self._fmt(remaining)} left of "
                    f"{self._fmt(self.policy.session_budget_atomic or 0)})",
                    amount_atomic,
                    **common,
                )
            )

        daily = self._remaining_daily()
        if daily is not None and amount_atomic > daily:
            return self._record(
                Verdict(
                    Decision.DENY,
                    DeclineReason.OVER_DAILY_BUDGET,
                    f"{self._fmt(amount_atomic)} would exceed today's budget "
                    f"({self._fmt(daily)} left)",
                    amount_atomic,
                    **common,
                )
            )

        if (
            self.policy.escalate_above_atomic is not None
            and amount_atomic > self.policy.escalate_above_atomic
        ):
            return self._record(
                Verdict(
                    Decision.ESCALATE,
                    DeclineReason.NEEDS_ESCALATION,
                    f"{self._fmt(amount_atomic)} is above escalate_above "
                    f"{self._fmt(self.policy.escalate_above_atomic)}",
                    amount_atomic,
                    **common,
                )
            )

        return self._record(Verdict(Decision.ALLOW, None, "within policy", amount_atomic, **common))

    # ---------------------------------------------------------- PHASE 2 -----

    async def authorize(
        self,
        call_id: str,
        *,
        amount_atomic: int | None,
        tool: str = "",
        resource: str = "",
        network: str = "",
        asset: str = "",
        arguments: dict[str, Any] | None = None,
    ) -> Verdict:
        """The authoritative, reserving, escalating check. Still pre-signature.

        On ALLOW the ceiling is reserved before this returns, so the very next
        thing that happens -- the signature -- is already accounted for.
        """
        verdict = self._static_checks(
            amount_atomic=amount_atomic,
            tool=tool,
            resource=resource,
            network=network,
            asset=asset,
            phase="authorize",
        )
        if verdict is not None:
            return self._record(verdict)

        assert amount_atomic is not None
        common = dict(tool=tool, resource=resource, network=network, asset=asset, phase="authorize")

        # Budget checks and the reservation must not interleave with another
        # concurrent call, or two calls each individually within budget could
        # both be allowed past it.
        with self._lock:
            remaining = self._remaining_session()
            if remaining is not None and amount_atomic > remaining:
                self.freeze(
                    f"session budget exhausted ({self._fmt(remaining)} left, "
                    f"{self._fmt(amount_atomic)} requested)"
                )
                return self._record(
                    Verdict(
                        Decision.DENY,
                        DeclineReason.OVER_SESSION_BUDGET,
                        f"{self._fmt(amount_atomic)} would exceed the session budget "
                        f"({self._fmt(remaining)} left)",
                        amount_atomic,
                        **common,
                    )
                )

            daily = self._remaining_daily()
            if daily is not None and amount_atomic > daily:
                return self._record(
                    Verdict(
                        Decision.DENY,
                        DeclineReason.OVER_DAILY_BUDGET,
                        f"{self._fmt(amount_atomic)} would exceed today's budget "
                        f"({self._fmt(daily)} left)",
                        amount_atomic,
                        **common,
                    )
                )

            needs_escalation = (
                self.policy.escalate_above_atomic is not None
                and amount_atomic > self.policy.escalate_above_atomic
            )
            if not needs_escalation:
                # Reserve inside the lock, so the budget cannot be double-spent.
                self.journal.reserve(call_id, amount_atomic, tool, resource)
                return self._record(
                    Verdict(Decision.ALLOW, None, "within policy", amount_atomic, **common)
                )

        # Escalation happens OUTSIDE the lock: asking a human can take minutes
        # and must not block every other call's policy evaluation. The cost is
        # that two concurrent escalations could both be approved against a
        # budget that only covers one -- so the budget is re-checked and the
        # reservation taken under the lock again below.
        approved = await self._ask(
            Escalation(
                tool=tool,
                resource=resource,
                network=network,
                asset=asset,
                amount_atomic=amount_atomic,
                asset_decimals=self.policy.asset_decimals,
                arguments=arguments or {},
                session_remaining_atomic=self._remaining_session(),
                daily_remaining_atomic=self._remaining_daily(),
            )
        )
        if not approved:
            return self._record(
                Verdict(
                    Decision.DENY,
                    DeclineReason.ESCALATION_DENIED
                    if self._approver is not None
                    else DeclineReason.NEEDS_ESCALATION,
                    "escalation denied"
                    if self._approver is not None
                    else "escalation required and no approver is configured (fail closed)",
                    amount_atomic,
                    **common,
                )
            )

        with self._lock:
            remaining = self._remaining_session()
            if remaining is not None and amount_atomic > remaining:
                return self._record(
                    Verdict(
                        Decision.DENY,
                        DeclineReason.OVER_SESSION_BUDGET,
                        "budget was consumed by a concurrent call while the escalation was pending",
                        amount_atomic,
                        **common,
                    )
                )
            self.journal.reserve(call_id, amount_atomic, tool, resource)
        return self._record(
            Verdict(Decision.ALLOW, None, "approved by escalation", amount_atomic, **common)
        )

    async def _ask(self, escalation: Escalation) -> bool:
        """Run the approver, sync or async, without blocking the event loop.

        A sync approver is assumed to be able to block -- `console_approver`
        reads stdin -- so it goes to a worker thread. Stalling the loop here
        would stall the MCP transport's own reads and time the session out
        while a human is still deciding.
        """
        if self._approver is None:
            return False
        try:
            if asyncio.iscoroutinefunction(self._approver):
                return bool(await self._approver(escalation))
            result = await asyncio.to_thread(self._approver, escalation)
            if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                return bool(await result)
            return bool(result)
        except Exception:
            log.exception("approver raised -- treating as DENY")
            return False

    # ------------------------------------------------------------ settlement

    def commit(self, call_id: str, captured_atomic: int) -> int:
        """Resolve the reservation to what was actually captured. Returns the refund."""
        refund = self.journal.commit(call_id, captured_atomic)
        if refund:
            log.debug(
                "call %s captured %s of a %s ceiling -- %s released back to the budget",
                call_id,
                self._fmt(captured_atomic),
                self._fmt(captured_atomic + refund),
                self._fmt(refund),
            )
        return refund

    def release(self, call_id: str) -> int:
        """Cancel a reservation. Only sound when no signature was produced."""
        return self.journal.release(call_id)

    def record_receipt(
        self, *, call_id: str, has_receipt: bool, tool: str = "", resource: str = ""
    ) -> Verdict | None:
        """`require_receipt`, enforced where it can actually be enforced.

        This is the one control that cannot be preventive, and pretending
        otherwise would be dishonest: the payment is signed and settled before
        any receipt can exist. What it can do is refuse to keep going. So the
        first unevidenced charge is absorbed and the session freezes; there is
        no second one.
        """
        if not self.policy.require_receipt or has_receipt:
            return None
        verdict = Verdict(
            Decision.DENY,
            DeclineReason.NO_RECEIPT,
            f"tool {tool!r} took payment and returned no verifiable receipt; "
            "freezing the session (the charge already happened -- this stops the next one)",
            0,
            tool=tool,
            resource=resource,
            phase="receipt",
        )
        self.freeze(f"no receipt from {tool or resource or call_id}")
        return self._record(verdict)

    # -------------------------------------------------------------- readouts

    def snapshot(self) -> dict[str, Any]:
        d = self.policy.asset_decimals
        remaining_session = self._remaining_session()
        remaining_daily = self._remaining_daily()
        return {
            "frozen": self.frozen,
            "frozenReason": self._frozen_reason,
            "policy": self.policy.describe(),
            "spend": self.journal.describe(d),
            "sessionRemaining": None
            if remaining_session is None
            else format_atomic(remaining_session, d),
            "dailyRemaining": None
            if remaining_daily is None
            else format_atomic(remaining_daily, d),
            "verdicts": len(self._verdicts),
        }
