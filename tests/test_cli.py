"""Tests for the CLI, the receipt format and the demo-data guarantee.

Driven through `app.cli.invoke()` -- the same entry point a human types -- so a
regression in argument parsing or exit codes is caught here rather than by
somebody running the command during a demo. No subprocesses, no server, no
network, no funds.

`DATABASE_URL` is set at MODULE IMPORT TIME, before anything under `app` is
imported. `app.config.get_settings()` is `lru_cache`d and `app.db` builds the
engine at import, so a fixture that sets it later is too late -- the engine
would already point at `sqlite:///./brainwave.db` and the suite would quietly
write a database file into the repository. `sqlite://` (in-memory, StaticPool --
see `app/db.py::_engine_kwargs`) is the same value `tests/test_spine.py` uses,
so running the two files together shares one engine instead of racing for it.

That sharing is why the demo-labelling test measures the CHANGE in real rows
rather than asserting there are none: `test_spine.py` legitimately writes real
rows into the same database, and a test that only passes when run alone is not
a test.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("PUBLIC_BASE_URL", "http://testserver")


@pytest.fixture(scope="module")
def ledger():
    """Schema created, and no demo rows left over from another module."""
    from sqlmodel import Session as DBSession

    from app.db import create_all, engine
    from app.demo import purge

    create_all()
    with DBSession(engine) as db:
        purge(db)
    yield engine
    with DBSession(engine) as db:
        purge(db)


def _real_counts():
    """Rows this module did not create. Used as a baseline, not an absolute."""
    from sqlalchemy import func
    from sqlmodel import Session as DBSession
    from sqlmodel import select

    from app.db import engine
    from app.demo import LEDGER_MODELS, real_only

    with DBSession(engine) as db:
        return {
            model.__tablename__: int(
                db.exec(select(func.count()).select_from(model).where(real_only(model))).one()
            )
            for model in LEDGER_MODELS
        }


# ---------------------------------------------------------------- wiring ----


def test_every_command_is_importable_and_declares_its_flags():
    from app.cli import COMMANDS, build_parser

    parser = build_parser()
    assert set(COMMANDS) == {"simulate", "doctor", "close_batch", "seed_demo"}
    # --help must not raise for any subcommand.
    for name in COMMANDS:
        with pytest.raises(SystemExit) as exc:
            parser.parse_args([name, "--help"])
        assert exc.value.code == 0


def test_json_flag_works_before_and_after_the_subcommand():
    """argparse normally lets a subparser clobber the parent's value; the
    SUPPRESS defaults in app.cli exist to stop that."""
    from app.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["--json", "doctor"]).json_mode is True
    assert parser.parse_args(["doctor", "--json"]).json_mode is True
    assert parser.parse_args(["doctor"]).json_mode is False


# -------------------------------------------------------------- simulate ----


def test_simulate_runs_on_a_clean_checkout_with_no_database():
    """The headline claim: this works with nothing set up at all."""
    from app.cli import invoke

    code, payload = invoke("simulate", ["--calls", "3", "--no-color"])
    assert code == 0
    assert payload["settledCalls"] == 3
    assert payload["ledgerClaimHolds"] is True
    assert payload["capturedAtomic"] == "6000"  # 3 x $0.002, exact integers


def test_simulate_signature_is_real_and_a_flipped_bit_is_rejected():
    from app.cli import invoke

    good, ok_payload = invoke("simulate", ["--calls", "1"])
    bad, bad_payload = invoke("simulate", ["--calls", "1", "--fail", "bad-signature"])
    assert good == 0 and bad == 0  # walking a failure path is a successful run
    assert ok_payload["settledCalls"] == 1
    assert bad_payload["settledCalls"] == 0
    assert bad_payload["records"][0]["status"] == "failed"


def test_simulate_replay_of_a_spent_nonce_is_rejected():
    from app.cli import invoke

    _, payload = invoke("simulate", ["--calls", "3", "--fail", "replay"])
    statuses = [r["status"] for r in payload["records"]]
    assert statuses[0] == "settled"
    assert "failed" in statuses


def test_simulate_guardian_declines_before_anything_is_signed():
    from app.cli import invoke

    _, payload = invoke("simulate", ["--calls", "4", "--fail", "over-budget"])
    declined = [r for r in payload["records"] if r["status"] == "declined"]
    assert declined, "expected the Guardian to stop the session"
    assert declined[0]["declineReason"] == "over_session_budget"
    # A decline has no authorization artefact -- nothing was signed, so nothing
    # could ever be settled.
    assert declined[0]["capturedAtomic"] == "0"


def test_simulate_refuses_to_capture_more_than_was_authorized():
    from app.cli import invoke

    _, payload = invoke("simulate", ["--calls", "1", "--fail", "over-capture"])
    assert payload["settledCalls"] == 0
    assert payload["records"][0]["declineReason"] == "capture_exceeds_authorization"


def test_simulate_no_payment_terminates_at_the_402():
    from app.cli import invoke

    _, payload = invoke("simulate", ["--calls", "2", "--fail", "no-payment"])
    assert payload["settledCalls"] == 0
    assert payload["records"][0]["status"] == "challenged"


def test_simulate_batch_settlement_opens_a_channel_and_signs_vouchers():
    from app.cli import invoke

    _, payload = invoke("simulate", ["--calls", "5", "--scheme", "batch-settlement"])
    assert payload["channelId"].startswith("0x") and len(payload["channelId"]) == 66
    assert payload["economics"]["schemeCanBatch"] is True
    # The ceiling is cumulative, not a sum of per-call ceilings.
    assert payload["authorizedCeilingAtomic"] == payload["capturedAtomic"]


def test_simulate_upto_captures_less_than_it_authorized():
    from app.cli import invoke

    _, payload = invoke("simulate", ["--calls", "1", "--scheme", "upto"])
    record = payload["records"][0]
    assert int(record["capturedAtomic"]) < int(record["authorizedAtomic"])


def test_simulate_never_claims_a_single_on_chain_event_for_a_batch():
    """Closing is claim + sweep. Reporting one would halve the fee load in our
    own favour, which is exactly the number this project is judged on."""
    from app.cli import invoke

    _, payload = invoke("simulate", ["--calls", "10", "--scheme", "batch-settlement"])
    economics = payload["economics"]
    assert economics["onChainEventsPerBatch"] == 2
    assert int(economics["batchedSettlementCostAtomic"]) == 2 * int(
        economics["settlementCostAtomic"]
    )


def test_simulate_is_deterministic_for_a_fixed_seed():
    from app.cli._x402 import demo_account

    first, key = demo_account("eraya-brainwave-demo-agent")
    second, again = demo_account("eraya-brainwave-demo-agent")
    assert first.address == second.address and key == again
    assert demo_account("other")[0].address != first.address


# --------------------------------------------------------------- receipts ---
#
# The CLI deliberately has NO receipt builder of its own -- `simulate` and
# `seed_demo` both go through `app.pay.receipts`, the same issuer the running
# gateway uses. Two canonicalisations would drift, and a receipt that verifies in
# the simulator but not in production is worse than no simulator. These tests
# pin the properties the CLI depends on.


def _shadow(authorized: int = 2000, captured: int = 2000):
    from app.models import Call, PaySession, Scheme

    call = Call(
        call_id="call_1",
        session_id=0,
        tool_id=0,
        payer="0xA",
        pay_to="0xB",
        network="eip155:84532",
        asset="0xU",
        scheme=Scheme.EXACT,
        authorized_atomic=authorized,
        captured_atomic=captured,
    )
    session = PaySession(session_id="sess_1", payer="0xA", network="eip155:84532", asset="0xU")
    return call, session


def _body(**overrides):
    from app.models import SettlementMode
    from app.pay import receipts as receipts_mod

    call, session = _shadow(overrides.pop("authorized", 2000), overrides.pop("captured", 2000))
    kwargs = dict(
        receipt_id="rcpt_1",
        call=call,
        session=session,
        resource_url="mcp://tool/x",
        settlement=SettlementMode.BATCHED,
        tx_hash=None,
        facilitator="x402.org",
        attestation=None,
        batch_id=None,
    )
    kwargs.update(overrides)
    return receipts_mod.build_body(**kwargs)


def test_receipt_hash_is_key_order_independent():
    from app.pay.receipts import body_digest

    body = _body()
    shuffled = dict(reversed(list(body.items())))
    assert body_digest(body) == body_digest(shuffled)


def test_receipt_money_is_serialised_as_strings():
    """`PaymentRequirements.amount` is a string in the x402 schema, and a JSON
    number above 2**53 does not survive every parser."""
    body = _body(authorized=10**15, captured=10**15)
    assert body["authorizedAtomic"] == str(10**15)
    assert isinstance(body["capturedAtomic"], str)


def test_receipt_shows_both_authorized_and_captured():
    """Under `upto` they differ, and a receipt showing only the charge would hide
    the ceiling the agent actually signed."""
    body = _body(authorized=20_000, captured=9_000)
    assert body["authorizedAtomic"] == "20000"
    assert body["capturedAtomic"] == "9000"


def test_batched_receipt_carries_no_transaction_at_issue_time():
    """Under batching nothing has moved on-chain yet. A placeholder hash here
    would be the most dishonest thing this codebase could do."""
    body = _body()
    assert body["transaction"] is None
    assert body["explorer"] is None
    # ...but the reconciliation keys that lead to one are present.
    assert body["sessionId"] == "sess_1"
    assert body["callId"] == "call_1"


def test_extra_cannot_overwrite_a_protocol_field():
    """`extra` exists so `seed_demo` can put `isDemo` INSIDE the hashed body. It
    must not become a way to rewrite an amount."""
    with pytest.raises(ValueError):
        _body(extra={"capturedAtomic": "1"})


def test_is_demo_travels_inside_the_hashed_body():
    from app.pay.receipts import body_digest

    body = _body(extra={"isDemo": True})
    assert body["isDemo"] is True
    stripped = {k: v for k, v in body.items() if k != "isDemo"}
    # Removing the flag changes the digest, so it cannot be edited off quietly.
    assert body_digest(stripped) != body_digest(body)


# ------------------------------------------------------------- demo data ----


def test_seed_demo_labels_every_row(ledger):
    """The guarantee: the seeder cannot produce a row with `is_demo=False`."""
    from app.cli import invoke

    before = _real_counts()
    code, payload = invoke("seed_demo", ["--reset", "--sessions", "3", "--seed", "1"])
    assert code == 0
    assert payload["seeded"]["calls"] > 0
    assert _real_counts() == before, "the seeder wrote a row it did not label as demo"


def test_mark_demo_refuses_a_model_without_the_column():
    """A future table that forgets `is_demo` must fail loudly, not seed
    unlabelled rows."""
    from app.demo import mark_demo

    class NotALedgerRow:
        pass

    with pytest.raises(TypeError):
        mark_demo(NotALedgerRow())


def test_demo_receipts_declare_themselves_inside_the_hashed_body(ledger):
    from sqlmodel import Session as DBSession
    from sqlmodel import select

    from app.db import engine
    from app.models import Receipt
    from app.pay import receipts as receipts_mod

    with DBSession(engine) as db:
        receipts = db.exec(select(Receipt).limit(20)).all()
        assert receipts, "seed_demo should have produced receipts"
        for receipt in receipts:
            result = receipts_mod.verify(db, receipt.receipt_id, record=False)
            assert result.ok, [c.name for c in result.checks if not c.ok]
            assert json.loads(receipt.body_json)["isDemo"] is True
            assert receipt.is_demo is True


def test_seed_demo_is_deterministic(ledger):
    from app.cli import invoke

    code, first = invoke("seed_demo", ["--reset", "--sessions", "3", "--seed", "5"])
    assert code == 0, first
    counts_a = first["seeded"]["calls"], first["seeded"]["capturedAtomic"]
    code, second = invoke("seed_demo", ["--reset", "--sessions", "3", "--seed", "5"])
    assert code == 0, second
    counts_b = second["seeded"]["calls"], second["seeded"]["capturedAtomic"]
    assert counts_a == counts_b


def test_reset_removes_only_demo_rows(ledger):
    from sqlalchemy import func
    from sqlmodel import Session as DBSession
    from sqlmodel import select

    from app.cli import invoke
    from app.db import engine
    from app.models import Author

    # A real row the reset must not touch.
    with DBSession(engine) as db:
        db.add(Author(slug="real-author", display_name="Real", pay_to="0xREAL"))
        db.commit()

    code, _ = invoke("seed_demo", ["--reset-only"])
    assert code == 0

    with DBSession(engine) as db:
        survivors = db.exec(select(Author)).all()
        assert [a.slug for a in survivors] == ["real-author"]
        assert survivors[0].is_demo is False
        db.delete(survivors[0])
        db.commit()
        assert db.exec(select(func.count()).select_from(Author)).one() == 0


# ----------------------------------------------------------------- doctor ---


def test_doctor_passes_on_a_clean_checkout():
    """Empty is a legitimate state; it must be `skip`, never a silent pass and
    never a failure."""
    from app.cli import invoke

    code, payload = invoke("doctor", ["--skip-ledger"])
    assert code == 0
    assert payload["ok"] is True
    assert payload["counts"]["fail"] == 0


def test_doctor_validates_the_challenge_against_the_sdks_own_builder():
    from app.cli import invoke

    _, payload = invoke("doctor", ["--skip-ledger"])
    check = next(f for f in payload["findings"] if f["check"] == "challenge matches SDK builder")
    assert check["status"] == "ok", check


def test_doctor_pins_the_mcp_meta_keys():
    from app.cli import invoke

    _, payload = invoke("doctor", ["--skip-ledger"])
    findings = {f["check"]: f for f in payload["findings"]}
    assert findings["request _meta key"]["status"] == "ok"
    assert findings["no X-PAYMENT header over MCP"]["status"] == "ok"


def test_doctor_is_strictly_read_only(ledger):
    from app.cli import invoke

    before = _real_counts()
    code, payload = invoke("doctor", [])
    assert code == 0, [f for f in payload["findings"] if f["status"] == "FAIL"]
    assert _real_counts() == before


def test_doctor_reconciles_a_seeded_ledger(ledger):
    from app.cli import invoke

    seeded, seed_payload = invoke("seed_demo", ["--reset", "--sessions", "4", "--seed", "3"])
    assert seeded == 0, seed_payload
    code, payload = invoke("doctor", [])
    failures = [f for f in payload["findings"] if f["status"] == "FAIL"]
    assert code == 0, failures
    checks = {f["check"] for f in payload["findings"]}
    assert "sum(call.captured) == batch.gross" in checks
    assert "body_hash verifies" in checks


def test_doctor_detects_a_tampered_receipt(ledger):
    from sqlmodel import Session as DBSession
    from sqlmodel import select

    from app.cli import invoke
    from app.db import engine
    from app.models import Receipt

    with DBSession(engine) as db:
        receipt = db.exec(select(Receipt)).first()
        assert receipt is not None
        original = receipt.body_json
        receipt.body_json = original.replace('"capturedAtomic":"', '"capturedAtomic":"9')
        db.add(receipt)
        db.commit()

    code, payload = invoke("doctor", [])
    assert code == 1
    hashes = next(f for f in payload["findings"] if f["check"] == "body_hash verifies")
    assert hashes["status"] == "FAIL"

    with DBSession(engine) as db:
        receipt = db.exec(select(Receipt)).first()
        receipt.body_json = original
        db.add(receipt)
        db.commit()


def test_the_replay_defence_is_a_database_constraint(ledger):
    """Not application logic that a new code path could route around."""
    import sqlalchemy.exc
    from sqlmodel import Session as DBSession

    from app.db import engine
    from app.models import Author, Call, PaySession, Scheme, Tool

    with DBSession(engine) as db:
        tag = uuid.uuid4().hex[:8]
        # Real rows, and self-consistent: hanging a real Call off a demo Tool
        # would be a foreign key this test invented, not one the app produces.
        author = Author(slug=f"nonce-probe-{tag}", display_name="probe", pay_to="0xB")
        db.add(author)
        db.commit()
        db.refresh(author)
        tool = Tool(
            author_id=author.id,
            name=f"nonce_probe_{tag}",
            resource_url="mcp://tool/nonce_probe",
            network="eip155:84532",
            asset="0xU",
        )
        db.add(tool)
        s = PaySession(
            session_id=f"sess_nonce_{tag}", payer="0xA", network="eip155:84532", asset="0xU"
        )
        db.add(s)
        db.commit()
        db.refresh(s)
        db.refresh(tool)

        common = dict(
            session_id=s.id,
            tool_id=tool.id,
            payer="0xA",
            pay_to="0xB",
            network="eip155:84532",
            asset="0xU",
            scheme=Scheme.EXACT,
            nonce=f"0xdeadbeef{tag}",
        )
        db.add(Call(call_id=f"call_n1_{tag}", **common))
        db.commit()
        db.add(Call(call_id=f"call_n2_{tag}", **common))
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            db.commit()
        db.rollback()


# ------------------------------------------------------------ close_batch ---


def test_close_batch_is_a_dry_run_by_default(ledger):
    from app.cli import invoke

    assert invoke("seed_demo", ["--reset", "--sessions", "6", "--seed", "9"])[0] == 0
    code, payload = invoke("close_batch", ["--all", "--force-window"])
    assert code == 0
    assert payload["live"] is False
    assert payload["settled"] == []


def test_close_batch_dry_run_writes_nothing(ledger):
    from sqlalchemy import func
    from sqlmodel import Session as DBSession
    from sqlmodel import select

    from app.cli import invoke
    from app.db import engine
    from app.models import Batch, Call, CallStatus

    def snapshot():
        with DBSession(engine) as db:
            return (
                db.exec(select(func.count()).select_from(Batch)).one(),
                db.exec(
                    select(func.count()).select_from(Call).where(Call.status == CallStatus.CAPTURED)
                ).one(),
            )

    before = snapshot()
    invoke("close_batch", ["--all", "--force-window"])
    assert snapshot() == before


@pytest.mark.parametrize(
    "argv",
    [
        ["--live"],
        ["--live", "--yes"],
        ["--live", "--yes", "--all"],
        ["--live", "--yes", "--all", "--confirm-network", "eip155:8453"],
    ],
)
def test_close_batch_refuses_every_incomplete_live_invocation(ledger, argv):
    """Exit 2 is the refusal code, and it happens before anything is read."""
    from app.cli import invoke

    code, _ = invoke("close_batch", argv)
    assert code == 2


def test_close_batch_live_stops_without_a_channel_store(ledger, monkeypatch):
    """Guards pass, but there are no signed vouchers, so it must refuse rather
    than fabricate a payload. Nothing is sent: the store is resolved before the
    facilitator client is even constructed."""
    from app.cli import invoke
    from app.config import settings

    monkeypatch.setattr(settings, "pay_to_address", "0x000000000000000000000000000000000000dEaD")
    assert invoke("seed_demo", ["--reset", "--sessions", "6", "--seed", "9"])[0] == 0
    code, payload = invoke(
        "close_batch",
        ["--live", "--yes", "--all", "--force-window", "--confirm-network", settings.x402_network],
    )
    assert code == 1
    assert payload["settled"] == []


def test_close_batch_respects_the_dust_floor(ledger, monkeypatch):
    from app.cli import invoke
    from app.config import settings

    monkeypatch.setattr(settings, "batch_min_gross_atomic", 10**12)
    monkeypatch.setattr(settings, "batch_window_seconds", 0)
    _, payload = invoke("close_batch", ["--all"])
    reasons = [p["skipReason"] or "" for p in payload["plans"]]
    assert any("dust floor" in r for r in reasons), reasons


# ------------------------------------------------------------- deployment ---


def test_render_yaml_only_sets_variables_config_actually_reads():
    """A blueprint that sets a variable nothing reads is a lie about how the app
    is configured, and the reverse -- a required variable the blueprint omits --
    is a deploy that boots wrong."""
    import re
    from pathlib import Path

    from app.config import Settings

    render = Path(__file__).resolve().parents[1] / "render.yaml"
    keys = set(re.findall(r"- key: ([A-Z0-9_]+)", render.read_text(encoding="utf-8")))
    unknown = {k for k in keys if k.lower() not in Settings.model_fields and k != "PYTHON_VERSION"}
    assert unknown == set(), f"render.yaml sets variables config.py never reads: {unknown}"

    # The five the production preflight refuses to start without.
    for required in (
        "APP_ENV",
        "DATABASE_URL",
        "STORAGE_SECRET",
        "ADMIN_PASSWORD",
        "PAY_TO_ADDRESS",
    ):
        assert required in keys, f"render.yaml must set {required}"


def test_runtime_txt_and_render_agree_on_the_python_version():
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    runtime = (root / "runtime.txt").read_text(encoding="utf-8").strip()
    render = (root / "render.yaml").read_text(encoding="utf-8")
    pinned = re.search(r"PYTHON_VERSION\s*\n\s*value:\s*([\d.]+)", render)
    assert pinned, "render.yaml must pin PYTHON_VERSION"
    assert runtime == f"python-{pinned.group(1)}", (runtime, pinned.group(1))


def test_build_sh_migrates_and_never_starts_a_server():
    from pathlib import Path

    build = (Path(__file__).resolve().parents[1] / "build.sh").read_text(encoding="utf-8")
    assert "alembic upgrade head" in build
    assert "pip install -r requirements.txt" in build
    # A build step that binds a port is a build step that never finishes.
    assert "uvicorn app.main:app --host" not in build.replace(
        'echo "build ok -- start with: uvicorn app.main:app --host 0.0.0.0 --port \\$PORT"', ""
    )


def test_seeded_tools_cannot_shadow_a_real_catalogue_tool(ledger):
    """`tool.name` is UNIQUE and the seeded names are modelled on the real ones.

    Without a namespace the seeder collides with `app.catalogue` the moment the
    gateway has been started once -- and a seeded row that DID land under a real
    tool's name would attach demo revenue to a real tool in every dashboard
    query. This is a regression test for exactly that.
    """
    from sqlmodel import Session as DBSession
    from sqlmodel import select

    from app.cli import invoke
    from app.cli.seed_demo import DEMO_PREFIX
    from app.db import engine
    from app.gateway.server import sync_registered_catalogue
    from app.mcp_app import get_mcp
    from app.models import Tool

    get_mcp()  # registers the real catalogue
    sync_registered_catalogue()
    assert invoke("seed_demo", ["--reset", "--sessions", "2", "--seed", "11"])[0] == 0

    with DBSession(engine) as db:
        tools = db.exec(select(Tool)).all()
    demo = {t.name for t in tools if t.is_demo}
    real = {t.name for t in tools if not t.is_demo}
    assert demo and not (demo & real)
    assert all(name.startswith(DEMO_PREFIX) for name in demo)
