# kotl resend — operator runbook

`ki-ops kotl resend` re-runs the day's submit pipeline **scoped to specific
tickers**, for a day that already had its main submit. Two flows:

1. **Retry unresolved securities** — the main submit ran with
   `--unresolved skip` (the standing pipeline mode), skipped the names
   missing from the Flex security master, and wrote them to
   `unresolved_<submit_id>.csv` next to the trade file. Once FlexTrade seeds
   the master, resend exactly those names.
2. **Re-send one ticker** — an order was cancelled in Flex (confirmed
   terminal) and its dead remainder should go out again, or a create-rejected
   name (e.g. missing Beta/ADV analytics on a freshly seeded security) is
   ready for another attempt after a cancel.

It is `submit-kelai` with `--force` implied and a ticker scope applied
*before* symbol resolution and target mode — so every safety rail (market
hours, SOD recon, pre-submit resolution, ledger/Flex cross-check, residual
cap, order/notional caps) still runs on the scoped set, and nothing outside
the scope can ever be sent. Because force always computes already-sent from
live `GetOrderInfo2` state, working and unfinalized (Flex "rejected") orders
stay fully protected; only never-sent quantity and confirmed-CANCELLED
remainders are eligible.

## Environment defaults (operator box)

With `KI_OPS_ENV=canary` exported (the operator-box boot service in
kelai-infra sets this, plus `KOTL_FLEX_SDK_PATH` and the Flex UAT proxy
endpoint), the env-aware defaults kick in and commands stay short:

| Setting | Default under `KI_OPS_ENV=canary` | Override |
| --- | --- | --- |
| Ledger (live envs) | MySQL via secret `kelai/kotl/db-canary`, schema `kotl` | `--store` / `--db-secret` / `--db-schema` |
| Shares trade file | `s3://kelaitrading/portfolio_canary/shares/[<strategy-id>/]Portfolio_<yyyymmdd>.csv` | `--shares` |
| Trade file + sidecars (with `--retry-unresolved`) | same folder as the retry CSV | `--trade-file-out` |
| Unresolved mode | `skip` (a still-missing name never blocks the rest of the scope) | `--unresolved block` |
| Force | always on (resend implies it) | — |

`--flex-env` is never defaulted for submits: say `UAT` (or `PROD`)
explicitly. `FAKE` (the default) keeps everything offline, including a
csv/`--data-dir` ledger. Without `KI_OPS_ENV` set, the legacy prod shares
root applies and a live submit still defaults to the canary preset ledger
(`KI_OPS_ENV` merely *selects* the preset; canary is the base default).

## Retry the unresolved report (minimal)

```bash
S=<strategy-id>   # e.g. df_combo_lseg_..._neutralized

ki-ops kotl resend --trade-date 2026-09-16 --flex-env UAT --sod-source flex \
  --strategy-id $S \
  --retry-unresolved s3://kelaitrading/trades/canary/$S/unresolved_<submit_id>.csv
```

That resolves to: canary MySQL ledger, canary shares book for `$S`, trade
file `trades_<new_submit_id>.csv` written **next to the retry CSV**, and —
if any name is *still* missing from the master — a fresh
`unresolved_<new_submit_id>.csv` in the same folder, ready to chain into the
next retry. Tickers that dropped out of today's target only warn; if *every*
scoped name is still unresolved, nothing is sent (exit 6).

Rehearse first by appending `--dry-run` (no gRPC, no ledger write, no claim;
the trade file gets a `_dryrun` suffix).

## Re-send one ticker (minimal)

```bash
ki-ops kotl resend --trade-date 2026-09-16 --flex-env UAT --sod-source flex \
  --strategy-id $S --ticker PVLA
```

`--ticker` is strict: a name with no residual trade intent refuses the
submit (exit 5) — typo protection, and "target already met" is an explicit
verdict, never a silent no-op. A fully-working scoped name is a clean
covered no-op. Note: without `--retry-unresolved` the trade file follows the
`--strategy-id` S3 template (`s3://kelaitrading/trades/<strategy-id>/…`);
pass `--trade-file-out` if the day's artifacts live elsewhere (e.g. the
canary pipeline's `trades/canary/<strategy-id>/` folder).

