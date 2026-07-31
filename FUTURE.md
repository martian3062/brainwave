# FUTURE — TRAPPIST × BRAINWAVE

> Working notes: where this stands, what is genuinely blocking, and what gets built next.
> Written 29 July 2026. Keep it honest — this file is for us, not for judges.

---

## Where this stands in one paragraph

The plumbing is real and verified: a single NiceGUI/FastAPI service mounts a paid MCP server
at `/mcp`, SQLAdmin at `/admin`, and a Python-only dashboard at `/`. 552 tests pass, Alembic
migrates cleanly both directions, and the MCP endpoint has been driven end to end in-process —
`initialize` → `tools/list` (12 tools, 5 free / 7 paid) → unpaid `tools/call` returning
well-formed 402 challenges. The live catalogue now uses the same payment core as the safety
tests, including exact metering, payer/nonce parsing, receipts and settlement. **No payment has
ever settled on-chain**, so the next milestone is still a verifiable Base Sepolia transaction.

**Nothing is deployed.** Render config is written and never run.

---

## Verified working

| | Evidence |
|---|---|
| FastAPI + NiceGUI + MCP compose in one ASGI app | `/mcp`, `/admin`, root all mounted; route order confirmed |
| **MCP session-manager lifespan actually runs** | drove the raw ASGI lifespan protocol; `session_manager._has_started == True`; without it every `tools/call` returns 500 |
| 402 challenges are spec-shaped | all 7 paid tools return `{"x402Version":2,"accepts":[…]}` unpaid |
| Money is integer atomic units end to end | a test asserts zero `Float` columns and that every `*_atomic` is `BigInteger` |
| DB and payment core enforce `captured <= authorized` | `ck_call_capture_le_authorized`; a mismatched facilitator amount fails the call and issues no receipt |
| Alembic | `upgrade head` → `downgrade base` → re-upgrade, zero drift |
| Offline demo | `python -m app.cli simulate` prints the full protocol trace with no network and no funds |
| Encrypted durable channel storage | `app/channels.py` + migration `0003_channel_state`; SDK vouchers stay outside the reporting ledger |
| 552 tests | pass locally |

---

## Resolved locally on 29 July 2026

These were the six blockers found in the first audit. They are fixed in the local build and
covered by regression tests; “resolved” here does **not** mean deployed or transacted live.

1. **The served gateway and tested payment core are unified.** `app/gateway/paid.py` is now a
   transport adapter onto `app/pay/decorator.py`, preserving MCP `_meta`, Bazaar discovery
   metadata and the shared settlement/receipt path.
2. **Over-capture fails closed.** The legacy ledger no longer inflates authorization to match
   capture, and the shared core requires the facilitator-reported amount to equal the requested
   capture. A mismatch marks the call failed and emits no false receipt.
3. **Settlement has a safe default and a durable batch path.** `BATCHING_ENABLED=false` means
   `exact`/`upto` settle per call. Only the SDK `batch-settlement` scheme may defer.
   `app/channels.py` encrypts signed channel material in Postgres; `close_batch` selects only
   batch-settlement sessions and resumes claim/sweep from recorded hashes.
4. **`upto` preserves payer and nonce.** The parser reads `permit2Authorization`, accepts integer
   nonces, and refuses to execute when the payer cannot be identified.
5. **Receipts have one digest.** Every writer and verifier uses the canonical `sha256:` body
   digest, and batch finalisation updates and re-hashes the same receipt body.
6. **`doctor` is read-only.** It builds a throwaway MCP catalogue for inspection and filters
   real rows without synchronising anything into the audited ledger.

---

## Then: make it earn

In order, once the blockers are gone.

- [ ] **One real settled payment on Base Sepolia.** Faucet USDC → sign an EIP-3009 authorization
      → facilitator verify → tool executes → settle → receipt carries a real `txHash` that
      resolves on Basescan. Until this exists nothing else matters.
- [ ] **Batch conservation proven against a live facilitator**, not just in tests:
      `Σ(captured) == settled on-chain`, exactly.
- [ ] **Deploy to Render** — one web service + Postgres, `uvicorn app.main:app`. Config exists;
      never run. Set `PUBLIC_BASE_URL` or MCP returns **421 Invalid Host** on `*.onrender.com`.
- [ ] **Claude Desktop end to end** — point it at the deployed gateway, ask it to analyse a
      contract, watch it pay. That is the demo.
