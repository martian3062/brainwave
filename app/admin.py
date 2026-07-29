"""SQLAdmin over the ledger.

This is the operator's view, not the author's dashboard -- raw rows, editable,
for fixing a stuck batch at 2am. The author-facing revenue view is the NiceGUI
app at `/`.

Money is displayed through `app.money.format_atomic`, never as a raw integer and
never as a float, so an operator reading this screen sees the same numbers the
receipts carry.

Every view lists `is_demo`. Seeded rows (`python -m app.cli seed_demo`) are
labelled in the schema rather than by convention, and the operator's raw view is
the one screen where an unlabelled fake row would do the most damage -- it is
the view people screenshot to prove a number. See `app/demo.py`.
"""

from __future__ import annotations

from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.config import settings
from app.db import engine
from app.models import Author, Batch, Call, Receipt, Session, Tool
from app.money import format_atomic


def _money(field: str):
    """Column formatter that renders atomic units as a decimal string."""

    def fmt(model, _attr):  # sqladmin calls this with (obj, attribute)
        value = getattr(model, field, None)
        if value is None:
            return ""
        decimals = getattr(model, "asset_decimals", None) or settings.x402_asset_decimals
        return format_atomic(int(value), decimals, symbol=settings.x402_asset_symbol)

    return fmt


class _BasicAuth(AuthenticationBackend):
    """Single shared operator credential.

    Deliberately minimal: /admin is an internal tool, and a login form beats an
    unauthenticated ledger editor on the public internet. `app.main` refuses to
    boot in production if `admin_password` is empty.
    """

    async def login(self, request: Request) -> bool:
        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        if username == settings.admin_username and password == settings.admin_password:
            request.session.update({"eraya_admin": username})
            return True
        return False

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool | RedirectResponse:
        return bool(request.session.get("eraya_admin"))


class AuthorAdmin(ModelView, model=Author):
    name = "Author"
    name_plural = "Authors"
    icon = "fa-solid fa-user-pen"
    category = "Catalogue"
    column_list = [
        Author.id,
        Author.slug,
        Author.display_name,
        Author.pay_to,
        Author.platform_take_bps,
        Author.is_active,
        Author.is_demo,
        Author.created_at,
    ]
    column_searchable_list = [Author.slug, Author.display_name, Author.pay_to]
    column_sortable_list = [Author.id, Author.slug, Author.created_at]
    column_default_sort = ("id", True)


class ToolAdmin(ModelView, model=Tool):
    name = "Tool"
    name_plural = "Tools"
    icon = "fa-solid fa-screwdriver-wrench"
    category = "Catalogue"
    column_list = [
        Tool.id,
        Tool.name,
        Tool.author_id,
        Tool.scheme,
        Tool.price_atomic,
        Tool.max_price_atomic,
        Tool.meter,
        Tool.enabled,
        Tool.total_calls,
        Tool.total_captured_atomic,
        Tool.is_demo,
    ]
    column_formatters = {
        Tool.price_atomic: _money("price_atomic"),
        Tool.max_price_atomic: _money("max_price_atomic"),
        Tool.total_captured_atomic: _money("total_captured_atomic"),
    }
    column_searchable_list = [Tool.name, Tool.resource_url]
    column_sortable_list = [Tool.id, Tool.name, Tool.total_captured_atomic]
    column_default_sort = ("id", True)


class SessionAdmin(ModelView, model=Session):
    name = "Session"
    name_plural = "Sessions"
    icon = "fa-solid fa-timeline"
    category = "Ledger"
    column_list = [
        Session.id,
        Session.session_id,
        Session.payer,
        Session.status,
        Session.settlement_mode,
        Session.call_count,
        Session.authorized_atomic,
        Session.captured_atomic,
        Session.settled_atomic,
        Session.is_demo,
        Session.opened_at,
    ]
    column_formatters = {
        Session.authorized_atomic: _money("authorized_atomic"),
        Session.captured_atomic: _money("captured_atomic"),
        Session.settled_atomic: _money("settled_atomic"),
    }
    column_searchable_list = [Session.session_id, Session.payer, Session.channel_id]
    column_sortable_list = [Session.id, Session.opened_at, Session.captured_atomic]
    column_default_sort = ("id", True)


