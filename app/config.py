"""Configuration for ERAYA x BRAINWAVE.

Every knob is an environment variable with a working local default, so a fresh
clone runs with `uvicorn app.main:app` and no `.env` at all: SQLite on disk,
Base Sepolia, the public x402 facilitator, batching on.

Money never appears here as a float. Prices are declared in human units
("$0.002") and immediately parsed to integer atomic units by `app.money`; the
cached properties below expose only the integer form.
"""

from __future__ import annotations

from functools import cached_property, lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.money import parse_price

# CAIP-2 identifiers. x402 v2 uses CAIP-2 everywhere -- "base-sepolia" is a v1
# spelling and will not match `PaymentRequirements.network`. Verified against
# x402.mechanisms.evm.constants.NETWORK_CONFIGS.
BASE_MAINNET = "eip155:8453"
BASE_SEPOLIA = "eip155:84532"

# USDC, from the same NETWORK_CONFIGS table (not from memory).
USDC = {
    BASE_MAINNET: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    BASE_SEPOLIA: "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
}

EXPLORER = {
    BASE_MAINNET: "https://basescan.org/tx/",
    BASE_SEPOLIA: "https://sepolia.basescan.org/tx/",
}


class Settings(BaseSettings):
    """Application settings, loaded from the environment and an optional .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- app ---
    app_name: str = "ERAYA x BRAINWAVE"
    app_env: Literal["local", "staging", "production"] = "local"
    debug: bool = False
    log_level: str = "INFO"

    #: Public base URL. Used to build absolute resource URLs in receipts and to
    #: derive the MCP allowed-host list on Render.
    public_base_url: str = "http://localhost:8000"

    #: Signs NiceGUI's session cookie (ui.storage.user / .browser). Override in
    #: production -- Render can generate one.
    storage_secret: str = "dev-only-change-me"

    # ----------------------------------------------------------- database ---
    #: SQLite fallback so the project runs locally with no Postgres. Render
    #: injects a `postgresql://...` URL; `app.db` rewrites the legacy
    #: `postgres://` scheme SQLAlchemy 2 rejects.
    database_url: str = "sqlite:///./brainwave.db"
    db_echo: bool = False
    #: Ignored by SQLite; applied to Postgres.
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # ------------------------------------------------------------- x402 -----
    #: CAIP-2. Switching mainnet <-> testnet is this one variable; no code path
    #: differs.
    x402_network: str = BASE_SEPOLIA
    #: Blank means "use the network's default USDC from x402's own table".
    x402_asset: str = ""
    x402_asset_symbol: str = "USDC"
    x402_asset_decimals: int = 6
    #: EIP-712 domain fields for the asset, needed to sign EIP-3009.
    x402_asset_name: str = "USDC"
    x402_asset_version: str = "2"

    facilitator_url: str = "https://x402.org/facilitator"
    #: Coinbase CDP hosted facilitator credentials (optional; the public
    #: facilitator above needs none on testnet).
    cdp_api_key_id: str = ""
    cdp_api_key_secret: str = ""
    facilitator_label: str = "x402.org"

    #: Where tool revenue lands when an Author row does not override it.
    pay_to_address: str = "0x0000000000000000000000000000000000000000"
    #: Platform take in basis points. 1000 = 10%.
    platform_take_bps: int = Field(default=1000, ge=0, le=10_000)

    #: What one settlement costs us at the facilitator. This is the number the
    #: whole economic argument turns on: at $0.002/call, per-call settlement
    #: burns 50% of revenue; amortised over a 100-call session it is 0.5%.
    #: CDP: first 1,000 tx/month free, then $0.001 each.
    facilitator_fee_price: str = "$0.001"
    facilitator_free_tx_per_month: int = 1000

    #: Default max age of a payment authorization, seconds.
    payment_timeout_seconds: int = 300

    # --------------------------------------------------------- batching -----
    #: Session batching is the product. Off = per-call settlement, which exists
    #: only so the dashboard can show the two side by side.
    # Safe until a tool explicitly opts into the SDK's batch-settlement
    # channel. Exact and upto authorizations are short-lived and must settle on
    # the call that consumed them; deferring those schemes extends unsecured
    # credit and can let the authorization expire before a closer sees it.
    batching_enabled: bool = False
    batch_window_seconds: int = 300
    batch_max_calls: int = 500
    #: Do not open an on-chain settlement for dust; roll it into the next batch.
    batch_min_gross_price: str = "$0.01"

    #: The SDK's batch-settlement channel state contains signed cumulative
    #: vouchers. `app.channels.DatabaseChannelStorage` encrypts that state with
    #: STORAGE_SECRET in a separate Postgres table so the web process and a
    #: one-shot closer share the same durable store.
    channel_storage_backend: Literal["database", "memory"] = "database"

    # ------------------------------------------------- buyer-side demo ------
    #: The demo agent's key. NEVER a real funded mainnet key.
    agent_wallet_key: str = ""
    #: Guardian defaults. These are buyer-side policy, evaluated before any
    #: signature exists -- a payment that was never signed cannot be settled.
    session_budget: str = "$5.00"
    per_call_max: str = "$0.10"
    daily_budget: str = "$50.00"
    escalate_above: str = "$1.00"
    require_receipt: bool = True
    allowlist: str = "mcp://*"

    # -------------------------------------------------------------- mcp -----
    mcp_server_name: str = "eraya-brainwave"
    #: Where the MCP app is mounted in the parent FastAPI app.
    mcp_mount_path: str = "/mcp"
    #: Stateless streamable HTTP: no server-side session affinity, so a single
    #: Render web service can be restarted without stranding MCP sessions.
    #: Payment sessions are OUR concept and live in Postgres, not in the MCP
    #: transport.
    mcp_stateless: bool = True
    mcp_json_response: bool = True

    #: mcp==1.28.1 turns DNS-rebinding protection ON by default and allows only
    #: localhost. Deployed to `*.onrender.com` every MCP request 421s with
    #: "Invalid Host header" unless this list includes the deploy host. Verified
    #: empirically -- see the note in app/mcp_app.py.
    mcp_allowed_hosts: str = ""
    mcp_allowed_origins: str = ""
    mcp_dns_rebinding_protection: bool = True

    # ------------------------------------------------------------ admin -----
    admin_enabled: bool = True
    admin_path: str = "/admin"
    admin_username: str = "admin"
    #: Empty password disables the login form entirely. Fine locally; the
    #: startup check in app.main refuses that combination in production.
    admin_password: str = ""

    # ------------------------------------------------------ validators ------
    @field_validator("x402_network")
    @classmethod
    def _caip2(cls, v: str) -> str:
        if ":" not in v:
            raise ValueError(
                f"x402_network must be a CAIP-2 id such as {BASE_SEPOLIA!r}, got {v!r}. "
                "x402 v2 dropped the v1 'base-sepolia' spelling."
            )
        return v

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("debug", mode="before")
    @classmethod
    def _debug_bool(cls, v: object) -> object:
        """Tolerate conventional build-profile values in ambient DEBUG.

        Windows developer shells and CI launchers sometimes export
        ``DEBUG=release``. Pydantic's strict boolean parser rejects that before
        the application can even import, although the unambiguous meaning is
        "debug disabled".
        """
        if isinstance(v, str):
            normalized = v.strip().lower()
            if normalized in {"release", "prod", "production"}:
                return False
            if normalized in {"debug", "dev", "development"}:
                return True
        return v

    # ------------------------------------------------------- derived --------
    @cached_property
    def asset_address(self) -> str:
        """Asset contract, defaulting to the network's canonical USDC."""
        return self.x402_asset or USDC.get(self.x402_network, "")

    @cached_property
    def is_mainnet(self) -> bool:
        return self.x402_network == BASE_MAINNET

    @cached_property
    def explorer_tx_base(self) -> str:
        return EXPLORER.get(self.x402_network, "")

    @cached_property
    def facilitator_fee_atomic(self) -> int:
        return parse_price(self.facilitator_fee_price, self.x402_asset_decimals)

    @cached_property
    def batch_min_gross_atomic(self) -> int:
        return parse_price(self.batch_min_gross_price, self.x402_asset_decimals)

    @cached_property
    def session_budget_atomic(self) -> int:
        return parse_price(self.session_budget, self.x402_asset_decimals)

    @cached_property
    def per_call_max_atomic(self) -> int:
        return parse_price(self.per_call_max, self.x402_asset_decimals)

    @cached_property
    def daily_budget_atomic(self) -> int:
        return parse_price(self.daily_budget, self.x402_asset_decimals)

    @cached_property
    def escalate_above_atomic(self) -> int:
        return parse_price(self.escalate_above, self.x402_asset_decimals)

    @cached_property
    def allowlist_patterns(self) -> list[str]:
        return [p.strip() for p in self.allowlist.split(",") if p.strip()]

    @cached_property
    def mcp_host_allowlist(self) -> list[str]:
        """Hosts the MCP transport will accept, incl. the public deploy host."""
        hosts = [h.strip() for h in self.mcp_allowed_hosts.split(",") if h.strip()]
        hosts += ["127.0.0.1:*", "localhost:*", "[::1]:*", "testserver"]
        host = _host_of(self.public_base_url)
        if host:
            hosts += [host, f"{host}:*"]
        return _dedupe(hosts)

    @cached_property
    def mcp_origin_allowlist(self) -> list[str]:
        origins = [o.strip() for o in self.mcp_allowed_origins.split(",") if o.strip()]
        origins += [
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        ]
        if self.public_base_url:
            origins.append(self.public_base_url.rstrip("/"))
        return _dedupe(origins)

    @cached_property
    def mcp_public_url(self) -> str:
        """The URL an agent points its MCP client at.

        Note the trailing slash. The FastMCP sub-app registers its endpoint at
        "/" and Starlette's Mount("/mcp") only matches paths *under* /mcp -- so
        the canonical endpoint is /mcp/. app.main registers an explicit redirect
        for the bare /mcp so both work.
        """
        return f"{self.public_base_url.rstrip('/')}{self.mcp_mount_path}/"

    def explorer_url(self, tx_hash: str | None) -> str | None:
        if not tx_hash or not self.explorer_tx_base:
            return None
        return f"{self.explorer_tx_base}{tx_hash}"


def _host_of(url: str) -> str:
    from urllib.parse import urlparse

    netloc = urlparse(url).netloc
    return netloc or ""


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()


settings = get_settings()
