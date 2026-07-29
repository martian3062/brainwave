# Deploying ERAYA × BRAINWAVE to Render

One web service. One Postgres database. Nothing else.

Everything — the paid MCP endpoint, the ledger admin and the NiceGUI dashboard —
is a single ASGI application, because **NiceGUI is FastAPI underneath**. That one
fact is why there is no worker, no Redis, no static site, no Node and no npm
anywhere in this repository.

> Nothing in this repository deploys anything. `render.yaml` and `build.sh` are
> files; Render runs them when *you* click deploy. No script here calls the
> Render API, opens a tunnel, or submits a transaction.

---

## What ends up running

| Path        | Served by | Notes |
|-------------|-----------|-------|
| `/`         | NiceGUI   | Author dashboard. Uses **websockets** (see below). |
| `/mcp/`     | FastMCP   | Paid MCP endpoint, streamable HTTP. Bare `/mcp` 307s here. |
| `/admin/`   | SQLAdmin  | Raw ledger. Password-protected; refuses to boot unprotected in production. |
| `/healthz`  | FastAPI   | Render's health check. **503 if the MCP session manager did not start.** |
| `/api/config` | FastAPI | What an agent operator needs to point a client here. |
| `/api/docs` | FastAPI   | OpenAPI UI, moved off `/` because NiceGUI owns the root. |

---

## 1. Create the database first

The web service reads `DATABASE_URL` from the database via `fromDatabase:`, so
the database has to exist before the first build. If you apply the blueprint as
a whole, Render orders this correctly. If you create the service by hand, create
the Postgres instance first.

```
name:   eraya-brainwave-db
plan:   basic-256mb
region: oregon              # same region as the web service, or every query pays a WAN hop
postgres version: 16
```

Render hands out a `postgres://` URL. SQLAlchemy 2 removed that scheme;
`app/db.py::normalize_database_url` rewrites it to `postgresql://`. You do not
need `dj-database-url` and it is not installed — SQLAlchemy parses the URL
itself.

---

## 2. Set the environment, in this order

The order matters. Three of these are checked by a startup preflight
(`app/main.py::_preflight`) that **refuses to start** rather than serving a
broken payment gateway that reports itself healthy.

### 2a. Set before the first deploy — the app will not boot without them

| Variable | Value | Why it is fatal |
|---|---|---|
| `APP_ENV` | `production` | Turns on the preflight checks below. |
| `DATABASE_URL` | from the database | SQLite in production means the ledger dies with the container. **Refused.** |
| `STORAGE_SECRET` | `generateValue: true` | Signs NiceGUI's session cookie and encrypts durable channel state. The dev default is **refused**. |
| `ADMIN_PASSWORD` | a real secret | Empty means `/admin` has no login form at all. **Refused.** |
| `PAY_TO_ADDRESS` | your receiving address | The zero address burns every payment collected. **Refused.** |

### 2b. Set before the first deploy — wrong values fail *silently*, which is worse

| Variable | Value | What breaks if wrong |
|---|---|---|
| `PUBLIC_BASE_URL` | `https://<service>.onrender.com` | See "the 421 trap" below. This is the single most likely thing to go wrong. |
| `X402_NETWORK` | `eip155:84532` (or `eip155:8453`) | CAIP-2 only. `base-sepolia` is a v1 spelling and matches nothing in x402 v2; `app/config.py` rejects anything without a colon. |
| `FACILITATOR_URL` | `https://x402.org/facilitator` | Public facilitator; no credentials needed on testnet. |

### 2c. Economics — safe defaults, but they are *your* numbers

| Variable | Default | Notes |
|---|---|---|
| `PLATFORM_TAKE_BPS` | `1000` | 10%. Per-author overrides live in the `author` table. |
| `BATCHING_ENABLED` | `false` | Safe default: `exact` and `upto` settle immediately. Enable only for the SDK's `batch-settlement` channel scheme. |
| `CHANNEL_STORAGE_BACKEND` | `database` | Encrypts signed channel material with `STORAGE_SECRET` in Postgres, outside the reporting ledger. |
| `BATCH_WINDOW_SECONDS` | `300` | How long a session accumulates before it is eligible to close. |
| `BATCH_MAX_CALLS` | `500` | Hard cap; a session at the cap closes regardless of the window. |
| `BATCH_MIN_GROSS_PRICE` | `$0.01` | Dust floor. Below this, closing costs more than it collects, so it rolls into the next batch. |
| `FACILITATOR_FEE_PRICE` | `$0.001` | Cost of ONE on-chain settlement. Every fee-load figure on the dashboard derives from it. |

