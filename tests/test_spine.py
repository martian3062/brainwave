"""Spine tests. Every claim the spine's docstrings make, checked.

No server is bound; these run entirely through Starlette's TestClient.

    .venv/Scripts/python -m pytest tests -q
"""

from __future__ import annotations

import contextlib
import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")  # in-memory, StaticPool
os.environ.setdefault("PUBLIC_BASE_URL", "http://testserver")

from sqlalchemy.exc import IntegrityError  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from app.money import PriceError, fee_load_bps, format_atomic, parse_price, split_take  # noqa: E402

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "tests", "version": "0"},
    },
}
HDRS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def client():
    from app.db import create_all
    from app.main import app

    create_all()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------- money ----


def test_price_parsing_is_exact():
    assert parse_price("$0.002", 6) == 2_000
    assert parse_price("0.002", 6) == 2_000
    assert parse_price("0.002 USDC", 6) == 2_000
    assert parse_price("$1.00", 6) == 1_000_000
    assert parse_price(2_000, 6) == 2_000  # already atomic


def test_sub_atomic_price_is_an_error_not_a_rounding():
    with pytest.raises(PriceError):
        parse_price("$0.0000001", 6)
    with pytest.raises(PriceError):
        parse_price("-$1.00", 6)


def test_format_round_trips():
    assert format_atomic(2_000, 6) == "0.002000"
    assert format_atomic(2_000, 6, symbol="USDC") == "0.002000 USDC"


@pytest.mark.parametrize("gross", [0, 1, 7, 999, 2_000, 199_999, 10**12 + 7])
@pytest.mark.parametrize("bps", [0, 1, 250, 1000, 3333, 10_000])
def test_revenue_split_conserves_exactly(gross, bps):
    platform, author = split_take(gross, bps)
    assert platform + author == gross
    assert platform >= 0 and author >= 0


def test_the_headline_economic_claim():
    """$0.002 per call, $0.001 per settlement: 50% fee load, or 0.5% batched."""
    price, fee = parse_price("$0.002", 6), parse_price("$0.001", 6)
    assert fee_load_bps(fee, price) == 5_000  # 50.00% -- per-call settlement
    assert fee_load_bps(fee, price * 100) == 50  # 0.50% -- one settlement, 100 calls


# --------------------------------------------------------------- ledger ----


def test_capture_may_not_exceed_authorization():
    """The `upto` invariant is a database CHECK, not a code comment."""
    from sqlmodel import Session as DBSession

    from app.db import create_all, engine
    from app.models import Call, PaySession, Scheme

    create_all()
    with DBSession(engine) as db:
        s = PaySession(session_id="sess_check", payer="0xA", network="eip155:84532", asset="0xU")
        db.add(s)
        db.commit()
        db.refresh(s)

        bad = Call(
            call_id="call_bad",
            session_id=s.id,
            tool_id=1,
            payer="0xA",
            pay_to="0xB",
            network="eip155:84532",
            asset="0xU",
            scheme=Scheme.UPTO,
            authorized_atomic=50_000,
            captured_atomic=50_001,  # one atomic unit over the ceiling
            platform_fee_atomic=0,
            author_net_atomic=50_001,
        )
        db.add(bad)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


def test_no_float_columns_anywhere_in_the_ledger():
    from sqlalchemy import Float
    from sqlmodel import SQLModel

    import app.models  # noqa: F401

    offenders = [
        f"{t.name}.{c.name}"
        for t in SQLModel.metadata.tables.values()
        for c in t.columns
        if isinstance(c.type, Float)
    ]
    assert offenders == [], f"float columns in a money ledger: {offenders}"


def test_enum_columns_store_the_x402_wire_value_not_the_member_name():
    """SQLModel's default for a StrEnum field is `sa.Enum`, which on Postgres
    creates a native ENUM type and stores the member NAME. The ledger would then
    hold 'BATCH_SETTLEMENT' where the x402 wire format says 'batch-settlement',
    and every new status would need an ALTER TYPE. `sa_type=ENUM_COL` fixes both.
    """
    from sqlalchemy import Enum as SAEnum
    from sqlalchemy import text
    from sqlmodel import Session as DBSession
    from sqlmodel import SQLModel

    from app.db import create_all, engine
    from app.models import PaySession, Scheme

    native = [
        f"{t.name}.{c.name}"
        for t in SQLModel.metadata.tables.values()
        for c in t.columns
        if isinstance(c.type, SAEnum)
    ]
    assert native == [], f"native enum columns: {native}"

    create_all()
    with DBSession(engine) as db:
        db.add(
            PaySession(
                session_id="sess_wire",
                payer="0xA",
                network="eip155:84532",
                asset="0xU",
                scheme=Scheme.BATCH_SETTLEMENT,
            )
        )
        db.commit()
        stored = db.exec(
            text("SELECT scheme, status FROM pay_session WHERE session_id='sess_wire'")
        ).one()
    assert stored[0] == "batch-settlement"
    assert stored[1] == "open"