## Recon on a scoped resend: only your tickers' rows block

The SOD recon guard compares the live Flex book against the overnight book
snapshot (or, with no snapshot in the ledger, yesterday's target file). On a
**scoped resend** the full-book diff is still printed — it is the
systemic-health signal (wrong account scope, symbol-map regression, a
Flex-side book reload) and one name with a huge unexplained move is a real
alarm — but the **block decision applies only to the scoped tickers' rows**:

- **Retry-unresolved**: the scoped names were never sent, so their book rows
  should not have moved — the run passes the strict `0/0` default clean,
  while today's fills on the rest of the book print as
  `informational only`. No recon flags needed.
- **`--ticker` after a cancel**: the scoped name's own fills show as its
  divergence, so acknowledge exactly that movement, e.g. a 20-share fill →
  `--recon-max-shares 20 --recon-max-names 1`. The thresholds are an
  operator acknowledgment, not a send cap — the send stays bounded by
  target mode to the scoped names' residuals regardless.

An **unscoped** `submit-kelai`, by contrast, blocks on the whole book: the
day's first submit runs pre-fills and should pass at `0/0` against the
nightly snapshot — a breach there means real book drift; investigate before
overriding.

## Airflow (kelaidata pipeline)

For a blocked **pipeline** submit, don't set recon env vars — use the
one-shot, date-pinned approval Variable (Airflow UI → Admin → Variables),
read at task runtime; then Clear the failed task:

```json
KOTL_SUBMIT_APPROVAL = {"trade_date": "2026-09-16",
                        "recon_max_shares": 50000, "recon_max_names": 2500}
```

`trade_date` is mandatory and must match the run — a stale Variable is
ignored, so it can never silently relax a later night's guards. Add
`"force": true` when re-running after the day's claim is taken. (Schema and
semantics: `KOTL_SUBMIT_APPROVAL` in kelaidata's
`dags/lseg_strategy_pipeline_dag.py`.)

## After the resend

- `ki-ops kotl fills --ticker TRAX` — live-state view of the resent names
  (env-aware, canary MySQL by default; add `--live` for a Flex overlay).
- `ki-ops kotl refresh --trade-date … --source live --flex-env UAT --store
  mysql --trade-file-out s3://…/trades_<submit_id>.csv` — sync fills and
  workflow statuses into the ledger and back-fill the trade file's
  `filled_qty` / `final_status` columns.
- A create-rejected order (gateway `success=false`, e.g. "Missing Beta") is
  booked in Flex as **unfinalized** — it can still be worked later, so
  target mode keeps protecting its quantity. The retry flow is: cancel it in
  Flex, confirm the cancel, then `resend --ticker <name>`.
- **Exposure-calc warnings vs true rejections**: FlexTrade's pre-trade
  exposure rule runs an aggregate calculation over the whole basket plus
  existing positions/open orders; securities with missing analytics inputs
  (price, delta, volume, Beta, …) emit *calc warnings* — e.g. `Calc failed
  for 13.9187% (298/2141) of securities`, `Missing Beta for security X`,
  `Missing ExposurePrice`, `invalid Avg Volume (90D) = NaN` — and the
  account's **0.00% error tolerance** turns any warning into an order
  reject. These are data-availability hiccups (typically pre-market, before
  the day's analytics load), not compliance verdicts: the same order
  usually goes through on an intraday resend (verified 2026-09-15: the
  06:57 ET submit had all 878 shorts calc-rejected; the 13:13 ET forced
  resend sent 872 of them). The tooling separates the two: the submit table
  and verdict summary mark `rejected (calc-warning)`, `kotl fills --json`
  carries `rejection_kind` (`calc_warning` / `rejection`), and the fills
  table summary counts `create_rejected=N (calc_warnings=M,
  true_rejections=K)`. Chase persistent calc warnings with FlexTrade (their
  "exception report email" has the per-security detail), not by resending
  blindly.

## Exit codes

Same as `submit-kelai`: `0` sent (or clean covered no-op), `2` usage (resend
needs `--ticker` and/or `--retry-unresolved`), `4` recon divergence,
`5` refused (caps, cross-check, or a scoped ticker with no residual),
`6` unresolved securities (all scoped names still missing), `7` market
closed.
