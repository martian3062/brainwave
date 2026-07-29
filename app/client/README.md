# The buyer side

A paying MCP client with a spend policy. Pure Python — no Node, no npm, no TypeScript.

```
app/client/
  guardian.py   spend policy, evaluated before any signature exists   <- the contribution
  signer.py     EIP-3009 signing (x402's EthAccountSigner) + an exact signature audit
  shim.py       the paying MCP client: SDK payment flow + Guardian + session accounting
  verify.py     receipt canonicalization, digest, attestation, on-chain transfer check
  __main__.py   CLI, and the stdio proxy that carries paid tools into Claude Desktop
```

---

## What is ours and what is the SDK's

This matters more than it usually would, because the x402 Python SDK **already implements
paid MCP on both sides** and a reviewer who knows it will check.

**Not ours.** `x402.mcp` does the whole payment dance: call the tool unpaid, detect the 402
in the result, build a payment payload, retry with it attached to the MCP `_meta` key
`x402/payment`, read the settlement back from `x402/payment-response`. `x402Client` selects
between the server's offers and drives the scheme. `x402.mechanisms.evm.signers.EthAccountSigner`
signs EIP-712 — and therefore EIP-3009 `transferWithAuthorization` — on `eth-account`.
That last fact is why this project has no JavaScript in it anywhere: the spec assumed a
Next.js buyer shim, and it is simply unnecessary.

**Ours.**

| | Why it does not exist upstream |
|---|---|
| `guardian.py` | `x402/hook_policy.py` sounds like spend policy and is not — it governs which extension may mutate which hook. `x402ClientBase._policies` (`prefer_network`, `prefer_scheme`, `max_amount`) are stateless *requirement selectors*. Nothing upstream can say "this call is affordable, but it is the last $0.02 of the session budget." |
| `verify.py` | x402 v2 standardizes `SettleResponse` and nothing else. `grep -r attestation` over the installed wheel returns nothing. The receipt envelope, its canonical form, its digest and its attestation are ours. |
| session accounting in `shim.py` | authorized vs captured vs settled, per call, so the buyer can *reconcile* against the seller's ledger instead of trusting it. |
| `__main__.py proxy` | an MCP host speaks stdio and holds no wallet, so it cannot answer a 402. |
| `_ClientSessionAdapter` | three defects in the installed SDK. See below. |

---

## The security property

An EIP-3009 authorization is a **bearer instrument**. Once your key has signed one, whoever
holds it can present it to the facilitator and take the money. You cannot un-sign it, and
"the server promised not to" is not a control.

So the only place a spend limit can be enforced is **before the signature exists, on the
buyer's side**. In x402 2.16.0 there is exactly one call site that produces one:

```
x402MCPClient.call_tool
 ├─ call the tool unpaid                        → server returns the 402
 ├─ _payment_required_hooks        ◄══ PHASE 1  Guardian.screen()
 │     abort here → PaymentRequiredError
 └─ payment_client.create_payment_payload
      ├─ _select_requirements_v2                (which offer we will pay)
      ├─ before_payment_creation hooks ◄══ PHASE 2  Guardian.authorize()
      │     AbortResult here → PaymentAbortedError
      └─ ExactEvmScheme.create_payment_payload
           └─ _sign_authorization
                └─ signer.sign_typed_data       ◄══ THE ONLY SIGNATURE
```

Phase 1 screens on the **worst-case** offer (the payment client has not chosen one yet).
Phase 2 is authoritative: the exact selected amount, the budget reservation, and the
escalation. It is registered on the **payment client**, not the MCP client, so it also
bounds anyone who bypasses this shim and drives `x402Client` by hand.

`AuditingSigner` counts calls to `sign_typed_data`, which makes the property *checkable*
rather than merely *claimed*:

```
tests/test_client.py::test_a_denied_call_never_produces_a_signature
```