def test_money_columns_are_bigint():
    """A bare `int` maps to 32-bit INTEGER on Postgres and overflows at ~2147 USDC."""
    from sqlalchemy import BigInteger
    from sqlmodel import SQLModel

    import app.models  # noqa: F401

    wrong = [
        f"{t.name}.{c.name}"
        for t in SQLModel.metadata.tables.values()
        for c in t.columns
        if c.name.endswith("_atomic") and not isinstance(c.type, BigInteger)
    ]
    assert wrong == [], f"money columns that are not BIGINT: {wrong}"


def test_release_profile_does_not_break_boolean_debug_parsing(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("DEBUG", "release")
    assert Settings(_env_file=None).debug is False


def test_unsafe_deferred_settlement_is_off_by_default(monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("BATCHING_ENABLED", raising=False)
    assert Settings(_env_file=None).batching_enabled is False


# ---------------------------------------------------------------- spine ----


def test_mcp_lifespan_actually_ran(client):
    """The one integration point that silently half-works."""
    body = client.get("/healthz").json()
    assert body["mcp_session_manager"] == "started"


def test_lifespan_syncs_catalogue_after_first_boot_schema_creation(client):
    """A clean SQLite clone must not miss catalogue rows on its first start."""
    from sqlmodel import Session as DBSession
    from sqlmodel import select

    from app.db import engine
    from app.gateway.paid import REGISTRY
    from app.models import Tool

    assert client.get("/healthz").status_code == 200
    with DBSession(engine) as db:
        tools = db.exec(select(Tool).where(Tool.is_demo.is_(False))).all()
    assert len(REGISTRY) == 7
    assert {spec.name for spec in REGISTRY}.issubset({tool.name for tool in tools})


def test_mounted_lifespan_does_not_run_on_its_own():
    """Proof of the negative: Starlette never dispatches lifespan into a Mount.

    This is why `combined_lifespan` exists. If a future Starlette starts
    propagating lifespan to mounted apps, this test fails and the workaround can
    be deleted.
    """
    from fastapi import FastAPI
    from starlette.applications import Starlette

    fired: list[str] = []

    @contextlib.asynccontextmanager
    async def sub_lifespan(_app):
        fired.append("sub")
        yield

    parent = FastAPI()
    parent.mount("/sub", Starlette(lifespan=sub_lifespan))
    with TestClient(parent):
        pass
    assert fired == [], "mounted lifespan ran -- combined_lifespan may be redundant now"


def test_paid_mcp_endpoint_speaks_json_rpc(client):
    r = client.post("/mcp/", json=INIT, headers=HDRS)
    assert r.status_code == 200, r.text
    assert r.json()["result"]["protocolVersion"]


def test_bare_mcp_path_redirects_instead_of_hitting_nicegui(client):
    """Mount('/mcp') never matches the bare '/mcp'; NiceGUI's Mount('/') would
    otherwise answer an agent with an HTML 404."""
    r = client.post("/mcp", json=INIT, headers=HDRS, follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"].endswith("/mcp/")

    r = client.post("/mcp", json=INIT, headers=HDRS, follow_redirects=True)
    assert r.status_code == 200


def test_tools_list_includes_the_free_tool(client):
    r = client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers=HDRS,
    )
    assert r.status_code == 200, r.text
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert "gateway_info" in names


def test_nicegui_serves_the_dashboard_at_root(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "nicegui" in r.text.lower()


def test_admin_is_mounted_and_reachable(client):
    assert client.get("/admin", follow_redirects=False).status_code == 307
    assert client.get("/admin/", follow_redirects=True).status_code == 200


def test_public_config_is_precise_about_the_two_transports(client):
    cfg = client.get("/api/config").json()
    assert cfg["x402Version"] == 2
    # Over MCP payment rides in _meta; the header names belong to the HTTP path
    # only. Confusing the two is the single most common integration mistake.
    assert cfg["paymentTransport"]["mcp"]["requestMeta"] == "x402/payment"
    assert "PAYMENT-SIGNATURE" in cfg["paymentTransport"]["http"]["request"]


# ------------------------------------------------------------ x402 SDK -----


def test_x402_mcp_resource_info_is_the_wrong_class():
    """Regression guard for a real bug in x402==2.16.0.

    The SDK's own docstring says `from x402.mcp import ResourceInfo`, but that
    resolves to `x402.mcp.types.ResourceInfo`, a plain class with no
    `model_dump()`. `create_payment_wrapper`'s 402 path calls
    `resource.model_dump(by_alias=True, ...)`, so using the documented import
    raises AttributeError on the FIRST unpaid tool call.

    The correct import is `from x402.schemas.payments import ResourceInfo`.
    When upstream fixes this, this test fails and the workaround can go.
    """
    import x402.mcp as xmcp
    from x402.schemas.payments import ResourceInfo as SchemaResourceInfo

    assert not hasattr(xmcp.ResourceInfo("mcp://tool/x"), "model_dump")
    assert hasattr(SchemaResourceInfo(url="mcp://tool/x"), "model_dump")
