# Demo script

The video, shot by shot, timestamped. **Target run time 3:00.** Hard ceiling 3:30.

The single job of this video is to make one claim land and be believed:

> **Every agent tool call is metered and settled with real x402 — and the ledger proves it,
> down to the transaction hash.**

Everything that does not serve that claim is cut, including things this project is proud of.

---

## Contents

- [Constraints](#constraints)
- [Pre-flight checklist](#pre-flight-checklist)
- [Shot list](#shot-list)
- [Full script](#full-script)
- [The money shot](#the-money-shot)
- [Fallback plans](#fallback-plans)
- [Recording setup](#recording-setup)
- [Post-production](#post-production)
- [What not to do](#what-not-to-do)
- [Self-check before upload](#self-check-before-upload)

---

## Constraints

| | |
|---|---|
| **Run time** | 3:00 target, 3:30 absolute maximum |
| **Audio** | Voiceover, recorded separately from the screen capture. Do not narrate live |
| **Resolution** | 1920×1080, 30fps |
| **Assumed knowledge** | The judge knows what MCP is and what x402 is. **Do not explain either.** Spending 30 seconds defining MCP is 17% of the video spent telling an expert something they know |
| **Everything on screen is real** | No mockups, no fixtures, no "imagine if". If it is not working, it is not in the video |

---

## Pre-flight checklist

Do all of this **before** pressing record. Every item is something that has ruined a take.

**System state**

- [ ] Service running against **Postgres**, not SQLite — the demo shows a ledger that survives
- [ ] `curl -s $BASE/healthz` returns **200** and `"mcp_session_manager": "started"`.
      **If this says `NOT STARTED`, stop.** Every paid call will 500 — see
      [`ARCHITECTURE.md`](ARCHITECTURE.md#the-lifespan-problem)
- [ ] `PUBLIC_BASE_URL` matches the real host, or every MCP request 421s
- [ ] Agent wallet funded with **Base Sepolia test USDC**, and enough for the whole session plus
      two retakes
- [ ] Channel deposit ≥ `BATCH_MAX_CALLS × max_price`, so the session cannot exhaust mid-take
- [ ] `BATCH_WINDOW_SECONDS` set **short** (30–60s) for recording. The default 300 means three
      minutes of silence waiting for a close. Set it back afterwards
- [ ] Ledger seeded with a few hundred prior calls so the dashboard has a real shape. An empty
      chart looks like a prototype
- [ ] A **second** browser tab pre-opened on `/admin` — logged in already, so no password is
      typed on camera

**Screen hygiene**

- [ ] No `.env` open anywhere. No private keys, no CDP secrets, no `ADMIN_PASSWORD` in scrollback
- [ ] Terminal scrollback **cleared**
- [ ] Notifications off — Slack, mail, calendar, OS updates
- [ ] Browser: no bookmarks bar, no unrelated tabs, no extension badges
- [ ] Terminal font ≥ 16pt. A judge may watch this on a laptop at 50% scale

**Content**

- [ ] Every number about to be spoken re-checked against the ledger **that morning**
- [ ] The Basescan link opens and shows a real transaction
- [ ] Run the whole thing once end to end, untimed, without recording

---

## Shot list

| # | Time | Duration | Shot | On screen | Proves |
|---|---|---|---|---|---|
| 1 | 0:00 | 0:12 | The problem | Title card → an MCP server's README with no pricing | There is no payment layer |
| 2 | 0:12 | 0:20 | The integration | `app/catalogue.py`, one decorator | Adoption cost is one line |
| 3 | 0:32 | 0:33 | The agent pays | Split: agent terminal / live call feed | The four protocol steps, real |
| 4 | 1:05 | 0:25 | `upto` capture | Receipt JSON: authorized ≠ captured | Honest metering |
| 5 | 1:30 | 0:20 | The Guardian declines | Over-budget call refused pre-signature | Spend policy is real |
| 6 | 1:50 | 0:35 | **Batch close → chain** | Batch closes, Basescan opens | **Real settlement** |
| 7 | 2:25 | 0:25 | The economics | Dashboard fee-load comparison | It is a business |
| 8 | 2:50 | 0:10 | Close | Title card, repo URL | Where to look |

---

## Full script

Timings are cumulative. **Narration** is what is spoken; **screen** is what is captured.

---

### Shot 1 · 0:00–0:12 · The problem

**Screen.** Cold open on a title card, 2 seconds, on the brand background `#1d0718`:

```
TRAPPIST × BRAINWAVE
MCP won the tool layer. This is its payment layer.
```

Cut to a real public MCP server's README, scrolling slowly past its tool list. Then cut to its
`package.json` or `pyproject.toml` — nothing about money anywhere.

**Narration.**

> "Thousands of MCP servers. Every one of them free — not because the authors want that, but
> because there is no way to charge. And an agent can't sign up for anything anyway. No email,
> no card, no 'I agree'."

**Note.** 12 seconds on the problem, no more. The judge already believes it.

---

### Shot 2 · 0:12–0:32 · The integration

**Screen.** `app/catalogue.py` in an editor, syntax-highlighted, large font. Scroll to a real
paid tool and let it sit still:

```python
@paid(price="$0.002", scheme="upto", max_price="$0.05", meter="tokens")
async def run_injection_attack_sim(payload: str, rounds: int = 3) -> dict:
    """Adversarial prompt-injection simulation. Cost scales with rounds."""
```

Highlight `scheme="upto"` for a beat.

**Narration.**

> "This is the whole seller-side integration. One decorator. Price, scheme, ceiling, and what
> meters it.
>
> `upto` matters. This tool's cost depends on how many rounds it runs — a fixed price is either
> a loss on the big inputs or a rip-off on the small ones. The agent authorizes a ceiling, and
> we capture only what it actually consumed.
>
> And to be exact about what's ours: the x402 SDK already implements paid MCP. This decorator is
> a thin wrapper over its `create_payment_wrapper`. What we built is everything after the
> payment — the metering, the ledger, the batching, and the receipts."

**Note.** That last paragraph is **non-negotiable**. Twelve words of honesty buys credibility for
the remaining two and a half minutes, and a judge who knows the SDK is checking for exactly this.

---

### Shot 3 · 0:32–1:05 · The agent pays

**Screen.** Split screen, held for the whole shot.

*Left* — a terminal running the buyer, printing the protocol trace as it happens:

```
→  tools/call run_injection_attack_sim
←  payment required   scheme=upto  max=$0.05  payTo=0xAUT…  nonce=0x8f…
✎  signed EIP-3009 authorization             (pure Python, no gas, no raw tx)
→  retry   _meta["x402/payment"] = eyJzY2hlbWU…
✓  facilitator verify → valid
⚙  tool executed                             1,842 tokens
←  200 OK   captured $0.007400 of $0.050000 authorized
🧾 receipt rcpt_01HZY…  session sess_01HZY…  (settles at batch close)
```

*Right* — the NiceGUI dashboard's live call feed, rows appearing in real time.

**Narration.**

> "Here's an agent calling it. Challenge. Sign. Retry. Settle — all four x402 steps, none of them
> stubbed.
>
> Two details worth catching. The agent signs an authorization, never a transaction — no gas, no
> chain selection, and it's signed in pure Python; there's no JavaScript anywhere in this
> project.
>
> And over MCP the payment rides in the JSON-RPC `_meta` key `x402/payment` — **not** an
> `X-PAYMENT` header. The header belongs to the plain-HTTP paywall on the same server. We run
> both, so we're precise about which is which."

**Note.** Let the trace scroll at real speed. Do not speed it up — an agent paying for a tool in
under a second is the demo, and accelerating it makes a judge suspect a fixture.

---

### Shot 4 · 1:05–1:30 · `upto` capture

**Screen.** Zoom into the returned receipt JSON. Highlight `authorized` and `captured` on
consecutive beats, then `meterUnits`, then `bodyHash`.

```jsonc
"scheme":     "upto",
"authorized": "0.050000",
"captured":   "0.007400",
"meter":      "tokens",
"meterUnits": 1842,
"session":    "sess_01HZY…",
"batchId":    "batch_01HZY…",
"settleTxHash": null,
"bodyHash":   "sha256:…"
```

**Narration.**

> "The receipt shows both numbers. Fifty thousandths authorized, seven thousandths captured,
> against eighteen hundred and forty-two tokens actually consumed.
>
> Showing both is the difference between a payment system and a black box. And capture can never
> exceed authorization — that's not a code convention, it's a CHECK constraint in the database.
> A metering bug is an error, not an overcharge.
>
> The transaction hash is null, because this hasn't settled yet. That's next."

---

### Shot 5 · 1:30–1:50 · The Guardian declines

**Screen.** Terminal, buyer side. Configure a tight budget, then attempt a call that breaches it:

```
guardian: session_budget=$0.05  per_call_max=$0.01
→  tools/call expensive_tool   (price $0.05)
✗  DECLINED  reason=per_call_max   $0.050000 > $0.010000
   no authorization was signed
```

Cut to `/admin`, filtered on `decline_reason`, showing the declined row in the ledger.

**Narration.**

> "An agent with a wallet and no ceiling is an unbounded liability, and nobody ships that.
>
> The Guardian evaluates policy before anything is signed. A payment that was never signed can't
> be settled — which is exactly why this check belongs to the buyer, not the seller.
>
> The SDK doesn't have this. It has a hook policy, but that guards hook mutations, not spend.
> This one's ours. And the decline is still written to the ledger, because you want the funnel,
> not just the wins."

---

### Shot 6 · 1:50–2:25 · Batch close → chain · **THE MONEY SHOT**

**Screen.** Three beats, no cuts away.

*Beat 1 (0:08)* — the dashboard's batch panel. `call_count` climbing, `gross_atomic` climbing,
`status: open`. Then the window elapses: `open → claiming → claimed → settling → settled`.

*Beat 2 (0:12)* — the batch row, both hashes populated:

```
batch_01HZY…   calls 100   gross $0.200000
claim_tx   0x…      ✓
settle_tx  0x…      ✓
facilitator_fee $0.002000     fee load 1.00%
```

*Beat 3 (0:15)* — click through to **Basescan**. Real transaction, real USDC transfer, real
timestamp. Let it sit on screen for a full three seconds without narration.

**Narration.**

> "One hundred calls just closed into one settlement.
>
> Two hashes, not one — `batch-settlement` is a payment channel, and closing it is a claim then a
> sweep. We store both, because a batch whose claim landed and whose sweep didn't is a real state
> you have to be able to see.
>
> And that's Basescan. Real USDC, moved on Base, for a hundred agent tool calls that each cost a
> fifth of a cent."

*(silence over Beat 3)*

**Note.** This is the shot the submission lives or dies on. If it does not work, see
[fallback plans](#fallback-plans) — but there is no version of this video where the chain is
faked.

---

### Shot 7 · 2:25–2:50 · The economics

**Screen.** The dashboard's fee-load comparison, side by side, computed live from the ledger.

```
per-call settlement          batched (N=100)
fee load        50.0%        fee load         1.0%
platform margin −$0.0008     platform margin +$0.018
```

Then briefly a terminal running the counterfactual SQL from
[`ECONOMICS.md`](ECONOMICS.md#check-it-yourself) over the same rows.

**Narration.**

> "Here's why the batching isn't a nice-to-have.
>
> A tenth-of-a-cent settlement fee against a fifth-of-a-cent call. Settle every call and the
> platform spends five times what it earns — per-call settlement has a hard price floor of one
> cent, and it prices the entire micro-tool category out of existence.
>
> Batched, the fee amortises. Break-even is ten calls. At a hundred it's one percent, and at a
> million calls a month the facilitator bill goes from nine hundred and ninety-nine dollars to
> nineteen.
>
> Every one of those numbers comes out of the ledger. None of them is typed into a slide."

---

### Shot 8 · 2:50–3:00 · Close

**Screen.** Title card on `#1d0718`:

```
TRAPPIST × BRAINWAVE
Paid MCP · x402 on Base

github.com/martian3062/brainwave
<live URL>
```

**Narration.**

> "One Python service. Paid MCP, a revenue ledger, and a dashboard — no Node, no npm, no
> TypeScript anywhere.
>
> MCP won the tool layer. This is its payment layer."

---

## The money shot

If only one thing survives editing, it is **Shot 6**, and specifically this chain of custody
visible on screen inside twelve seconds:

```
tool call → receipt (session + batch id) → batch row (gross) → tx hash → Basescan
```

That is the entire submission. A judge who sees a receipt tie to a batch, the batch tie to a
transaction, and the transaction resolve on a public explorer has verified the claim themselves.
Everything else in the video is context for that one chain.

Do not cover it with narration. Do not speed it up. Do not cut away from Basescan before three
full seconds have passed.

---

## Fallback plans

Have these decided **before** recording day, not during it.

| If this fails | Do this | Do **not** do this |
|---|---|---|
| Settlement won't land on Base Sepolia | Record everything else. Add an on-screen caption at Shot 6: *"Settlement pending — testnet facilitator unavailable at recording"*, and say so in the voiceover | Fake a hash. Show a screenshot of somebody else's transaction. Say "settled" over a `null` |
| Batching not wired in time | Show per-call settlement working, then present the batching model as measured projection at Shot 7 — clearly labelled **"projected"** | Let a projection sound like a measurement |
| Guardian not wired | Cut Shot 5 entirely, give the 20s to Shot 6 | Show a decline that is a hardcoded print |
| Facilitator down mid-take | Reschedule | Stub the facilitator and keep the word "real" in the narration |
| Nothing on-chain works at all | Retitle the video honestly and lead with the ledger and the economics, stating up front that settlement is not yet live | Imply otherwise for even one sentence |

**The rule:** a demo that under-claims and is true beats a demo that over-claims and gets caught.
Judges test the links.

---

## Recording setup

**Screen capture**

| | |
|---|---|
| Resolution | 1920×1080, 30fps |
| Terminal | ≥ 16pt monospace, high contrast, scrollback cleared |
| Browser | Full screen, no bookmarks bar, no extensions visible, no other tabs |
| Editor | Large font, minimap off, sidebar collapsed, one file open |
| Cursor | Highlight enabled; keystroke overlay **off** — it is noise |
| Zoom | Editor and JSON at ~150%. Assume a small screen |

**Palette for title cards** — the brand values, so the video matches the product:

```
background   #1d0718
foreground   #fbf4f2
accent       #ff6f91
accent-deep  #e6416f
cream        #fff3ec
```

**Audio**

- Record the voiceover **separately**, after the screen capture is locked. Narrating live
  produces stumbles and forces retakes of working footage.
- Quiet room, pop filter if available, one take per shot rather than one take for the video.
- Speak roughly 15% slower than feels natural. Technical density plus speed loses people.
- No background music under the narration. Optional low bed under Shots 1 and 8 only.

**Order of work**

1. Rehearse end to end, untimed, not recording.
2. Capture screen only, shot by shot, several takes each.
3. Assemble the picture edit to the shot list.
4. Write the final narration **to the assembled picture** — timings will have drifted.
5. Record voiceover.
6. Mix, caption, export.

---

## Post-production

- [ ] **Captions burned in.** Many judges watch muted first
- [ ] On-screen labels for every number spoken aloud — a viewer should not have to trust their
      ears for `$0.007400`
- [ ] Highlight boxes on: `scheme="upto"`, `authorized` vs `captured`, `decline_reason`, both tx
      hashes, the fee-load pair
- [ ] Basescan URL legible at 720p
- [ ] Final pass with audio **off** — the video must still make its argument
- [ ] Final pass at 50% window size — this is how it gets watched
- [ ] Export ≤ 3:30. If over, cut Shot 5 first, then trim Shot 1 to 8 seconds. **Never cut
      Shot 6**

---

## What not to do

| Don't | Why |
|---|---|
| Explain what MCP is | The judge knows. It costs 8% of the video |
| Explain what x402 is | Same |
| Show the architecture diagram | Impressive in a README, dead on video. It is in `ARCHITECTURE.md` |
| Narrate the lifespan bug | Genuinely the best engineering in the project, and completely wrong for a 3-minute demo |
| Show a code walkthrough beyond Shot 2 | Judges read code in the repo, not in a video |
| Say "as you can see" | Say what they should see |
| Show a spinner for more than 2 seconds | Cut it |
| Use the word "seamlessly" | |
| Claim `create_payment_wrapper` as ours | Instantly disqualifying to anyone who knows the SDK |
| Show a `.env`, a key, or a password | |
| Speed-ramp the payment trace | Makes a real thing look like a fixture |

---

## Self-check before upload

Answer all five out loud. If any answer is no, the video is not ready.

1. Does a judge see a **real transaction on a public explorer**, tied to a specific batch, tied
   to specific tool calls?
2. Is it unambiguous which parts are **the SDK's** and which are **ours**?
3. Is the **economic argument** stated as a measured result rather than an assertion?
4. Would every claim in the narration survive someone opening the repository and checking?
5. Is it under **3:30**?

---

*See also:* [`ARCHITECTURE.md`](ARCHITECTURE.md) · [`ECONOMICS.md`](ECONOMICS.md) ·
[`SPEC_CONFORMANCE.md`](SPEC_CONFORMANCE.md) · [`../README.md`](../README.md)