class CallAdmin(ModelView, model=Call):
    name = "Call"
    name_plural = "Calls"
    icon = "fa-solid fa-bolt"
    category = "Ledger"
    column_list = [
        Call.id,
        Call.call_id,
        Call.session_id,
        Call.tool_id,
        Call.status,
        Call.scheme,
        Call.authorized_atomic,
        Call.captured_atomic,
        Call.batch_id,
        Call.decline_reason,
        Call.is_demo,
        Call.created_at,
    ]
    column_formatters = {
        Call.authorized_atomic: _money("authorized_atomic"),
        Call.captured_atomic: _money("captured_atomic"),
    }
    column_searchable_list = [Call.call_id, Call.payer, Call.nonce]
    column_sortable_list = [Call.id, Call.created_at, Call.captured_atomic]
    column_default_sort = ("id", True)


class BatchAdmin(ModelView, model=Batch):
    name = "Batch"
    name_plural = "Batches"
    icon = "fa-solid fa-layer-group"
    category = "Ledger"
    column_list = [
        Batch.id,
        Batch.batch_id,
        Batch.session_id,
        Batch.status,
        Batch.call_count,
        Batch.gross_atomic,
        Batch.facilitator_fee_atomic,
        Batch.author_net_atomic,
        Batch.claim_tx_hash,
        Batch.settle_tx_hash,
        Batch.is_demo,
        Batch.settled_at,
    ]
    column_formatters = {
        Batch.gross_atomic: _money("gross_atomic"),
        Batch.facilitator_fee_atomic: _money("facilitator_fee_atomic"),
        Batch.author_net_atomic: _money("author_net_atomic"),
    }
    column_searchable_list = [Batch.batch_id, Batch.claim_tx_hash, Batch.settle_tx_hash]
    column_sortable_list = [Batch.id, Batch.opened_at, Batch.gross_atomic]
    column_default_sort = ("id", True)


class ReceiptAdmin(ModelView, model=Receipt):
    name = "Receipt"
    name_plural = "Receipts"
    icon = "fa-solid fa-receipt"
    category = "Ledger"
    can_create = False
    can_edit = False  # receipts are evidence; editing them defeats the point
    column_list = [
        Receipt.id,
        Receipt.receipt_id,
        Receipt.call_id,
        Receipt.session_id,
        Receipt.batch_id,
        Receipt.scheme,
        Receipt.authorized_atomic,
        Receipt.captured_atomic,
        Receipt.tx_hash,
        Receipt.verify_status,
        Receipt.is_demo,
        Receipt.issued_at,
    ]
    column_formatters = {
        Receipt.authorized_atomic: _money("authorized_atomic"),
        Receipt.captured_atomic: _money("captured_atomic"),
    }
    column_searchable_list = [Receipt.receipt_id, Receipt.tx_hash, Receipt.payer]
    column_sortable_list = [Receipt.id, Receipt.issued_at]
    column_default_sort = ("id", True)


VIEWS = [AuthorAdmin, ToolAdmin, SessionAdmin, CallAdmin, BatchAdmin, ReceiptAdmin]


def mount_admin(app) -> Admin | None:
    """Mount SQLAdmin onto the parent FastAPI app. Returns None if disabled.

    Call BEFORE `ui.run_with()`: NiceGUI mounts at "/" and Starlette resolves
    routes in registration order, so anything registered afterwards is dead.
    """
    if not settings.admin_enabled:
        return None

    admin = Admin(
        app=app,
        engine=engine,
        base_url=settings.admin_path,
        title=f"{settings.app_name} - ledger",
        # SQLAdmin's session middleware needs a secret; reuse NiceGUI's.
        authentication_backend=(
            _BasicAuth(secret_key=settings.storage_secret) if settings.admin_password else None
        ),
    )
    for view in VIEWS:
        admin.add_view(view)
    return admin