runs each policy rule end-to-end and asserts `signer.count == 0`. Its control,
`test_an_allowed_call_produces_exactly_one_signature`, proves the harness can see a
signature at all — so a zero means "refused", not "the test is broken".

---

## The ceiling is the exposure

Under `upto`, the amount in `PaymentRequirements` is a **ceiling**, not a price: authorize up
to $0.05, and the server captures what the work actually cost.

It is tempting to charge the budget only the captured amount. That turns a $5 budget into an
unbounded one — 100 authorizations of $0.05 are 100 bearer instruments worth $5 in total,
whatever the server later chooses to capture. So the Guardian **reserves the ceiling** and
refunds the difference on commit. Exposure is always `committed + Σ outstanding reservations`.

A reservation is only ever *released* when `AuditingSigner.count` is unchanged — that is, when
no bearer instrument was created. Everything else fails closed: a signature with no reported
capture books the whole ceiling.

---

## Controls

```python
from app.client import Guardian, SpendPolicy, console_approver

guardian = Guardian(
    SpendPolicy.from_prices(
        session_budget="$5.00",
        per_call_max="$0.10",
        daily_budget="$50.00",
        escalate_above="$1.00",
        allowlist=["mcp://tool/*"],
        require_receipt=True,
        networks=["eip155:84532"],
    ),
    approver=console_approver,  # omit → escalations DENY (fail closed)
)
```

| Control | On breach | Pre-signature? |
|---|---|---|
| `per_call_max` | typed decline, tool call fails cleanly | yes |
| `session_budget` | decline **and** the session freezes; what was already authorized still settles, honestly | yes |
| `daily_budget` | decline; survives a restart via the optional journal file | yes |
| `allowlist` | unknown resources never receive a signature | yes |
| `networks` / `assets` | refuse to sign on a chain or token you did not intend | yes |
| `escalate_above` | approver is asked; **no approver means deny** | yes |
| `require_receipt` | session freezes — see below | **no** |

`require_receipt` is the one control that cannot be preventive, and pretending otherwise
would be dishonest: the payment settles before any receipt can exist. What it does is refuse
to continue. The first unevidenced charge is absorbed and reported; there is no second one.
`test_require_receipt_stops_the_second_charge_not_the_first` pins exactly that.

---

## Three SDK defects this shim works around

All reproduced against the installed wheels (`x402==2.16.0`, `mcp==1.28.1`), each with a test
that fails when upstream fixes it, so the workaround can be deleted rather than rot.

1. **`x402.mcp.create_x402_mcp_client` is SSE-only.** The factory the SDK's own docstring
   shows imports `mcp.client.sse.sse_client` and appends `/sse`. This gateway serves
   **streamable HTTP** at `/mcp/`, so the documented entry point cannot connect to it.
   Separately, the `x402MCPSession` it yields exposes no hooks — no seam for a spend policy.

2. **`x402MCPClient` cannot drive a real `ClientSession`.** It calls
   `mcp_client.call_tool(params_dict)` — one positional dict. `ClientSession.call_tool` is
   `call_tool(name, arguments=None, *, meta=None)`. The dict would bind to `name`.

