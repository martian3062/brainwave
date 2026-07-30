"""Gateway-only settings.

Deliberately a SEPARATE settings object rather than new fields on
`app.config.Settings`. Everything here describes what the *tools* need -- an
upstream API base, a Casper node, an LLM key -- and none of it is part of the
payment spine. Keeping them apart means the spine's config stays the answer to
"how is this gateway paid" and this file stays the answer to "what can its tools
reach", and neither file has to be edited to change the other.

Every value has a default that makes the app boot on a completely empty `.env`.
Where a default would mean "silently call something", the default is empty and
the tool degrades to a clearly-labelled offline mode instead. Tools never raise
at import time for a missing key -- that is checked once, at call time, and
turned into a structured result the agent can read.
"""

from __future__ import annotations

from functools import cached_property, lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.money import parse_price

__all__ = ["GatewaySettings", "gateway_settings", "get_gateway_settings"]


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------ catalogue --
    #: Author the built-in catalogue is registered under. Revenue for these
    #: tools is credited here in the ledger.
    catalogue_author_slug: str = "eraya"
    catalogue_author_name: str = "ERAYA Swarm"
    #: Re-sync tool rows (price, description, enabled) from code on every boot.
    #: Off means the database is authoritative and an operator can reprice a
    #: tool from /admin without a redeploy -- which is the point of Tool.price.
    catalogue_sync_prices: bool = True

    # ----------------------------------------------------- upstream: swarm ---
    #: Base URL of a running ERAYA backend, e.g. https://host/eraya_api.
    #: Empty (the default) puts the swarm tools in self-contained mode: they run
    #: the pipeline locally and label the result `engine: "local"` rather than
    #: calling anything. Never point this at a host you do not operate.
    eraya_api_base: str = ""
    eraya_api_timeout: float = 15.0
    #: HMAC key the local KAVACHA fallback signs its verdicts with. The signature
    #: proves the verdict came from this process untampered; it is not a claim
    #: about the upstream swarm's key.
    eraya_audit_key: str = "dev-audit-key"

    # ---------------------------------------------------- upstream: casper ---
    #: Casper 2.0 JSON-RPC endpoint. Reads only -- the gateway never signs or
    #: submits a Casper transaction.
    casper_node_rpc_url: str = "https://node.testnet.casper.network/rpc"
    casper_chain_name: str = "casper-test"
    casper_explorer_base: str = "https://testnet.cspr.live"
    #: Default account for `casper_balance` when the caller omits one.
    casper_public_key: str = ""
    casper_rpc_timeout: float = 15.0

    # ------------------------------------------------------ upstream: base ---
    #: Base/EVM JSON-RPC endpoint -- the chain this gateway actually settles on.
    #: Defaults to the public Base Sepolia RPC so `base_balance` / `base_transaction`
    #: / `base_chain_status` boot and sell on a completely empty `.env`; point this
    #: at a dedicated provider (Quicknode, Alchemy, ...) for production rate limits.
    #: Reads only -- the gateway holds no EVM signing key for these calls.
    base_rpc_url: str = "https://sepolia.base.org"
    base_rpc_timeout: float = 15.0

    # ------------------------------------------------- upstream: firecrawl ---
    #: Absent, `scrape_url` reports itself unavailable and captures nothing --
    #: same contract as `analyze_contract` without ANTHROPIC_API_KEY.
    firecrawl_api_key: str = ""
    firecrawl_api_base: str = "https://api.firecrawl.dev"
    firecrawl_timeout: float = 30.0

    # ------------------------------------------------------- upstream: LLM ---
    #: Anthropic API key for `analyze_contract`. Absent, the tool returns a
    #: structured "unavailable" result and captures nothing.
    anthropic_api_key: str = ""
    #: Claude Opus 5. Thinking is on by default on this model, and `max_tokens`
    #: caps thinking + response together -- see app/gateway/tools/analysis.py.
    analysis_model: str = "claude-opus-5"
    analysis_max_tokens: int = 8_000
    analysis_effort: str = "medium"
    analysis_timeout: float = 120.0
    #: Hard ceiling on input size, so one caller cannot hand us a 900k-token
    #: contract and make us eat the ceiling on a metered tool.
    analysis_max_input_chars: int = 60_000

    # ------------------------------------------------------------- pricing ---
    #: Declared in human units and parsed to integer atomic units once, below.
    #: These are the defaults the catalogue seeds with; the Tool row wins after.
    price_injection_sim: str = "$0.01"
    price_casper_read: str = "$0.001"
    price_base_read: str = "$0.001"
    price_scrape_url: str = "$0.003"
    price_summarize_bug_report: str = "$0.001"
    #: `analyze_contract` is `upto`: this is the authorized ceiling.
    price_analyze_contract_max: str = "$0.05"
    #: ...of which this much is charged unconditionally once the model runs.
    price_analyze_contract_base: str = "$0.002"
    #: ...plus this much per 1k tokens (input + output).
    price_analyze_contract_per_ktok: str = "$0.004"

    # ------------------------------------------------------------ derived ----
    @cached_property
    def decimals(self) -> int:
        from app.config import settings

        return settings.x402_asset_decimals

    @cached_property
    def injection_sim_atomic(self) -> int:
        return parse_price(self.price_injection_sim, self.decimals)

    @cached_property
    def casper_read_atomic(self) -> int:
        return parse_price(self.price_casper_read, self.decimals)

    @cached_property
    def base_read_atomic(self) -> int:
        return parse_price(self.price_base_read, self.decimals)

    @cached_property
    def scrape_url_atomic(self) -> int:
        return parse_price(self.price_scrape_url, self.decimals)

    @cached_property
    def summarize_atomic(self) -> int:
        return parse_price(self.price_summarize_bug_report, self.decimals)

    @cached_property
    def analyze_max_atomic(self) -> int:
        return parse_price(self.price_analyze_contract_max, self.decimals)

    @cached_property
    def analyze_base_atomic(self) -> int:
        return parse_price(self.price_analyze_contract_base, self.decimals)

    @cached_property
    def analyze_per_ktok_atomic(self) -> int:
        return parse_price(self.price_analyze_contract_per_ktok, self.decimals)

    @cached_property
    def eraya_api_configured(self) -> bool:
        return bool(self.eraya_api_base.strip())

    @cached_property
    def llm_configured(self) -> bool:
        return bool(self.anthropic_api_key.strip())

    @cached_property
    def firecrawl_configured(self) -> bool:
        return bool(self.firecrawl_api_key.strip())


@lru_cache(maxsize=1)
def get_gateway_settings() -> GatewaySettings:
    return GatewaySettings()


gateway_settings = get_gateway_settings()