### 2d. Optional

| Variable | When you need it |
|---|---|
| `CDP_API_KEY_ID` / `CDP_API_KEY_SECRET` | Only for Coinbase's hosted facilitator. The public one needs neither. |
| `MCP_ALLOWED_HOSTS` | Only if you put a custom domain or a proxy in front. Comma-separated; the host from `PUBLIC_BASE_URL` is added automatically. |
| `ADMIN_USERNAME` | Defaults to `admin`. |

### Do **not** set `AGENT_WALLET_KEY`

It is a **buyer-side** key. This service is the seller and never signs a payment
authorization. A private key in a web service's environment is a private key in
every log line, crash dump and support session that touches that environment.

---

## 3. Build and start commands

```
buildCommand:  bash build.sh
startCommand:  uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips '*'
```

**`bash build.sh`, not `./build.sh`.** The executable bit does not survive a
clone from a Windows checkout, and the deploy dies with `Permission denied`
before running a single line of it.

**`--proxy-headers`** so the app sees the real scheme and host behind Render's
proxy. Without it, absolute URLs in receipts come out as `http://` on an HTTPS
service.

`build.sh` does three things:

1. `pip install -r requirements.txt`
2. `alembic upgrade head` — **Alembic owns the schema.** `app.db.create_all()`
   only ever runs against the local SQLite fallback, so this line is the only
   thing standing between a deploy and an empty database.
3. `python -m app.cli doctor --skip-ledger` — protocol conformance, no database,
   no network, about a second. A build that cannot produce a valid 402 should
   not become a running payment gateway. Set `SKIP_DOCTOR=1` to bypass in an
   emergency.

`runtime.txt` pins `python-3.11.9` and `render.yaml` sets `PYTHON_VERSION` to
the same value. Render honours either; keep them equal, because disagreeing
values are a silent version skew.

---

## 4. Websockets

**Render supports websockets on the standard HTTP port. There is nothing to
enable, no separate service, and no extra configuration.**

Both of these depend on it:

- **NiceGUI** opens a websocket per browser tab for its reactive updates. Without
  one the dashboard renders once and then never changes.
- **MCP streamable HTTP** uses long-lived connections for server-sent events.

Two practical consequences:

- **Do not put a `BaseHTTPMiddleware` in front of streaming routes.** It buffers
  response bodies and will hang SSE. The x402 HTTP paywall is one, which is why
  `app/main.py` documents that it must short-circuit `/mcp*`, `/` and
  `/_nicegui*`.
- **One instance, or sticky sessions.** NiceGUI keeps per-client state in the
  process. Scaling to several instances without session affinity gives users
  intermittently blank pages. The MCP transport is configured stateless
  (`MCP_STATELESS=true`) precisely so *it* does not care — payment sessions are
  our concept and live in Postgres, not in the MCP transport.

---

## 5. The 421 trap — read this before you debug anything else

`mcp==1.28.1` ships DNS-rebinding protection **enabled by default**, with an
allowed-host list of `['127.0.0.1:*', 'localhost:*', '[::1]:*']`.

Deployed to `something.onrender.com`, **every MCP request returns
421 "Invalid Host header"** until the deploy host is on that list. Nothing is
logged that points at the cause, and every other route on the service works
perfectly.

`app/mcp_app.py` derives the allowlist from `PUBLIC_BASE_URL`, so the fix is to
set that variable correctly. If you front the service with a custom domain, add
it to `MCP_ALLOWED_HOSTS` as well.

Verify after deploying:

```bash
curl -s https://<service>.onrender.com/healthz | jq
# expect: {"status":"ok", "mcp_session_manager":"started", ...}

curl -s -X POST https://<service>.onrender.com/mcp/ \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
# 421 here means the host allowlist. 200 with a protocolVersion means you are done.
```

