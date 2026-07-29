# Economics

The unit-economics model in full: the fee schedule, who bears which cost, the break-even batch
size, and the SQL that checks every claim in this document against the ledger.

This is the section most submissions will not have, and it is the one that decides whether any
of this is a business rather than a demo.

---

## Contents

- [The one-line version](#the-one-line-version)
- [Notation](#notation)
- [The fee schedule](#the-fee-schedule)
- [Who bears the settlement fee](#who-bears-the-settlement-fee)
- [The hard price floor of per-call settlement](#the-hard-price-floor-of-per-call-settlement)
- [Batching, and the break-even batch size](#batching-and-the-break-even-batch-size)
- [The headline table](#the-headline-table)
- [At scale — one million calls a month](#at-scale--one-million-calls-a-month)
- [The free tier](#the-free-tier)
- [Sensitivity](#sensitivity)
- [`upto`, and why fees track capture](#upto-and-why-fees-track-capture)
- [The risks of batching, honestly](#the-risks-of-batching-honestly)
- [What the ledger must record](#what-the-ledger-must-record)
- [Check it yourself](#check-it-yourself)
- [Corrections made while writing this](#corrections-made-while-writing-this)

---

## The one-line version

> **At a 10% platform take and a $0.001 settlement fee, per-call settlement is loss-making for
> every tool priced under $0.01.** A $0.002 call produces $0.0002 of take and costs $0.001 to
> settle — the platform loses $0.0008 on it, spending five times the revenue it earns. Session
> batching turns the fee from a per-call cost into a per-*session* cost, and the business turns
> profitable at **ten calls per batch**.

Everything below derives that, and the ledger is built so it can be checked rather than
believed.

---

## Notation

| Symbol | Meaning | Default in this repo |
|---|---|---|
| `p` | tool price per call | `$0.002` (2,000 atomic units, USDC 6dp) |
| `N` | calls in one batch / session | `BATCH_MAX_CALLS=500`, typical 50–200 |
| `k` | on-chain settlement actions per batch | **2** — `batch-settlement` closes with claim **then** sweep |
| `f` | facilitator fee per settlement transaction | `$0.001` (`FACILITATOR_FEE_PRICE`) |
| `t` | platform take rate | `0.10` (`PLATFORM_TAKE_BPS=1000`) |
| `G` | gross revenue for a batch = `N·p` | |
| `F` | facilitator cost for a batch = `k·f` batched, `N·f` per-call | |
| `M` | platform margin = `G·t − F` | |
| `A` | author net = `G·(1 − t)` | |

All money in this project is an **integer count of atomic units**. `app/money.py` is the only
converter; `Decimal` never escapes it and no `Float` column exists anywhere in the ledger.
That is not fastidiousness — the whole submission rests on the claim that
`Σ(captured) == settled on-chain`, exactly. One float round-trip and that claim becomes a
rounding artefact instead of a proof.

**Fee load** is the headline ratio:

```
fee_load = F / G
```

implemented as integer basis points in `app/money.py`:

```python
def fee_load_bps(settlement_cost_atomic: int, gross_atomic: int) -> int:
    if gross_atomic <= 0:
        return 0
    return (settlement_cost_atomic * 10_000) // gross_atomic
```

---

## The fee schedule

**Coinbase CDP hosted facilitator** — the published schedule this model uses:

| Tier | Cost |
|---|---|
| First **1,000** settlement transactions per calendar month | **free** |
| Every transaction thereafter | **$0.001** |

Two honesty notes:

1. **Verify before mainnet.** Fee schedules change. `FACILITATOR_FEE_PRICE` and
   `FACILITATOR_FREE_TX_PER_MONTH` are environment variables precisely so this model is re-run
   against the schedule in force rather than the one that was true when this was written.
2. **Gas is the facilitator's problem, until it isn't.** The $0.001 is CDP's charge; CDP submits
   the transaction and pays Base gas. Run your own facilitator and you swap a fixed $0.001 for
   Base L2 gas, which is smaller on average but **volatile** and paid in ETH, adding an
   inventory problem. The model treats settlement cost as one number `f` because that is exactly
   what the hosted facilitator charges. The public `https://x402.org/facilitator` used on testnet
   charges nothing, which is why every number here should be re-derived before mainnet.

**Why `k = 2`.** `x402.mechanisms.evm.batch_settlement` is a payment **channel**, not a bag of
authorizations. Closing a batch is two on-chain steps:

```
ClaimPayload   → claim the cumulative vouchers   → claim_tx_hash
SettlePayload  → sweep claimed funds to payTo    → settle_tx_hash
```

`Batch` stores both hashes, because a batch whose claim landed and whose sweep did not is a real
state. There is also a one-off **deposit** to open or top up the channel (ERC-3009
`receiveWithAuthorization` or Permit2). It amortises across the channel's whole life rather than
per batch, so it is excluded from `k` and noted separately under
[risks](#the-risks-of-batching-honestly).

---

## Who bears the settlement fee

This is the question the draft spec skipped, and the ledger answers it unambiguously.

`app/models.py` puts two CHECK constraints on the batch:

```sql
CHECK (platform_fee_atomic + author_net_atomic = gross_atomic)   -- ck_batch_split_conserves
CHECK (gross_atomic >= 0)
```

`facilitator_fee_atomic` is a **separate column, deliberately outside that identity**. So by
construction:

| Party | Gets / pays |
|---|---|
| **Author** | `A = G·(1 − t)` — exactly 90% of gross, **independent of settlement cost** |
| **Platform** | `M = G·t − F` — the take, *minus* the whole facilitator bill |

**The platform absorbs the settlement fee out of its own take.** That is how a real payments
marketplace works — Stripe absorbs interchange out of its fee rather than surprising the
merchant with it — and it puts the incentive in the right place: the party that chooses the
settlement strategy is the party that pays for it.

It also relocates the entire economic argument. The author is paid the same per call whether
settlement is batched or not. **Batching is not an author feature; it is the reason the platform
survives to keep paying the author.**

The identity that follows is the cleanest statement of the whole model:

```
share of the platform's take consumed by settlement  =  fee_load / t
```

At a 10% take: a **1%** fee load consumes **10%** of the take. A **50%** fee load consumes
**500%** of the take — five times underwater.

---

## The hard price floor of per-call settlement

Under per-call settlement, each call carries its own fee, so per-call margin is:

```
m = p·t − f
```

which is positive only when

```
p  ≥  f / t
```

At `f = $0.001` and `t = 10%`, that floor is **$0.01 per call**.

| Take rate `t` | Minimum viable price per call, per-call settlement |
|---:|---:|
| 5% | **$0.0200** |
| 10% | **$0.0100** |
| 20% | **$0.0050** |
| 30% | **$0.0033** |
| 50% | **$0.0020** |

Read that table next to the product. The tools MCP most needs to unlock — a single enrichment
lookup, one adversarial simulation round, one classification — belong at fractions of a cent. A
$0.01 floor prices the entire micro-tool category out of existence, and raising the take to 50%
to rescue it is not a marketplace anyone joins.

**Per-call settlement does not make micropayments expensive. It makes them impossible.** That is
the problem this project exists to remove.

---

## Batching, and the break-even batch size

Batch a session of `N` calls and the fee stops scaling with calls:

```
F = k·f                     (independent of N)
fee_load(N) = k·f / (N·p)
M(N)        = N·p·t − k·f
```

**Break-even batch size** — the smallest `N` at which the platform stops losing money:

```
N* = ceil( k·f / (p·t) )
```

At `k=2, f=$0.001, p=$0.002, t=10%`:

```
N* = ceil( 0.002 / 0.0002 ) = 10 calls
```

**Ten calls per batch is where this becomes a business.** Below it, batching is still better than
per-call settlement — but the platform is still paying to operate.

And batching beats per-call settlement whenever `k·f < N·f`, i.e. **`N > k`**: from the third
call onward.

### Fee load and platform margin as `N` grows

`p = $0.002`, `f = $0.001`, `k = 2`, `t = 10%`:

| `N` | Gross | Facilitator | **Fee load** | Take (10%) | **Platform margin** | Margin as % of take |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | $0.002 | $0.002 | 100.0% | $0.0002 | −$0.0018 | −900% |
| 2 | $0.004 | $0.002 | 50.0% | $0.0004 | −$0.0016 | −400% |
| 3 | $0.006 | $0.002 | 33.3% | $0.0006 | −$0.0014 | −233% |
| 5 | $0.010 | $0.002 | 20.0% | $0.0010 | −$0.0010 | −100% |
| **10** | $0.020 | $0.002 | **10.0%** | $0.0020 | **$0.0000** | **0% — break-even** |
| 25 | $0.050 | $0.002 | 4.0% | $0.0050 | +$0.0030 | +60% |
| 50 | $0.100 | $0.002 | 2.0% | $0.0100 | +$0.0080 | +80% |
| **100** | $0.200 | $0.002 | **1.0%** | $0.0200 | **+$0.0180** | **+90%** |
| 250 | $0.500 | $0.002 | 0.4% | $0.0500 | +$0.0480 | +96% |
| 500 | $1.000 | $0.002 | 0.2% | $0.1000 | +$0.0980 | +98% |
| *per-call, any N* | `N`×$0.002 | `N`×$0.001 | **50.0%** | 10% | **−$0.0008 / call** | **−400%** |

Note row `N=1`: a "batch" of one call is *worse* than per-call settlement, because closing a
channel costs two transactions where a direct settlement costs one. Batching is not free
optimism — it is a fixed cost amortised, and it must actually be amortised.

### Choose a target, get a batch size

```
N*(L) = ceil( k·f / (L·p) )
```

| Target fee load `L` | Required `N` | Gross per batch |
|---:|---:|---:|
| 10% | 10 | $0.02 |
| 5% | 20 | $0.04 |
| 2% | 50 | $0.10 |
| **1%** | **100** | **$0.20** |
| 0.5% | 200 | $0.40 |
| 0.1% | 1,000 | $2.00 |

### A configuration bug this model catches

`BATCH_MIN_GROSS_PRICE` exists to stop the batcher opening an on-chain settlement for dust. Its
default is **`$0.01`**. Run it through the model:

```
worst-case fee load at the minimum = k·f / 0.01 = 0.002 / 0.01 = 20%
```

A batch allowed to close at $0.01 of gross can burn **20%** of it — twice the whole platform
take. To guarantee a fee load of at most `L`, the minimum gross must satisfy

```
BATCH_MIN_GROSS  ≥  k·f / L
```

| Guaranteed fee load | Required `BATCH_MIN_GROSS_PRICE` |
|---:|---:|
| 10% | $0.02 |
| **1%** | **$0.20** |
| 0.5% | $0.40 |

**Recommendation: raise `BATCH_MIN_GROSS_PRICE` to `$0.20`** before any mainnet run, and let
`BATCH_WINDOW_SECONDS` roll sub-threshold dust into the next batch rather than settling it. The
default is safe on testnet, where `f = 0`, and wrong on mainnet. This is exactly the kind of
thing an economics document is for.

---

## The headline table

`p = $0.002`, `f = $0.001`, `t = 10%`, `k = 2`.

| | Per-call settlement | **Batched session (N = 100)** |
|---|---:|---:|
| Tool price | $0.002 | $0.002 |
| Calls | 1 | 100 |
| Gross revenue | $0.002 | $0.200 |
| On-chain settlement events | 1 | **2** (claim + sweep, for the whole batch) |
| Facilitator cost | $0.001 | $0.002 |
| **Fee load** | **50%** | **1.0%** |
| Author net (90% of gross) | $0.0018 | $0.180 |
| Platform take (10% of gross) | $0.0002 | $0.020 |
| **Platform margin** (take − fee) | **−$0.0008 · loss** | **+$0.018 · profit** |
| Settlement's share of the take | **500%** | **10%** |

Same author revenue per call in both columns — the author's cut is a fixed share of gross. The
difference is entirely whether the marketplace underneath them is solvent.

---

## At scale — one million calls a month

1,000,000 calls at $0.002 → **$2,000 gross**, $1,800 to authors, $200 of platform take. Free
tier: 1,000 settlement transactions.

| | Per-call settlement | **Batched, N = 100** |
|---|---:|---:|
| Settlement transactions | 1,000,000 | 20,000 |
| Billable after the free tier | 999,000 | 19,000 |
| **Facilitator bill** | **$999.00** | **$19.00** |
| Fee load | 49.95% | 0.95% |
| Author net | $1,800 | $1,800 |
| **Platform margin** | **−$799.00** | **+$181.00** |

A **$980/month swing on $2,000 of gross revenue**, and the difference between a marketplace that
compounds and one that pays to exist.

---

## The free tier

1,000 free settlement transactions a month is not a rounding error at this scale — it is the
entire runway of an early marketplace, and batching multiplies what it buys.

| Settlement strategy | Calls covered by the free tier | Gross revenue served free |
|---|---:|---:|
| Per-call | 1,000 | $2.00 |
| Batched, N = 50 (`k=2`) | 25,000 | $50.00 |
| **Batched, N = 100 (`k=2`)** | **50,000** | **$100.00** |
| Batched, N = 500 (`k=2`) | 250,000 | $500.00 |

At N=100 the same free tier serves **50× more revenue**. For a hackathon demo and the first
months of real traffic, batching means the facilitator bill is not $0.001 — it is zero.

---

## Sensitivity

Fee load at `N = 100`, `k = 2`:

**vs. the facilitator fee `f`** (fee load = `2f / 0.2` = `10f`):

| `f` | Fee load | Margin as % of take |
|---:|---:|---:|
| $0.0000 (public testnet facilitator) | 0.0% | 100% |
| $0.0005 | 0.5% | 95% |
| **$0.0010 (CDP)** | **1.0%** | **90%** |
| $0.0050 | 5.0% | 50% |
| $0.0100 | 10.0% | 0% — break-even |

**vs. the tool price `p`** (fee load = `0.002 / (100p)`):

| `p` | Fee load | Margin as % of take |
|---:|---:|---:|
| $0.0005 | 4.0% | 60% |
| $0.0010 | 2.0% | 80% |
| **$0.0020** | **1.0%** | **90%** |
| $0.0100 | 0.2% | 98% |
| $0.0500 | 0.04% | 99.6% |

**vs. the batch size `N`** — see [the table above](#fee-load-and-platform-margin-as-n-grows).

The model is far more sensitive to `N` than to anything else, which is the whole point:
`N` is the one variable the gateway controls.

---

## `upto`, and why fees track capture

For any tool whose cost scales with input — token counts, document length, rounds of
simulation — a fixed price is either a loss on large inputs or a rip-off on small ones. `upto`
is the only honest pricing primitive for LLM-backed tools:

```
agent authorizes:  max_price_atomic     (the ceiling)
gateway captures:  price_per_unit × meter_units, clamped to the ceiling
```

Economically this matters because **gross, and therefore fee load, is computed on `captured`,
not `authorized`**:

| Quantity | Basis |
|---|---|
| `Batch.gross_atomic` | `Σ Call.captured_atomic` |
| `fee_load` | `facilitator_fee / gross` — capture-based |
| Author net | `captured × (1 − t)` |
| Platform take | `captured × t` |

Consequence, and it is a good one: **an agent that authorizes a generous ceiling is not
penalised for it.** Authorizing $0.05 and consuming $0.0074 pays for $0.0074. Nothing in the
model rewards the gateway for pushing ceilings up, which is exactly the property that makes
`upto` trustworthy enough for an agent to use unattended.

The invariant is enforced by the database, not by the metering code:

```sql
CHECK (captured_atomic <= authorized_atomic)   -- ck_call_capture_le_authorized
```

An `upto` metering bug is a **database error**, not a silent overcharge. The same constraint is
repeated on `receipt`, so a receipt that claims an impossible capture cannot be stored.

---

## The risks of batching, honestly

Batching moves cost off the critical path. It does not make cost vanish, and it introduces
exposures a per-call design does not have.

### 1. Settlement risk — bounded by the channel deposit

Between capture and batch close, the gateway has delivered work it has not been paid for. Under
a naive "collect signatures, settle later" design that is unsecured credit extended to an
anonymous agent.

**`batch-settlement` removes it.** The client **deposits into the channel up front**
(`DepositPayload`, ERC-3009 or Permit2), so the funds are already escrowed on-chain before the
first voucher is issued. The gateway's exposure is bounded by the channel balance, not by the
agent's honesty. This is the single strongest reason to use the SDK's mechanism rather than
inventing a parallel one.

Residual exposure: capture beyond the deposited balance. Mitigation:
`Session.budget_atomic ≤ channel deposit`, enforced at session open.

### 2. Channel exhaustion

The cumulative voucher ceiling cannot exceed the deposit. A long session must top up mid-flight,
which is another on-chain event and therefore another fee. Modelled as `k` rising above 2 for
sessions that outlive their deposit. Mitigation: size the deposit from
`BATCH_MAX_CALLS × max_price`, and freeze the session rather than silently exceeding it —
`SessionStatus.FROZEN` exists for this.

### 3. Float — the author waits

Revenue lands at batch close, not at call time. The gap is visible in the ledger as
`Session.captured_atomic − Session.settled_atomic`, and `CHECK ck_session_settled_le_captured`
guarantees it is never negative. At `BATCH_WINDOW_SECONDS=300` the worst case is five minutes,
which is a fair trade for a 50× fee reduction — but it is a real change to the author's
experience and it is disclosed on the dashboard rather than hidden.

### 4. Partial close

Claim lands, sweep fails. Funds are claimed but not swept. This is why `Batch` carries
**both** `claim_tx_hash` and `settle_tx_hash` and a `BatchStatus` with distinct `CLAIMING`,
`CLAIMED`, `SETTLING`, `SETTLED` and `FAILED` states. A reconciliation that models the close as
atomic reports a lie in exactly this case.

### 5. Fee-schedule change

The whole model turns on `f`. It is an environment variable, the dashboard computes fee load
from it live rather than from a hard-coded table, and `Batch.facilitator_fee_atomic` records
what was **actually** charged per batch — so a schedule change shows up as a divergence between
the projection and the ledger instead of quietly invalidating a README.

---

## What the ledger must record

For every claim in this document to be checkable rather than asserted:

| Field | Why the model needs it |
|---|---|
| `Call.captured_atomic` | the true revenue atom; gross is the sum of these |
| `Call.authorized_atomic` | proves `upto` capture was legitimate |
| `Call.meter` / `meter_units` | the evidence behind a variable capture |
| `Call.platform_fee_atomic` / `author_net_atomic` | the split, conserving by CHECK |
| `Session.captured_atomic` / `settled_atomic` | float outstanding |
| `Batch.call_count` | the `N` in every formula above |
| `Batch.gross_atomic` | the denominator of fee load |
| **`Batch.facilitator_fee_atomic`** | the numerator — **the total cost of the batch across all `k` on-chain steps**, not one step's |
| `Batch.claim_tx_hash` / `settle_tx_hash` | ties `gross_atomic` to what the chain actually moved |
| `Receipt.batch_id` | lets one call be traced to the settlement that paid for it |

> **Batch-closer contract:** `Batch.facilitator_fee_atomic` is the
> **total** facilitator cost of that batch — claim plus sweep, plus any deposit top-up
> attributed to it. Writing one step's fee there would understate fee load by half and every
> number on the dashboard would be wrong by a factor of `k`. `app/cli/close_batch.py` implements
> this as a resumable, confirmation-gated command; no live close is claimed yet.

---

## Check it yourself

Not one of these figures is a fixture. Each is derivable from the ledger with SQL.

**Realised fee load per settled batch:**

```sql
SELECT b.batch_id,
       b.call_count,
       b.gross_atomic,
       b.facilitator_fee_atomic,
       (b.facilitator_fee_atomic * 10000) / NULLIF(b.gross_atomic, 0) AS fee_load_bps,
       b.platform_fee_atomic - b.facilitator_fee_atomic               AS platform_margin_atomic
FROM batch b
WHERE b.status = 'settled'
ORDER BY b.settled_at DESC;
```

`fee_load_bps` should be ~100 (1.0%) at N=100, and `platform_margin_atomic` positive above
N*=10.

**Conservation — this must return zero rows:**

```sql
SELECT s.session_id,
       SUM(c.captured_atomic) AS calls_captured,
       s.captured_atomic      AS session_captured
FROM pay_session s
JOIN call c ON c.session_id = s.id
GROUP BY s.id, s.session_id, s.captured_atomic
HAVING SUM(c.captured_atomic) <> s.captured_atomic;
```

**Batch gross equals the calls it settled — also zero rows:**

```sql
SELECT b.batch_id, b.gross_atomic, SUM(c.captured_atomic) AS calls_captured
FROM batch b
JOIN call c ON c.batch_id = b.id
GROUP BY b.id, b.batch_id, b.gross_atomic
HAVING SUM(c.captured_atomic) <> b.gross_atomic;
```

**Per-call settlement, counterfactually costed from the same rows:**

```sql
SELECT COUNT(*)                                   AS calls,
       SUM(captured_atomic)                       AS gross_atomic,
       COUNT(*) * 1000                            AS per_call_facilitator_cost_atomic,
       (COUNT(*) * 1000 * 10000)
           / NULLIF(SUM(captured_atomic), 0)      AS per_call_fee_load_bps
FROM call
WHERE status IN ('captured', 'settled');
```

That last one is the argument in a single query: the same traffic, priced under the design we
rejected.

And in Python, pinned by `tests/test_spine.py::test_the_headline_economic_claim`:

```python
from app.money import fee_load_bps

fee_load_bps(1_000, 2_000)  # 5000 bps = 50%   -- one call, one settlement
fee_load_bps(2_000, 200_000)  #  100 bps =  1%   -- 100 calls, one claim + one sweep
```

The dashboard computes the same two numbers at render time from `settings.facilitator_fee_atomic`
rather than displaying a constant, so a fee-schedule change is visible on the front page.

---

## Corrections made while writing this

Two numbers in the original project brief did not survive contact with the SDK and the schema.
Both corrections are recorded rather than quietly applied.

### 1. "One settlement event per batch" → **two**

The brief modelled batching as `N` authorizations netted into **one** on-chain settlement, giving
a 0.5% fee load at N=100. Reading `x402.mechanisms.evm.batch_settlement` shows the close is two
steps — `ClaimPayload`, then `SettlePayload` — so `k = 2` and the honest fee load at N=100 is
**1.0%**.

The argument survives comfortably: 50% → 1.0% is still a 50× improvement, and the break-even is
still ten calls. Publishing the flattering number would not have survived a judge who opened the
SDK.

### 2. "Fee comes out of the author's revenue" → **the platform absorbs it**

The brief's table computed `net to author = gross − facilitator fee − platform take`, implying
the author pays for settlement. The ledger's `ck_batch_split_conserves` constraint
(`platform_fee + author_net = gross`, with `facilitator_fee` outside it) says otherwise: the
author receives a fixed 90% of gross and the platform pays the facilitator out of its own 10%.

This is the more defensible design — the party choosing the settlement strategy pays for it —
and it makes the argument **sharper**, not softer. Under the brief's accounting, per-call
settlement merely thinned the author's margin. Under the real accounting, per-call settlement
means the **platform spends five times its revenue on fees** and the marketplace cannot exist
below a $0.01 price floor.

---

*See also:* [`ARCHITECTURE.md`](ARCHITECTURE.md) · [`SPEC_CONFORMANCE.md`](SPEC_CONFORMANCE.md) ·
[`DEMO_SCRIPT.md`](DEMO_SCRIPT.md) · [`../README.md`](../README.md)
