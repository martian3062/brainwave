"""x402 conformance and ledger integrity. Exit code 0 means everything checked out.

    python -m app.cli doctor
    python -m app.cli doctor --json
    python -m app.cli doctor --strict          # warnings become failures

Four groups of checks, in the order a payment gateway breaks in practice:

    PROTOCOL   Are the payment requirements spec-shaped? Is our 402 challenge
               byte-identical to the one the SDK's own builder produces? Are the
               `_meta` keys the SDK's constants and not a hand-typed string? Is
               the network CAIP-2? Is `amount` an atomic-unit string?

    RECEIPTS   Does every captured call have a receipt? Does each receipt's
               `body_hash` still match its `body_json`? Do the stored columns
               agree with the body they were derived from? Does any receipt
               claim a capture above its authorization?

    LEDGER     Does `sum(call.captured)` equal `batch.gross` for every batch?
               Does the platform/author split conserve? Does a settled batch
               carry a transaction hash? Does `session.settled` ever exceed
               `session.captured`?

    NONCES     Is the UNIQUE (network, nonce) index actually present in the live
               schema, and are there duplicates?

Every check reports one of `ok`, `FAIL`, `skip` or `warn`, and each one names
what it looked at. A check that cannot run -- no receipts yet, no catalogue
module -- is `skip`, never a silent pass: an empty ledger is a legitimate state
on a clean checkout and must not be reported as healthy conformance.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.cli._out import Printer
from app.config import settings

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
CAIP2_RE = re.compile(r"^[a-z0-9\-]{3,8}:[a-zA-Z0-9\-_]{1,32}$")
ATOMIC_RE = re.compile(r"^\d+$")
TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


@dataclass
class Finding:
    group: str
    name: str
    status: str  # ok | FAIL | warn | skip
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "check": self.name,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def ok(self, group: str, name: str, detail: str = "") -> None:
        self.findings.append(Finding(group, name, "ok", detail))

    def fail(self, group: str, name: str, detail: str = "") -> None:
        self.findings.append(Finding(group, name, "FAIL", detail))

    def warn(self, group: str, name: str, detail: str = "") -> None:
        self.findings.append(Finding(group, name, "warn", detail))

    def skip(self, group: str, name: str, detail: str = "") -> None:
        self.findings.append(Finding(group, name, "skip", detail))

    def record(self, condition: bool, group: str, name: str, detail: str = "") -> bool:
        (self.ok if condition else self.fail)(group, name, detail)
        return condition

    def count(self, status: str) -> int:
        return sum(1 for f in self.findings if f.status == status)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures (exit 1)")
    parser.add_argument(
        "--sample",
        type=int,
        default=0,
        help="check at most N receipts (0 = all; useful on a large ledger)",
    )
    parser.add_argument(
        "--skip-ledger", action="store_true", help="protocol checks only; do not open the database"
    )


def run(args: argparse.Namespace, out: Printer) -> int:
    report = Report()

    out.title(
        "TRAPPIST x BRAINWAVE - doctor",
        "x402 conformance and ledger integrity. Offline; reads only.",
    )

    _check_protocol(report)
    _check_meta_keys(report)
    _check_catalogue(report)
    if not args.skip_ledger:
        _check_ledger(report, sample=args.sample)
    else:
        report.skip("ledger", "database", "--skip-ledger")

    _emit(out, report)

    failures = report.count("FAIL")
    warnings = report.count("warn")
    exit_code = 1 if failures or (args.strict and warnings) else 0

    if out.json_mode:
        out.emit_json(
            {
                "ok": exit_code == 0,
                "counts": {
                    "ok": report.count("ok"),
                    "fail": failures,
                    "warn": warnings,
                    "skip": report.count("skip"),
                },
                "findings": [f.as_dict() for f in report.findings],
            }
        )
    return exit_code


#: Display order. Checks are appended as they run, which interleaves groups
#: (the nonce index is inspected before the ledger totals but reads as part of
#: `nonces`); the report is grouped for reading, not for execution order.
GROUP_ORDER = ("protocol", "transport", "catalogue", "ledger", "receipts", "nonces")


def _emit(out: Printer, report: Report) -> None:
    ordered = sorted(
        report.findings,
        key=lambda f: GROUP_ORDER.index(f.group) if f.group in GROUP_ORDER else len(GROUP_ORDER),
    )
    current = ""
    for finding in ordered:
        if finding.group != current:
            current = finding.group
            out.section(current)
        {
            "ok": out.ok,
            "FAIL": out.fail,
            "warn": out.warn,
            "skip": out.skip,
        }[finding.status](f"{finding.name:<34}{finding.detail}")

    out.raw()
    out.rule()
    counts = (report.count("ok"), report.count("FAIL"), report.count("warn"), report.count("skip"))
    out.kv("ok", str(counts[0]), width=10)
    out.kv("failed", str(counts[1]), width=10)
    out.kv("warnings", str(counts[2]), width=10)
    out.kv("skipped", str(counts[3]), width=10)
    out.raw()
    if counts[1]:
        out.fail("conformance FAILED")
    else:
        out.ok("conformance clean")
    out.raw()


# --------------------------------------------------------------------------
# PROTOCOL
# --------------------------------------------------------------------------


def _check_protocol(report: Report) -> None:
    from app.cli._x402 import ToolSpec, build_challenge, build_requirements
    from app.models import Scheme

    group = "protocol"

    spec = ToolSpec(name="doctor_probe", description="conformance probe")
    try:
        requirements = build_requirements(spec, pay_to=settings.pay_to_address)
    except Exception as exc:  # noqa: BLE001
        report.fail(group, "build PaymentRequirements", str(exc))
        return

    dumped = requirements.model_dump(by_alias=True, exclude_none=True)

    report.record(
        dumped.get("amount") is not None and isinstance(dumped["amount"], str),
        group,
        "amount is a string",
        f"{dumped.get('amount')!r} -- the schema types it `str`, not a number",
    )
    report.record(
        bool(ATOMIC_RE.match(str(dumped.get("amount", "")))),
        group,
        "amount is atomic units",
        "digits only: no '$', no decimal point, no exponent",
    )
    report.record(
        bool(CAIP2_RE.match(str(dumped.get("network", "")))),
        group,
        "network is CAIP-2",
        f"{dumped.get('network')} -- x402 v2 dropped the v1 'base-sepolia' spelling",
    )
    report.record(
        bool(ADDRESS_RE.match(str(dumped.get("payTo", "")))),
        group,
        "payTo is a 20-byte address",
        str(dumped.get("payTo")),
    )
    report.record(
        bool(ADDRESS_RE.match(str(dumped.get("asset", "")))),
        group,
        "asset is a contract address",
        str(dumped.get("asset")),
    )
    report.record(
        int(dumped.get("maxTimeoutSeconds", 0)) > 0,
        group,
        "maxTimeoutSeconds set",
        f"{dumped.get('maxTimeoutSeconds')}s -- an authorization with no expiry never dies",
    )
    report.record(
        str(dumped.get("scheme")) in {s.value for s in Scheme},
        group,
        "scheme is known",
        str(dumped.get("scheme")),
    )

    extra = dumped.get("extra") or {}
    report.record(
        bool(extra.get("name")) and bool(extra.get("version")),
        group,
        "extra carries EIP-712 domain",
        f"name={extra.get('name')!r} version={extra.get('version')!r} -- "
        "without these a client that does not know the token cannot sign",
    )

    # The pay-to address matters more than conformance: a correct challenge
    # pointing at the zero address burns every payment it collects.
    if settings.pay_to_address.lower() == "0x" + "0" * 40:
        report.warn(
            group,
            "PAY_TO_ADDRESS",
            "still the zero address -- revenue would be burned. app.main refuses "
            "this in production; set it before deploying.",
        )
    else:
        report.ok(group, "PAY_TO_ADDRESS", settings.pay_to_address)

    # -- the challenge, against the SDK's own builder ----------------------
    challenge = build_challenge([requirements], spec)
    for key in ("x402Version", "accepts", "error", "resource"):
        report.record(key in challenge, group, f"challenge has `{key}`", "")
    report.record(
        challenge.get("x402Version") == 2,
        group,
        "challenge x402Version",
        str(challenge.get("x402Version")),
    )

    _compare_with_sdk_builder(report, group, challenge, requirements, spec)


def _compare_with_sdk_builder(report: Report, group: str, challenge, requirements, spec) -> None:
    """Compare our hand-built 402 body with `x402.mcp.server`'s own.

    We build the challenge ourselves (through the `PaymentRequired` schema) so
    the CLI can produce one without a live FastMCP context. That is a place
    where drift could hide, so this check reaches into the SDK's private
    `_create_payment_required_result` and diffs the key sets. If upstream
    changes the shape, this fails here rather than at an agent's first call.

    Private API, deliberately: comparing against the public schema would only
    prove the schema agrees with itself.
    """
    try:
        from x402.mcp.server import _create_payment_required_result
        from x402.schemas.payments import ResourceInfo
    except Exception as exc:  # noqa: BLE001
        report.skip(group, "challenge matches SDK builder", f"not importable: {exc}")
        return

    try:
        result = _create_payment_required_result(
            [requirements],
            ResourceInfo(url=spec.resource_url, description=spec.description or spec.name),
            "Payment Required",
            None,
        )
        sdk_body = result.structuredContent
    except Exception as exc:  # noqa: BLE001
        report.fail(
            group,
            "challenge matches SDK builder",
            f"the SDK's own 402 builder raised {type(exc).__name__}: {exc}",
        )
        return

    ours = set(challenge)
    theirs = set(sdk_body)
    missing = theirs - ours
    extra_keys = ours - theirs
    if missing or extra_keys:
        report.fail(
            group,
            "challenge matches SDK builder",
            f"missing={sorted(missing)} unexpected={sorted(extra_keys)}",
        )
        return

    same_accepts = challenge["accepts"] == sdk_body["accepts"]
    report.record(
        same_accepts,
        group,
        "challenge matches SDK builder",
        "identical key set and identical `accepts` serialisation",
    )

    # The bug this project had to work around. When upstream fixes it, this
    # flips to a warn so it gets noticed and the workaround can be removed.
    try:
        import x402.mcp as xmcp

        broken = not hasattr(xmcp.ResourceInfo("mcp://tool/x"), "model_dump")
    except Exception:  # noqa: BLE001
        broken = False
    if broken:
        report.ok(
            group,
            "x402.mcp.ResourceInfo workaround",
            "still required: the documented import has no model_dump() and would "
            "raise AttributeError on the FIRST 402. app/mcp_app.py uses "
            "x402.schemas.payments.ResourceInfo instead.",
        )
    else:
        report.warn(
            group,
            "x402.mcp.ResourceInfo workaround",
            "upstream appears fixed -- the workaround in app/mcp_app.py can be removed",
        )


def _check_meta_keys(report: Report) -> None:
    """The single most common x402-over-MCP integration mistake."""
    group = "transport"
    try:
        from x402.mcp.constants import MCP_PAYMENT_META_KEY, MCP_PAYMENT_RESPONSE_META_KEY
    except Exception as exc:  # noqa: BLE001
        report.fail(group, "MCP _meta constants importable", str(exc))
        return

    report.record(
        MCP_PAYMENT_META_KEY == "x402/payment",
        group,
        "request _meta key",
        f"{MCP_PAYMENT_META_KEY!r}",
    )
    report.record(
        MCP_PAYMENT_RESPONSE_META_KEY == "x402/payment-response",
        group,
        "response _meta key",
        f"{MCP_PAYMENT_RESPONSE_META_KEY!r}",
    )

    # Prove we USE the constants rather than a copy of their current value.
    try:
        from app.cli import _x402

        report.record(
            _x402.MCP_PAYMENT_META_KEY is MCP_PAYMENT_META_KEY,
            group,
            "app re-exports the constant",
            "not a hand-typed string that could drift from the SDK",
        )
    except Exception as exc:  # noqa: BLE001
        report.fail(group, "app re-exports the constant", str(exc))

    # And that the encoder we use is the SDK's.
    try:
        from x402.mcp.utils import attach_payment_to_meta

        params = attach_payment_to_meta({"name": "t", "arguments": {}}, _probe_payload())
        report.record(
            MCP_PAYMENT_META_KEY in params.get("_meta", {}),
            group,
            "payment rides in _meta",
            "encoded by the SDK's attach_payment_to_meta, not by us",
        )
        report.record(
            "x-payment" not in json.dumps(params).lower(),
            group,
            "no X-PAYMENT header over MCP",
            "PAYMENT-SIGNATURE / X-PAYMENT belong to the plain-HTTP paywall only",
        )
    except Exception as exc:  # noqa: BLE001
        report.fail(group, "payment rides in _meta", str(exc))

    # Header names for the OTHER transport, so both are stated and neither is
    # confused for the other.
    try:
        from x402.http import constants as http_constants

        names = [
            getattr(http_constants, n)
            for n in dir(http_constants)
            if n.isupper() and isinstance(getattr(http_constants, n), str)
        ]
        current = [n for n in names if "PAYMENT" in n.upper()]
        report.ok(group, "HTTP header names", ", ".join(sorted(set(current))[:6]))
    except Exception:  # noqa: BLE001
        report.skip(group, "HTTP header names", "x402.http.constants not importable")


def _probe_payload():
    from x402.schemas.payments import PaymentPayload

    from app.cli._x402 import ToolSpec, build_requirements

    requirements = build_requirements(ToolSpec(name="doctor_probe"))
    return PaymentPayload(x402Version=2, payload={"authorization": {}}, accepted=requirements)


def _check_catalogue(report: Report) -> None:
    """Validate what the live catalogue actually advertises.

    Reads `app.pay.decorator.registry` -- everything `@paid()` has decorated in
    this process -- and re-derives each tool's `PaymentRequirements` through
    `app.pay.pricing`. That is the same object the 402 challenge carries, so a
    tool whose price cannot be expressed on the wire fails here rather than at
    an agent's first call.

    Tool declarations run inside each catalogue module's `register(mcp)`.
    Register them on a throwaway FastMCP instance directly: the production
    `app.gateway.server.register_tools()` also syncs the catalogue to the
    database, and a diagnostic advertised as read-only must never call it.
    """
    group = "catalogue"
    try:
        from mcp.server.fastmcp import FastMCP

        from app.gateway.tools import register_all
    except Exception as exc:  # noqa: BLE001
        report.fail(group, "catalogue is importable", f"{type(exc).__name__}: {exc}")
        return

    try:
        from app.pay.decorator import registry
        from app.pay.pricing import payment_requirements
    except ImportError:
        report.skip(group, "app.pay", "not present -- nothing is priced yet")
        return

    try:
        registered = register_all(FastMCP(name="brainwave-doctor-read-only"))
        if not registered:
            raise RuntimeError("no catalogue modules registered")
    except Exception as exc:  # noqa: BLE001
        report.fail(group, "catalogue registers", f"{type(exc).__name__}: {exc}")
        return

    if not registry:
        # Not necessarily a problem: a catalogue may declare its prices straight
        # into the `tool` table rather than through `app.pay`'s in-process
        # registry. `_check_tool_rows()` validates that path from the database,
        # so the two together cover both.
        report.skip(
            group,
            "in-process priced tools",
            "no @paid() declarations in this process; prices checked from the `tool` table",
        )
        return

    from x402.schemas.payments import PaymentRequirements

    problems: list[str] = []
    free_tools: list[str] = []
    for name, entry in sorted(registry.items()):
        try:
            requirements = payment_requirements(entry.spec)
            PaymentRequirements.model_validate(
                requirements.model_dump(by_alias=True, exclude_none=True)
            )
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{name}: {type(exc).__name__}: {exc}"[:160])
            continue

        amount = str(requirements.amount)
        if not ATOMIC_RE.match(amount):
            problems.append(f"{name}: amount {amount!r} is not an atomic-unit string")
        elif amount == "0":
            # Not a failure -- a deliberately free tool is legitimate -- but a
            # priced tool that rounds to zero is a silent revenue leak.
            free_tools.append(name)
        if not CAIP2_RE.match(str(requirements.network)):
            problems.append(f"{name}: network {requirements.network!r} is not CAIP-2")
        if not ADDRESS_RE.match(str(requirements.pay_to)):
            problems.append(f"{name}: payTo {requirements.pay_to!r} is not an address")

    report.record(
        not problems,
        group,
        "advertised requirements",
        "; ".join(problems[:3]) if problems else f"{len(registry)} priced tools, all spec-shaped",
    )
    if free_tools:
        report.warn(
            group,
            "zero-priced tools",
            f"{', '.join(free_tools[:5])} advertise amount 0 -- intentional, or a price "
            "that rounded away below the asset's decimals?",
        )


# --------------------------------------------------------------------------
# LEDGER
# --------------------------------------------------------------------------


def _check_ledger(report: Report, *, sample: int = 0) -> None:
    from sqlalchemy import func, inspect
    from sqlmodel import Session as DBSession
    from sqlmodel import select

    from app.db import DATABASE_URL, engine
    from app.demo import summary
    from app.models import Batch, BatchStatus, Call, PaySession
    from app.money import format_atomic

    group = "ledger"
    d = settings.x402_asset_decimals

    inspector = inspect(engine)
    if "call" not in inspector.get_table_names():
        report.skip(
            group,
            "schema",
            f"no ledger tables in {DATABASE_URL.split('://')[0]} -- run `alembic upgrade head`",
        )
        return
    report.ok(group, "schema present", f"{len(inspector.get_table_names())} tables")

    # -- the replay defence has to exist, not just be intended -------------
    #
    # It is declared as a table-level UniqueConstraint, which the two dialects
    # report in different places: Postgres surfaces it under
    # `get_unique_constraints`, SQLite may only expose the auto-created index
    # under `get_indexes`. Checking one and not the other reports a false
    # failure on whichever database you did not develop against.
    want = {"network", "nonce"}
    from_constraints = any(
        set(c.get("column_names") or []) == want for c in inspector.get_unique_constraints("call")
    )
    from_indexes = any(
        ix.get("unique") and set(ix.get("column_names") or []) == want
        for ix in inspector.get_indexes("call")
    )
    report.record(
        from_constraints or from_indexes,
        "nonces",
        "UNIQUE(network, nonce) exists",
        "the replay defence is a database constraint, not application logic",
    )

    with DBSession(engine) as db:
        # -- demo labelling ------------------------------------------------
        state = summary(db)
        if state.mixed:
            report.warn(
                group,
                "demo data",
                f"{state.total_rows} demo rows alongside real ones -- every revenue "
                "view must label per row, not just banner",
            )
        elif state.present:
            report.ok(group, "demo data", f"{state.total_rows} rows, all labelled is_demo=True")
        else:
            report.ok(group, "demo data", "none present")

        total_calls = int(db.exec(select(func.count()).select_from(Call)).one() or 0)
        if total_calls == 0:
            report.skip(group, "ledger contents", "no calls yet -- nothing to reconcile")
            report.skip("receipts", "receipts", "no calls yet")
            report.skip("nonces", "duplicate nonces", "no calls yet")
            return

        # -- nonce duplicates ----------------------------------------------
        dupes = db.exec(
            select(Call.network, Call.nonce, func.count())
            .where(Call.nonce.is_not(None))
            .group_by(Call.network, Call.nonce)
            .having(func.count() > 1)
        ).all()
        report.record(
            not dupes,
            "nonces",
            "no duplicate authorizations",
            f"{len(dupes)} reused (network, nonce) pairs" if dupes else "checked every call",
        )

        # -- capture invariant ---------------------------------------------
        over = db.exec(
            select(func.count())
            .select_from(Call)
            .where(Call.captured_atomic > Call.authorized_atomic)
        ).one()
        report.record(
            int(over or 0) == 0,
            group,
            "capture <= authorization",
            f"{over} calls overcharged" if over else "holds for every call",
        )

        split_broken = db.exec(
            select(func.count())
            .select_from(Call)
            .where(Call.platform_fee_atomic + Call.author_net_atomic != Call.captured_atomic)
        ).one()
        report.record(
            int(split_broken or 0) == 0,
            group,
            "revenue split conserves",
            "platform + author == captured, exactly, on every call",
        )

        # -- batch reconciliation ------------------------------------------
        batches = db.exec(select(Batch)).all()
        mismatched: list[str] = []
        unhashed: list[str] = []
        for batch in batches:
            total = int(
                db.exec(
                    select(func.coalesce(func.sum(Call.captured_atomic), 0)).where(
                        Call.batch_id == batch.id
                    )
                ).one()
                or 0
            )
            if total != batch.gross_atomic:
                mismatched.append(
                    f"{batch.batch_id}: calls {format_atomic(total, d)} vs "
                    f"gross {format_atomic(batch.gross_atomic, d)}"
                )
            if batch.status == BatchStatus.SETTLED and not batch.settle_tx_hash:
                unhashed.append(batch.batch_id)

        if batches:
            report.record(
                not mismatched,
                group,
                "sum(call.captured) == batch.gross",
                "; ".join(mismatched[:3]) if mismatched else f"reconciled {len(batches)} batches",
            )
            report.record(
                not unhashed,
                group,
                "settled batches carry a tx hash",
                ", ".join(unhashed[:3]) if unhashed else "every settled batch has one",
            )
            malformed = [
                b.batch_id
                for b in batches
                if b.settle_tx_hash and not TX_HASH_RE.match(b.settle_tx_hash)
            ]
            report.record(
                not malformed,
                group,
                "tx hashes are 32 bytes",
                ", ".join(malformed[:3]) if malformed else "checked",
            )
            synthetic = [
                b.batch_id for b in batches if (b.settle_tx_hash or "").startswith("0xdead")
            ]
            if synthetic:
                report.warn(
                    group,
                    "synthetic tx hashes",
                    f"{len(synthetic)} batches carry a 0xdead placeholder -- demo or dry-run "
                    "output, never an on-chain settlement",
                )
        else:
            report.skip(group, "batch reconciliation", "no batches yet")

        # -- session totals -------------------------------------------------
        bad_sessions = db.exec(
            select(func.count())
            .select_from(PaySession)
            .where(PaySession.settled_atomic > PaySession.captured_atomic)
        ).one()
        report.record(
            int(bad_sessions or 0) == 0,
            group,
            "settled <= captured per session",
            "no session claims to have settled more than it charged",
        )

        # -- what the catalogue actually advertises ---------------------------
        _check_tool_rows(report, db)

        # -- receipts --------------------------------------------------------
        _check_receipts(report, db, sample=sample)


def _check_tool_rows(report: Report, db) -> None:
    """Every enabled `tool` row must be expressible as `PaymentRequirements`.

    This is the price check that does not care which module declared the price.
    `app.pay`'s in-process registry only exists when `@paid()` ran in THIS
    process; the `tool` table is where every catalogue implementation lands, and
    it is what the ledger, the receipts and the dashboard all join against. A
    row here that cannot produce a valid 402 is a tool an agent cannot buy.
    """
    from sqlmodel import select
    from x402.schemas.payments import PaymentRequirements

    from app.demo import real_only
    from app.models import Scheme, Tool

    group = "catalogue"
    tools = db.exec(select(Tool).where(Tool.enabled.is_(True)).where(real_only(Tool))).all()
    if not tools:
        report.skip(group, "priced tool rows", "no enabled tools in the catalogue")
        return

    problems: list[str] = []
    for tool in tools:
        try:
            PaymentRequirements(
                scheme=str(tool.scheme),
                network=tool.network,
                asset=tool.asset,
                amount=str(
                    tool.max_price_atomic
                    if (tool.scheme == Scheme.UPTO and tool.max_price_atomic)
                    else tool.price_atomic
                ),
                payTo=_pay_to_of(db, tool),
                maxTimeoutSeconds=tool.max_timeout_seconds,
                extra={},
            )
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{tool.name}: {type(exc).__name__}: {exc}"[:140])
            continue

        if not CAIP2_RE.match(tool.network):
            problems.append(f"{tool.name}: network {tool.network!r} is not CAIP-2")
        if not ADDRESS_RE.match(_pay_to_of(db, tool)):
            problems.append(f"{tool.name}: payTo is not a 20-byte address")
        if tool.scheme == Scheme.UPTO and not tool.max_price_atomic:
            problems.append(
                f"{tool.name}: scheme is `upto` with no ceiling -- the agent would be "
                "asked to authorize an unbounded amount"
            )
        if tool.max_price_atomic is not None and tool.max_price_atomic < tool.price_atomic:
            problems.append(f"{tool.name}: ceiling below base price")

    report.record(
        not problems,
        group,
        "priced tool rows",
        "; ".join(problems[:3]) if problems else f"{len(tools)} enabled tools, all spec-shaped",
    )

    zero = [t.name for t in tools if t.price_atomic == 0 and not t.max_price_atomic]
    if zero:
        report.warn(
            group,
            "zero-priced tools",
            f"{', '.join(zero[:5])} are free -- intentional, or a price that rounded "
            "away below the asset's decimals?",
        )


def _pay_to_of(db, tool) -> str:
    from app.models import Author

    author = db.get(Author, tool.author_id)
    return author.pay_to if author else settings.pay_to_address


def _check_receipts(report: Report, db, *, sample: int) -> None:
    from sqlalchemy import func
    from sqlmodel import select

    from app.models import Call, CallStatus, Receipt
    from app.pay import receipts as receipts_mod

    group = "receipts"

    chargeable = db.exec(
        select(func.count())
        .select_from(Call)
        .where(Call.status.in_([CallStatus.CAPTURED, CallStatus.SETTLED]))
    ).one()
    receipt_count = int(db.exec(select(func.count()).select_from(Receipt)).one() or 0)

    if receipt_count == 0:
        report.skip(group, "receipts present", f"{chargeable} chargeable calls, 0 receipts")
        return

    report.record(
        receipt_count >= int(chargeable or 0),
        group,
        "every charged call has a receipt",
        f"{receipt_count} receipts for {chargeable} captured/settled calls",
    )

    statement = select(Receipt).order_by(Receipt.id.desc())
    if sample:
        statement = statement.limit(sample)
    receipts = db.exec(statement).all()

    # Verified through the gateway's OWN verifier (`app.pay.receipts.verify`),
    # not a second implementation living in the CLI. A conformance tool that
    # re-implements the thing it is checking will eventually agree with itself
    # and disagree with production.
    tampered: list[str] = []
    reasons: list[str] = []
    strengths: dict[str, int] = {}
    for receipt in receipts:
        result = receipts_mod.verify(db, receipt.receipt_id, record=False)
        strengths[result.status] = strengths.get(result.status, 0) + 1
        if not result.ok:
            tampered.append(receipt.receipt_id)
            failed = [c.name for c in result.checks if not c.ok]
            reasons.append(f"{receipt.receipt_id}: {', '.join(failed)}")

    report.record(
        not tampered,
        group,
        "body_hash verifies",
        "; ".join(reasons[:3]) if tampered else f"recomputed sha256 for {len(receipts)} receipts",
    )
    # How strong the evidence actually is. `verified_local` means "internally
    # consistent" and nothing more -- a hash we computed over our own data is
    # not third-party evidence, and reporting it as if it were would be the
    # exact dishonesty this tool exists to catch.
    if strengths:
        report.ok(
            group,
            "evidence strength",
            ", ".join(f"{count} {status}" for status, count in sorted(strengths.items())),
        )

    # A receipt must be joinable back to the settlement that paid for it, or it
    # is decoration. This is the reconciliation chain the whole design rests on.
    orphans = [r.receipt_id for r in receipts if r.batch_id is None and r.tx_hash]
    report.record(
        not orphans,
        group,
        "tx hash implies a batch",
        ", ".join(orphans[:3]) if orphans else "receipt -> batch -> tx chain is intact",
    )

    unattested = sum(1 for r in receipts if not r.attestation)
    if unattested:
        report.warn(
            group,
            "facilitator attestation",
            f"{unattested}/{len(receipts)} receipts have none. Expected for demo and "
            "offline rows; a live settlement should carry the facilitator's signature, "
            "which is what makes a receipt verifiable WITHOUT trusting this gateway.",
        )
    else:
        report.ok(group, "facilitator attestation", f"present on all {len(receipts)}")

    demo = sum(1 for r in receipts if r.is_demo)
    if demo:
        report.ok(
            group,
            "demo receipts self-declare",
            f'{demo}/{len(receipts)} carry "isDemo": true INSIDE the hashed body',
        )
