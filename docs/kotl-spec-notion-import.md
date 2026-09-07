# KelAI Working Order Tracking Ledger (KOTL) - Spec (v1)

> **Status:** draft for review (no implementation yet)
> **Source in repo:** `docs/working-order-book-spec.md`

## 1. Goal

Give KelAI a clear answer to three questions for what we send to trade:

1. **Sent** — how much did we submit?
2. **Done** — how much has filled?
3. **Left** — how much is still outstanding (`sent − done`, adjusted for cancels)?

This is the **KelAI Order Tracking Ledger (KOTL)** — KelAI-side sent / done / left tracking — not a replacement for Flex or the SMA’s OMS.

---

## 2. Context

| System | Role |
|--------|------|
| **ki-ops** | Pre-trade: intents, limits, tradability, perturb checks |
| **Flex OMS** | Execution / street routing for the SMA path (system of record for fills) |
| **SMA OMS** | Client-side OMS (out of scope for v1) |
| **This project** | Record what KelAI sent and track fill progress vs remaining |

ki-ops answers “what *should* we trade?”  
KOTL answers “what *did* we send, and are we finished?”

---

## 3. Non-goals (v1)

- Cancel / replace / rebalance from KelAI (observe only)
- Position / SOD book of record (later phase)
- Real-time P&L product

---

## 4. Primary user questions (acceptance framing)

For a given **trade date** and **fund / strategy** (e.g. `KELAI` / `USATop2000_strategy_v1`):

| Question | Example answer shape |
|----------|----------------------|
| What did we send today? | Per-line: symbol, side, sent qty, Flex `orderId` |
| How much is done? | filled qty, avg fill px (if available) |
| What’s left? | leaves qty; list of still-open / partial lines |
| Are we flat on the ticket set? | sum(leaves) == 0 (or within tolerance) after 4:00pm ET (end of VWAP) |

---

## 5. Domain model

### 5.1 Concepts

```
Intent (v1.1+)           Submission                 Working order (ledger)
ki-ops trade line   →    CreateOrders          →    match on flex_order_id
                         persist Flex orderIds      sent / filled / leaves
```

**v1 identity (locked):** primary key for fill refresh is Flex parent **`orderId`**, saved from the create response into our store. Refresh matches `GetOrderInfo2` rows by that id.

### 5.2 Working order (core row)

One row per **Flex parent order** (not per street child), unless we later need street-level detail.

| Field | Description | Source | v1 |
|-------|-------------|--------|----|
| `trade_date` | Business date | Flex `tradeDate` / submit date | required |
| `flex_order_id` | Parent order id | Create response + Flex `orderId` | **required (join key)** |
| `flex_batch_id` | Flex-side batch (if returned) | Flex `batchId` | optional |
| `submit_id` | Our send attempt | Ledger | required |
| `symbol` | e.g. `DASH.US` | Send + Flex | required |
| `side` | BUY / SELL | Send + Flex | required |
| `fund` / `position_group` | Account / strategy book | Send payload | required |
| `sent_qty` | Submitted qty (**signed**) | Flex `quantity` × side sign | required |
| `filled_qty` | Cumulative filled (**signed**) | Flex `filledQuantity` × side sign | required |
| `leaves_qty` | Outstanding (**signed**) | Derived: see §6 | required |
| `avg_fill_px` | VWAP of fills | Flex `weightedAvgPrice` | optional |
| `status` | open / partial / done / cancelled | Derived from Flex `status` + qtys | required |
| `broker` / `algo` / `order_type` | Execution context | Send + Flex | optional |
| `last_seen_at` | Last successful poll | Ledger | required |
| `client_batch_id` | Our batch label on Flex | Flex `clientBatchIdentifier` if we set it | **not required for v1** |
| `intent_id` | Link to ki-ops line | Our stamp on send | **v1.1+** |

Street orders (`get_orders_detailed`) are **supporting detail**, not the primary v1 grain.

### 5.3 Submit event (audit)

Immutable record of each send attempt:

| Field | Description |
|-------|-------------|
| `submit_id` | Our UUID (local grouping for “this send”) |
| `submitted_at` | Timestamp |
| `env` | UAT / PROD |
| `payload` | Order dicts sent (or hash + pointer) |
| `flex_order_ids` | Ids returned by create (critical) |
| `flex_response` | Raw / summarized response for debug |
| `ok` | Success / partial / fail |