3. **The settlement response is silently dropped.** `x402/mcp/utils.py::convert_mcp_result`
   reads `getattr(result, "_meta", {})`, but on `mcp.types.CallToolResult` the field is *named*
   `meta` and `_meta` is only its serialization alias — so that getattr always returns `{}`.
   Consequence: `MCPToolCallResult.payment_response` is always `None`. The client pays and
   then reports no proof of payment. (`x402MCPSession._build_result` reads `result.meta`
   correctly, so the SDK's two client classes disagree with each other.)

`_ClientSessionAdapter` + `_NormalizedResult` do exactly three things: translate the calling
convention, reshape the result so the SDK's own extraction works, and nothing else. Every
byte of protocol handling stays in the SDK.

---

## CLI

```bash
# no network, no key — the argument as arithmetic
python -m app.client economics --price '$0.002' --fee '$0.001' --calls 100
python -m app.client policy

# against a gateway
python -m app.client tools    --url http://localhost:8000/mcp/
python -m app.client quote    --url http://localhost:8000/mcp/ --tool run_injection_attack_sim
python -m app.client call     --url http://localhost:8000/mcp/ --tool run_injection_attack_sim --args '{}'
python -m app.client simulate --url http://localhost:8000/mcp/ --tool run_injection_attack_sim --calls 20

# receipts
python -m app.client verify --receipt receipt.json --rpc https://sepolia.base.org --attestor 0x…
```

`--ephemeral-key` mints a throwaway wallet: settlement will fail at the facilitator (it holds
nothing), but the 402, the Guardian and the whole protocol trace are real. Useful for a demo
that must not touch funds.

`--approve auto|ask|deny` chooses how escalations resolve. The default is `deny`.
Signing on a mainnet chain id requires `--allow-mainnet`; refusing by default is deliberate —
the most expensive bug in this design is an agent pointed at Base mainnet by a stale env var.

`call` prints the protocol trace:

```
->  tools/call run_injection_attack_sim {"target": "..."}
<-  402 Payment Required   max=0.050000
[ok] guardian/screen: allow within policy
[ok] guardian/authorize: allow within policy
~   signed 1 EIP-3009 authorization (no gas, no raw transaction)
->  retry  _meta['x402/payment'] = <signed payload>
<-  200 OK   captured=0.007400 of 0.050000 authorized
$   batched -- settles with the session at window close
R   receipt rcpt_01HZY…
```

`simulate` ends with a reconciliation of the Guardian's books against the signer's own tally
of bearer instruments produced. If they disagree, trust the signer.

---

## Claude Desktop

There is no `npx` package in this design and there does not need to be. The buyer side is
Python because the *signing* is Python. `proxy` is a stdio MCP server that re-exposes a paid
remote gateway: it holds the key, enforces the Guardian, pays, and hands the host back an
ordinary tool result. Remote `inputSchema`s are passed through verbatim (which is why it is
built on `mcp.server.lowlevel.Server` rather than FastMCP — FastMCP derives a schema from a
Python signature, which would mean inventing one for every remote tool).

```jsonc
// claude_desktop_config.json
{
  "mcpServers": {
    "eraya-paid-tools": {
      "command": "/abs/path/to/brainwave/.venv/Scripts/python",
      "args": [
        "-m", "app.client", "proxy",
        "--url", "https://your-service.onrender.com/mcp/",
        "--session-budget", "$5.00",
        "--per-call-max", "$0.10",
        "--daily-budget", "$50.00",
        "--journal", "~/.eraya/spend.json",
        "--approve", "deny"
      ],
      "cwd": "/abs/path/to/brainwave",
      "env": {
        "AGENT_WALLET_KEY": "0x…",
        "X402_NETWORK": "eip155:84532"
      }
    }
  }
}
```

Notes that will save an hour:

* **`cwd` must be the repo root.** `-m app.client` resolves `app` from the working directory.
* **Use the venv's interpreter by absolute path.** Desktop hosts do not inherit your shell.
* **Logs go to stderr, always.** stdout is the JSON-RPC stream; one stray log line corrupts it.
  `main()` configures logging to stderr for exactly this reason.
* **`--approve ask` is useless here** — there is no tty. Leave it `deny` and set
  `escalate_above` above anything the agent should be able to buy unattended.
* **Use a Base Sepolia key.** Not a funded mainnet one. The signer refuses mainnet unless
  `--allow-mainnet` is passed.

---

## Testing

```bash
.venv/Scripts/python -m pytest tests/test_client.py -q
```

43 tests, no network, no server bound. The MCP session is a fake that answers with a real
x402 v2 payment-required body; everything downstream of it — requirement selection, EIP-712
domain construction, EIP-3009 signing — is the real SDK doing real work with a real throwaway
key.
