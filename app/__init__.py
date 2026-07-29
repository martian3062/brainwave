"""ERAYA x BRAINWAVE -- paid MCP.

MCP won the tool layer. It has no payment layer. This is that layer: every agent
tool call metered and settled with real x402 on Base.

Honest scope statement, repeated in the docs and in the code that matters:
the x402 Python SDK ALREADY implements paid MCP. `x402.mcp.create_payment_wrapper`
does the 402 challenge, facilitator verify, execution and settle, and carries
payment in the MCP `_meta` keys `x402/payment` / `x402/payment-response`. We did
not invent that and do not claim to. What this project adds on top is the part
the SDK does not have:

  * a buyer-side spend Guardian (budgets, allowlists, escalation) -- `x402/hook_policy.py`
    guards hook *mutations*, not spend, so this genuinely does not exist upstream
  * a revenue ledger with per-author accounting and a platform take
  * session batching and receipt reconciliation: session -> batch -> on-chain tx
  * an author dashboard and a conformance/simulation CLI

Layout:
    app.config      settings, every env var, SQLite fallback
    app.money       integer atomic units; the only money conversion in the repo
    app.models      Author / Tool / Session / Call / Receipt / Batch
    app.db          engine + sessions
    app.mcp_app     the FastMCP server (mount path + DNS-rebinding gotchas)
    app.admin       SQLAdmin over the ledger
    app.dashboard   NiceGUI pages
    app.demo        the is_demo guarantee: seeded rows can never pass for real
    app.cli         simulate / doctor / close_batch / seed_demo (stdlib argparse)
    app.main        the spine: composition order and the combined lifespan

The fastest way to see the whole thing work, with no server, no database rows,
no .env, no network and no funds:

    python -m app.cli simulate
"""

__version__ = "0.1.0"