Local `submit_id` groups lines we sent together. That lives in **our** store; it does not require writing a batch stamp into Flex for v1.

### 5.4 Intent link (v1.1+)

| Field | Description |
|-------|-------------|
| `intent_id` | Stable id for ki-ops trade line |
| `symbol`, `side`, `target_qty` | From ki-ops |
| `submit_id` / `flex_order_id` | How the intent was sent |

Without this, v1 still answers sent / done / left for recorded Flex orders. It cannot answer “vs what ki-ops intended.”

---

## 6. Quantity rules

**Sign convention (locked):** quantities are **signed** — BUY positive, SELL/SHORT negative. Keep Flex `side` as well for clarity. Convert unsigned Flex `quantity` / `filledQuantity` using side when ingesting.

```
sign        = +1 if BUY else -1
sent_qty    = sign * abs(parent order quantity)
filled_qty  = sign * abs(parent filledQuantity)
leaves_qty  = sent_qty - filled_qty   # same sign as sent; 0 when done
# remaining work in shares: abs(leaves_qty)
```

Status derivation (use magnitudes; refine after UAT samples):

| Condition | Status |
|-----------|--------|
| `abs(filled_qty) == 0` and not cancelled | `open` |
| `0 < abs(filled_qty) < abs(sent_qty)` | `partial` |
| `abs(filled_qty) >= abs(sent_qty)` | `done` (`leaves_qty = 0`) |
| Flex status indicates cancel / dead and leaves unused | `cancelled` (`leaves_qty = 0` for “remaining work”) |

**Tolerance:** optional absolute share epsilon (e.g. 0) for “fully done.” Confirm with live Flex statuses before coding edge cases (replaces, restages, busts = out of v1).

End-of-VWAP flat check: `sum(abs(leaves_qty)) == 0` (or within tolerance) after **4:00pm ET**.

---

## 7. Flex interface mapping

Grounded in existing KelAI helpers + Brooklyn `OrderService`.

| Ledger need | Flex API | Existing helper / sample |
|-------------|----------|---------------------------|
| Submit | `CreateOrders` (`sendToEms=True`) | `kelai_sender.send_orders` |
| Refresh orders for date | `GetOrderInfo2` | `kelai_flex_utils.get_orders` / `get_orders_detailed` |
| Live updates (later) | `Subscribe` orders | Brooklyn `subscribe_orders.py` |
| Positions (not v1) | `ReplayPositions` | `get_positions` |
| Fill tape (optional) | `ListTradeActivity` | Brooklyn `list_trade_activity_order.py` |

**v1 sync mode:** poll `GetOrderInfo2` for `trade_date` on an interval or on demand; **update only rows whose `flex_order_id` we already stored**.  
**Not v1:** streaming subscribe (later phase).

### Identity (v1 locked)

```
CreateOrders → save each Flex orderId → refresh matches by orderId → update filled/leaves
```

No Flex-side client stamp required for that loop.

### Optional later: `clientBatchIdentifier`

Flex exposes `clientBatchIdentifier` on create/query (and cancel-by-client-batch). Kelai `get_orders*` already *reads* it; `kelai_sender` does not *set* it today.

Useful later for: naming a whole send in Flex UI, recovering orders if create ids were not saved, cancel-by-batch. **Out of scope for v1** unless a Phase 0 spike shows create responses are unreliable and we need a rediscovery path.

---

## 8. System flow (v1)

```
[ki-ops trade list] ── optional intent link in v1.1 ──
        │
        ▼
[Submit adapter]  CreateOrders → submit record + working_orders keyed by flex_order_id
        │
        ▼
[Poller]  GetOrderInfo2(trade_date) → match flex_order_id → filled / leaves / status
        │
        ▼
[Views]  sent | done | left  (per order + totals)
```

Notify can reuse existing email/Slack patterns from ki-ops extras (summary after 4:00pm ET / end of VWAP: open leaves).

---

## 9. Phasing

### Phase 0 — Spike (hours, not a product)