- [ ] **ERC-8004 registry deployed** and reputation earned from settled volume, not seeded.

## Later

- [ ] Bazaar / x402scan listing so agents discover the tools without a human
- [ ] Solana settlement path (`x402.mechanisms.svm` already exists)
- [ ] Streaming settlement for long-running tools
- [ ] Revenue sharing for composed tools — a paid tool calling a paid tool splits automatically
- [ ] Author payouts to fiat

---

## SDK findings worth reporting upstream

These came out of building against the real wheels and are the strongest material for
`feedback.md` — concrete friction, not praise.

1. **`x402.mcp.ResourceInfo` is a bug, and the SDK's own docstring is wrong.** It resolves to
   `x402.mcp.types.ResourceInfo`, a plain class with no `model_dump()`, but
   `x402/mcp/server.py::_create_payment_required_result` calls
   `resource.model_dump(by_alias=True, exclude_none=True)`. Following the documented import
   raises `AttributeError` on the **first unpaid call** — the 402 challenge itself. The correct
   import is `x402.schemas.payments.ResourceInfo`. Reproduced and pinned by a test.
2. **`mcp==1.28.1` enables DNS-rebinding protection by default** (`allowed_hosts` limited to
   localhost), so every MCP request on a deployed host returns **421 Invalid Host header**.
3. **A mounted sub-app's lifespan never runs.** `Mount` matches only `http`/`websocket`;
   `lifespan` is dispatched by the top router alone. Since `FastMCP.streamable_http_app()`
   carries its session manager in its lifespan, mounting it naively yields routes that resolve,
   no warning, and 500 on every `tools/call`. Fix:
   `async with mcp_asgi.router.lifespan_context(mcp_asgi)` inside the parent lifespan.
4. **`streamable_http_path` defaults to `/mcp`**, so `mount("/mcp", app)` gives `/mcp/mcp`; and
   `Mount("/mcp")` never matches the bare `/mcp`, which NiceGUI's `Mount("/")` then swallows
   into an HTML 404. Build with `streamable_http_path="/"` plus an explicit 307.
5. **Network IDs are CAIP-2** (`eip155:84532`), not the `base-sepolia` spelling in most docs.

---

## Decisions already made (don't relitigate)

- **NiceGUI over Django.** NiceGUI *is* FastAPI, so x402's official `middleware/fastapi` applies
  and `FastMCP.streamable_http_app()` (a Starlette app) mounts in one line. Django needed a
  hand-written ASGI dispatcher with fragile lifespan handling, and x402 ships no Django
  middleware.
- **No Node, no npm, no TypeScript.** The x402 Python SDK has the full buyer side
  (`x402MCPClient`) and `EthAccountSigner` does EIP-3009, so JS buys nothing.
- **We did not invent paid MCP.** `create_payment_wrapper` and `create_x402_mcp_client` are the
  SDK's. BRAINWAVE adds pricing, metering, the ledger, batching economics, the buyer-side
  Guardian, receipts with reconciliation, the dashboard and the CLI. Say this plainly
  everywhere — a judge who knows the SDK will check.
- **Render only.** Vercel cannot host this: NiceGUI needs a persistent Socket.IO connection,
  the MCP session manager is stateful, and batch close is an explicit resumable command that
  shares Postgres state. A static page on Vercel pointing at Render is fine; a Vercel-hosted
  frontend is not.

---

## Open questions

- Does `batch-settlement` genuinely fit MCP tool calls, or is the channel/voucher model too
  heavy for a $0.002 call? Read `x402/mechanisms/evm/batch_settlement/server/scheme.py` before
  committing to it.
- Is the 10% platform take defensible when we are also the tool author? Probably needs to be
  0% for our own tools and 10% for third parties.
- `analyze_contract` needs a real LLM backend before its `upto` metering means anything.
  Currently it meters tokens it does not actually spend.

---

## Related work

| Project | Path | Chain | Status |
|---|---|---|---|
| **ERAYA Casper** | `E:\microsoft_eraya` | Casper testnet | live at eraya.online, contracts deployed |
| **ORISIS** | `E:\wtf` | Ethereum Sepolia | contracts compile, 66 tests pass, not deployed |
| **BRAINWAVE** | `E:\brainwave` | Base | this file |

ERAYA's `core/casper/x402.py` is Casper-native with a bespoke proof format and is **not**
x402-compliant. It was superseded here, not ported — keep saying so in the disclosure.