`"mcp_session_manager": "NOT STARTED"` means the mounted MCP app's lifespan did
not run — Starlette never dispatches `lifespan` into a `Mount`, which is why
`app/main.py` enters it explicitly. Every `tools/call` would 500. `/healthz`
returns 503 in that state so Render's health check catches it instead of your
first paying agent.

---

## 6. Closing batches in production

There is **no background worker**, and that is a decision rather than an
omission: the only code path that can move money should not live in an
always-on web process where a stray restart loop becomes a stray settlement
loop.

Closing is a command:

```bash
# plan only. Writes nothing, sends nothing. Safe to run anywhere, any time.
python -m app.cli close_batch --all

# a real settlement needs all four flags, and there is no shorter form:
python -m app.cli close_batch --session sess_... --live --yes \
    --confirm-network eip155:84532
```

Run it from a **Render Cron Job** pointed at the same repository and database
(schedule it slightly longer than `BATCH_WINDOW_SECONDS`), or by hand from a
shell. `--confirm-network` exists to catch the specific mistake of a `.env` that
still says mainnet; on mainnet a further `--i-understand-mainnet` is required.

Closing is two on-chain steps — `claim`, then sweep — so `Batch` carries both
`claim_tx_hash` and `settle_tx_hash`, and the row advances
`OPEN → CLAIMING → CLAIMED → SETTLING → SETTLED` with a commit before each
network call. A crash leaves a row saying exactly how far it got; `--resume`
picks it up.

`--live` needs the signed cumulative vouchers, which are **not** in the six
reporting ledger tables. `app.channels` encrypts them with `STORAGE_SECRET` in
the separate `channel_state` table, allowing the web service and a cron closer
to share and resume channel state without placing spendable material in reports.
The command still refuses live close when no claimable voucher exists.

---

## 7. After the first deploy

```bash
# 1. is it alive and is MCP really started?
curl -s https://<service>.onrender.com/healthz | jq

# 2. does the ledger reconcile, and are the receipts intact?
python -m app.cli doctor            # exit 0 means clean; 1 means look at the FAILs

# 3. what would close right now?
python -m app.cli close_batch --all
```

Do **not** run `python -m app.cli seed_demo` against production. It refuses when
`APP_ENV=production` unless you pass `--force`, and the reason is in
`app/demo.py`: a screenshot of fabricated USDC revenue presented as a payment
gateway's ledger is a lie regardless of intent. If you ever do seed a shared
environment by accident, `seed_demo --reset-only` removes every demo row and
nothing else.

---

## 8. Costs and limits, honestly

- **`plan: starter`, not `free`.** The free tier sleeps after inactivity. A
  sleeping payment gateway drops the first agent request that wakes it, and an
  MCP client reads that as a broken server.
- **`basic-256mb` Postgres** is ample for a hackathon ledger: six narrow reporting
  tables plus one encrypted channel-state table, integer money columns, and
  indexes sized to the dashboard's queries.
- **One instance.** See the websockets section. If you need more, add session
  affinity first.

---

## Deployment checklist

- [ ] Postgres created, same region as the web service
- [ ] `APP_ENV=production`
- [ ] `STORAGE_SECRET` generated (not the dev default)
- [ ] `ADMIN_PASSWORD` set to a real secret
- [ ] `PAY_TO_ADDRESS` set to an address you control (**not** the zero address)
- [ ] `PUBLIC_BASE_URL` exactly matches the deployed URL
- [ ] `X402_NETWORK` is CAIP-2 and is the network you meant
- [ ] `BATCHING_ENABLED=false` unless the SDK batch-settlement path has been tested live
- [ ] `CHANNEL_STORAGE_BACKEND=database`
- [ ] `AGENT_WALLET_KEY` is **not** set
- [ ] Build succeeded, including `alembic upgrade head` and the doctor gate
- [ ] `/healthz` returns `"mcp_session_manager": "started"`
- [ ] A raw `initialize` POST to `/mcp/` returns 200, not 421
- [ ] `/admin/` asks for a password
- [ ] `python -m app.cli doctor` exits 0