- Point kelai scripts at local Brooklyn `python3/API`
- UAT: `get_orders(today)`, `get_positions`
- Inspect create response: confirm `orderId` is returned per line (v1 depends on this)
- Capture real `status` / `quantity` / `filledQuantity` samples  
**Exit:** sample of create response + order query rows showing join-by-`orderId` works  
**Optional:** try setting `clientBatchIdentifier` and see if it round-trips (not a v1 gate)

### Phase 1 — KOTL v1

- Persist submit records + working orders keyed by `flex_order_id` (CSV/Parquet)
- Poll/refresh fills for a trade date (match stored ids only)
- Report: sent / done / left (+ open list)
- **CLI-triggered** submit / refresh / status (no scheduler in v1)

### Phase 2 — Intent linkage + end-of-VWAP alerts

- Link `intent_id` on send; optional Flex `clientBatchIdentifier` if useful
- Recon: intended vs sent vs filled
- Slack/email when leaves remain after 4:00pm ET (end of VWAP)

### Phase 3 — Near-real-time + richer lifecycle

- Subscribe to order updates
- Cancel/replace awareness; street-level fill detail if needed
- Position/SOD recon (separate “position ledger” spec)

---

## 10. Storage (decided)

**v1: Option C — append-only CSV / Parquet** under a data dir (not necessarily `examples/`; exact path TBD, gitignored as needed). Dead simple; open in pandas; weak concurrency is acceptable for CLI-only refresh.

Normalized tables (files), e.g.:

- `submits.csv` / `.parquet`
- `working_orders.csv` / `.parquet` (keyed by `flex_order_id`)

Optional raw JSON blob alongside for debug. No Brooklyn binaries in storage. Snowflake later is a lift-and-shift of the same columns if needed.

---

## 11. Repo / package sketch (for later; not building yet)

**Decided:** lives in the **ki-ops** repo as `ki_ops` package code (e.g. `src/ki_ops/kotl/` or `src/ki_ops/ledger/`).

```
src/ki_ops/kotl/             # KelAI Order Tracking Ledger
  models.py                 # WorkingOrder, Submit
  flex_adapter.py           # wrap kelai_sender / get_orders
  store.py                  # CSV / Parquet
  refresh.py                # poll + upsert
  report.py                 # sent/done/left
vendor/flextrade/
  kelai_flex_sample_codes/  # tracked
  brooklyn-…/               # gitignored
```

**Trigger (decided):** CLI first — e.g. `ki-ops kotl submit|refresh|status` (names TBD). Airflow/cron later if useful.

---

## 12. Acceptance tests (v1)

1. After a known UAT send of qty N, ledger shows `sent_qty = N` and Flex `orderId` stored.
2. After partial fill F, refresh shows `filled_qty = F`, `leaves_qty = N − F`, status `partial`.
3. After full fill, `leaves_qty = 0`, status `done`.
4. Status report for trade date lists only in-scope fund/strategy (filter rule documented).
5. Re-running refresh is idempotent (same order id → upsert, no duplicate rows).

---

## 13. Decisions

1. ~~**Stamp field**~~ — **decided:** join on Flex `orderId` from create; `clientBatchIdentifier` optional later (not a v1 gate).
2. ~~**v1 store**~~ — **decided:** Option C — append-only CSV / Parquet.
3. **Scope filter (still open):** fund `KELAI` only? which `positionGroup`s? (refresh still only updates *stored* order ids; filter matters for any “scan Flex” views)
4. **Intent link (still open):** v1 or v1.1? (spec default: **v1.1**)
5. ~~**Package home**~~ — **decided:** in the **ki-ops** repo (`ki_ops.kotl` / similar).
6. ~~**Who triggers refresh**~~ — **decided:** CLI first; scheduler later if needed.
7. ~~**Signed quantities**~~ — **decided:** store signed (BUY +, SELL −).

---

## 14. References

- KelAI Flex helpers: `vendor/flextrade/kelai_flex_sample_codes/`
- Brooklyn OrderService RPCs (local): `vendor/flextrade/brooklyn-12.57.0/python3/resources/Orders.proto`
- API field browser (local, gitignored): `vendor/flextrade/APIDocumentation.html`
- Upstream pre-trade: ki-ops intents / EMS / perturb notify
