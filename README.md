# KI Ops

Deterministic pre-trade risk checks for rebalance trade intents.

```
trade_intents = target − SOD
```

The primary demo path is an LSEG long/short POC: start-of-day dollar notionals, signed share trade intents, Datastream2 CLOSE prices, YAML risk limits, and LSEG listing / ticker / ADV snapshots.

## Setup

Python 3.9+.

```bash
cd ~/ki-ops   # or /data/robert/repos/ki-ops on the AWS box
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

On the AWS box the shared venv is already at `/data/robert/venvs/ki-ops`.

## LSEG POC (main path)

Inputs are listed in [`config/poc_pos_and_px.yaml`](config/poc_pos_and_px.yaml). Filenames include the source table (or Datastream2) so stdout paths are identifiable.

| File | Role |
|------|------|
| `examples/sod_lseg_20260805.csv` | SOD dollar notionals (`infocode,ticker,notional`) |
| `examples/trade_intents_lseg_20260806.csv` | Signed whole-share trade qtys |
| `examples/lseg_datastream2_px_20260804.csv` | Datastream2 CLOSE 2026-08-04 |
| `examples/lseg_security_master_dt.csv` | `SECURITY_MASTER_DT` — tradability (`ISACTIVE`, `STATUSCODE`, `DELISTDATE`); current ticker is a label fallback only |
| `examples/lseg_ticker_mapping_dt.csv` | `TICKER_MAPPING_DT` — point-in-time INFOCODE ↔ ticker (`VALIDFROM`/`VALIDTO`). **Sample fixture** (recycled ticker `THRM`, rename `BSQR`/`BSQRD`), not a full table dump |
| `examples/lseg_base_data_us_dt_20260804.csv` | `BASE_DATA_US_DT` ADV snapshot (prefers `ADV20_ADJ`) |
| `config/risk_management_poc.yaml` | Risk limits (~$90M GMV book) |

`TICKER_MAPPING_DT`.`ISCURRENT` is “latest ticker interval,” not “still listed” (`ISACTIVE`). Tradability always comes from `SECURITY_MASTER_DT`. Ticker labels start from the master’s current ticker, then **overlay** PIT intervals as of `--as-of`.

POC manifest keys in [`config/poc_pos_and_px.yaml`](config/poc_pos_and_px.yaml):

```yaml
sod: examples/sod_lseg_20260805.csv
trades: examples/trade_intents_lseg_20260806.csv
prices: examples/lseg_datastream2_px_20260804.csv
security_master: examples/lseg_security_master_dt.csv   # tradability (SECURITY_MASTER_DT)
ticker_mapping: examples/lseg_ticker_mapping_dt.csv     # PIT labels (TICKER_MAPPING_DT)
adv: examples/lseg_base_data_us_dt_20260804.csv
```

Manifest vs stdout (and `input_hashes` keys):

| Manifest | Stdout path | Role |
|----------|-------------|------|
| `security_master` | `security_master` — `{path}: tradability_isactive` | May we trade? (`ISACTIVE`, `STATUSCODE`, `DELISTDATE`) |
| `ticker_mapping` | `ticker_mapping_dt` | INFOCODE ↔ ticker as of `--as-of` |

The old manifest key `ticker_map` is **not** supported; use `security_master`.

At run time, SOD `$` → shares via CLOSE (`qty = notional / px`). Order / turnover notionals use `abs(qty) × px`.

```bash
source .venv/bin/activate   # or /data/robert/venvs/ki-ops/bin/activate
cd /path/to/ki-ops

ki-ops run-perturb-baseline     # stdout perturb: "baseline"
ki-ops run-perturb-zero         # stdout perturb: "zero-turnover"; writes <trades>_zero.csv
ki-ops run-perturb-var-checks   # stdout perturb: "var-checks"; writes <trades>_var_checks.csv
```

Each command runs the **full** pre-trade gate on the same POC SOD + trade CSVs (`--as-of` 2026-08-06).

| Command | Book | Exit | `passed` | What fires |
|---------|------|------|----------|------------|
| `run-perturb-baseline` | live intents (~24% two-way TO) | 0 | `"with warnings"` | Drop delisted **56992** (`NOT_TRADABLE`); **335446** over 10% ADV (`MAX_ADV_PARTICIPATION`, warn) |
| `run-perturb-zero` | all trade qty → 0 | 0 | `"with warnings"` | Turnover 0; `MAX_POSITION_SIZE` warn on SOD **335446** |
| `run-perturb-var-checks` | scale trades above `max_turnover` (~26% vs 25% cap) | 2 | `false` | **Block `MAX_TURNOVER`**; same two warnings as baseline |

Stdout JSON has a top-level `perturb` (`"baseline"` / `"zero-turnover"` / `"var-checks"`), then `input` and `output` (blank line between them):

- `input`: `as_of`, `sod_source`, paths (`config`, `security_master`, `ticker_mapping_dt`, `adv`, `prices_csv`, `sod_csv`, `trade_intents_file`), hashes (`config_hash`, `input_hashes`), caps (`max_turnover`, `max_net_exposure`, `max_adv_participation`, `max_order_size`), `turnover_convention`
- `output`: SOD GMV / NMV / net exposure, `turnover`, `projected_portfolio_value`, `projected_net_exposure`, `passed` (`true` / `false` / `"with warnings"`), `violation_codes` / `warning_codes` plus full `violations` / `warnings`

Write the same JSON to disk with `--json-out FILE`.

Shared overrides: `--sod`, `--trades`, `--prices`, `--adv`, `--ticker-mapping`, `--poc-data`, `--config` (POC YAML, default `config/risk_management_poc.yaml`), `--as-of` (default `2026-08-06`), `--cash`, `--json-out`.

`run-perturb-var-checks` also takes `--target-turnover` (default `0.26`) and `--target-gmv` (default `90000000`).

There are no other perturb command names (no `run-perturb`, `run-perturb-turnover`, or `run-perturb-scaled`).

### Turnover convention (matches kelaisim)

Turnover is **two-way (gross)**, the same convention as kelaisim's `stats.py`:

```
turnover = (buy$ + sell$) / position GMV        # GMV = Σ|position MV|, cash excluded
```

**One-way turnover is exactly half of this** (a full book replace = 200% two-way
= 100% one-way). The YAML `max_turnover: 0.25` is a **two-way** cap, i.e.
≈ 12.5% one-way — tighter than a 25% one-way cap would be. The POC baseline
reads ~24% two-way and clears the cap.
Every JSON output carries a `turnover_convention` field stating the formula so
numbers are never compared across conventions by accident.

### GMV vs GMV + cash

Two explicit portfolio measures (`Portfolio.gmv` / `Portfolio.gmv_plus_cash`):

- `gmv` — Σ|position MV|, **cash excluded**. Denominator for `max_turnover`
  and `max_position_concentration` (matches kelaisim's positions-only GMV).
- `gmv_plus_cash` — deployed capital. Used by `max_portfolio_value` and
  reported as `projected_portfolio_value`.

### Risk notes (POC)

- `max_turnover` — two-way / position GMV; **blocks**
- `max_net_exposure` — projected `|NMV|/GMV`; **blocks** (POC cap 0.10)
- `max_portfolio_value` — projected GMV + cash; **blocks**
- `max_position_concentration` — |MV| / position GMV; **blocks**
- `max_position_size` — abs **share** qty (notional / px); **warning only**
- Tradability — `SECURITY_MASTER_DT` (`ISACTIVE`, `STATUSCODE`, `DELISTDATE`): live intents in inactive / delisted names are **dropped** and **warned**; the rest of the book still sends. Unknown infocodes **warn** (ticket kept).
- Ticker identity — `TICKER_MAPPING_DT` intervals vs `--as-of` (EMS extras ticker → INFOCODE; perturb labels).
- `max_adv_participation` — abs(order shares) / ADV; **warning only** for now. `0` disables (small-book default). Missing ADV **warns** (`MISSING_ADV`); ticket kept. POC cap 0.10.
- Order size min/max — off (`enforce_order_size_limits: false`)

Wolfe `h5` risk loadings are not wired yet.

### Decimal vs float

ki-ops does all money math in Python `Decimal` for deterministic, auditable
results (no float representation drift in a pre-trade gate). kelaisim and the
alpha parquets are `float`/numpy — values are converted once at the boundary
(`Decimal(str(float(v)))`), so tiny last-digit differences vs sim-reported
numbers are expected and harmless.

## Generic SOD / targets

Still supported for smaller example books (default `--config` is `config/risk_management_small_book.yaml`):

```bash
ki-ops run examples/sod_positions.csv examples/target_intents.csv
ki-ops derive-trades examples/sod_positions.csv examples/target_intents.csv
ki-ops check-rebalance examples/sod_positions.csv examples/target_intents.csv
```

```python
from ki_ops import PreTradeEngine, load_sod_positions_csv, load_target_intents_csv

sod = load_sod_positions_csv("examples/sod_positions.csv")
targets = load_target_intents_csv("examples/target_intents.csv")
result = PreTradeEngine.from_config_path("config/risk_management_small_book.yaml").evaluate_from_targets(
    sod, targets
)
print(result.to_dict())  # "passed", violations, warnings, turnover, …
```

Exit code `2` means a **blocking** check failed.

## Gate (kelaidata pipeline pre-trade gate)

`ki-ops gate` runs in the kelaidata `lseg_strategy_pipeline_combo` DAG right before `dollar_to_shares`. It validates the **neutralized dollar book** (`s3://kelaitrading/portfolio/dollar/<strategy_id>/Portfolio_<YYYYMMDD>.csv`, header `SecurityID,$_value`; `--env dev` uses `portfolio_dev`) and, when a converted shares file exists (or `--shares-file` is given), re-prices it with the ds2 H5 prior close and re-runs the same limits — this catches conversion bugs like a one-sided shares book built from a neutral dollar book.

```bash
ki-ops gate --strategy-id df_combo_..._neutralized --trade-date 2026-08-06 \
  [--env prod|dev] [--dollar-file PATH] [--prior-file PATH] [--shares-file PATH] \
  [--ds2 PATH] [--config PATH-or-s3://] [--json-out PATH-or-s3://]
```

Checks (limits from the risk YAML): GMV > 0, `|net|/GMV` vs `max_net_exposure`, per-name `|dollars|/GMV` vs `max_position_concentration`, two-way turnover vs the latest prior `Portfolio_*.csv` in the same folder vs `max_turnover` (prior missing → warning, check skipped). Shares side adds net/GMV, day-over-day churn, and a dropped-names count vs the dollar book.

`--config` accepts an `s3://` URI (fetched through the ETag cache like every other S3 input), so limits can change without a wheel release or Airflow redeploy. The SMA/IMA mandate limits live in [`config/risk_management_sma_ima.yaml`](config/risk_management_sma_ima.yaml) (repo source of record); the runtime copy the kelaidata gate task reads is `s3://kelaitrading/config/ki_ops/risk_management_sma_ima.yaml` — re-upload after changing the repo copy. `--config` defaults to the POC limits (`config/risk_management_poc.yaml`).

Exit codes (Airflow contract):

| Exit | Meaning | stdout |
|------|---------|--------|
| `0` | passed (`true` or `"with warnings"`) | full verdict JSON |
| `1` | infra error (missing input, S3 failure…) | `{"passed": false, "error": …, "error_type": "infra", "ki_ops_version": …}` |
| `2` | a blocking risk check failed | full verdict JSON with `violation_codes` |

The verdict JSON has two sections. `input` is what was read: files, hashes, and the limits that were applied (`max_net_exposure`, `max_position_concentration`, `max_turnover`, `max_adv_participation`). `output` is the result: `passed` (`true` / `false` / `"with warnings"`), `2-way turnover` (dollar turnover vs the prior book; `null` if no prior), `dollar` / `shares` metrics, `violation_codes` / `violations`, `warning_codes` / `warnings`, and `corp_action_check: "not_implemented"` (Snowflake DS2Adj corp-action check is a schema-reserved follow-up). Top-level fields are only `command` and `ki_ops_version`. `--json-out` writes the same payload to a local file or an `s3://` URI.

## Email + Slack notifications

`ki-ops extras notify-perturbs` runs all three perturb commands, captures their JSON stdout, and sends **one email** plus an optional **Slack** post. Settings load from `config/notify.env` (gitignored; copy [`config/notify.env.example`](config/notify.env.example)). Process env vars override the file.

### Email (Google Workspace SMTP)

```bash
./scripts/setup-notify-email.sh
# edit config/notify.env → KI_OPS_SMTP_PASSWORD=<Google App Password>
ki-ops extras notify-config
ki-ops extras notify-perturbs --test-email --skip-slack
ki-ops extras notify-perturbs --skip-slack    # full perturb JSON
```

Defaults: **From / To** `robert@kelaitech.com`, `smtp.gmail.com:587`. Use a [Google App Password](https://support.google.com/accounts/answer/185833), not your login password.

Without `config/notify.env`, macOS falls back to **Mail.app** (`robert@kelaitech.com` must be an account there; iCloud is not used).

### Slack (Incoming Webhook)

Add `KI_OPS_SLACK_WEBHOOK_URL` to `config/notify.env`. Webhooks post to **one channel**, not a DM — pick a channel you watch.

```bash
ki-ops extras notify-config                    # slack_webhook_set: true
ki-ops extras notify-perturbs --test-slack --skip-email
ki-ops extras notify-perturbs                  # email + Slack
```

Slack shows the subject plus JSON in a code block (truncated if very long; email has the full body).

[`dags/ki_ops_poc_perturbs.py`](dags/ki_ops_poc_perturbs.py) is an optional Airflow example (Airflow is not a package dependency). Cron is the same CLI entrypoint.

## KOTL (KelAI Order Tracking Ledger)

**Spec:** [`docs/working-order-book-spec.md`](docs/working-order-book-spec.md)

ki-ops is pre-trade (intents + gates). **KOTL** is the KelAI-side **sent / done / left** book for orders we submit to Flex:

| Question | Meaning |
|----------|---------|
| **Sent** | Qty submitted (`CreateOrders`) |
| **Done** | Qty filled (`GetOrderInfo2`) |
| **Left** | Signed `sent − done`; flat after 4:00pm ET (end of VWAP) |

**IDs:**

- **`submit_id`** — generated by KOTL (UUID) per submit call; groups one send in our store
- **`flex_order_id`** — from Flex create response; join key for refresh / fill updates

Store: CSV (`submits.csv`, `working_orders.csv`, default) **or MySQL**
(`--store mysql` on submit-kelai/refresh/status/eod; tables `kotl_submits`,
`kotl_working_orders`, `kotl_eod_snapshots`, idempotent DDL on first use;
creds from `KOTL_DB_*` env vars or `--db-secret dev/kelaidb --db-schema kelai`;
`pip install "ki-ops[db]"`, local dev via `docker-compose.kotl-db.yml`).

**Live Flex adapter** ([`kotl/flex_live.py`](src/ki_ops/kotl/flex_live.py),
`pip install "ki-ops[flex]"`): `--flex-env UAT|PROD` submits via
`OrderService.CreateOrders` (gRPC, bearer-token metadata); `--source live` on
refresh/eod polls `GetOrderInfo2`; `--sod-source flex` replays the position
book (`ReplayPositions`). Endpoint/token from `KOTL_FLEX_ENDPOINT` /
`KOTL_FLEX_TOKEN` else Secrets Manager `kelai/flextrade/api-token`; Brooklyn
SDK stays local-only via `KOTL_FLEX_SDK_PATH` (see
`docs/flextrade-connectivity-guide.md`). Flex samples under
[`vendor/flextrade/kelai_flex_sample_codes/`](vendor/flextrade/kelai_flex_sample_codes/).

**kelaidata S3 inputs:** `kotl submit-kelai` pulls the trade-dated shares file
(`s3://kelaitrading/portfolio/shares/[<strategy-id>/]Portfolio_<YYYYMMDD>.csv`
via `--strategy-id`, headerless `TICKER,shares,VWAP` from `dollar_to_shares`)
and prior-close prices + the point-in-time ticker map straight from the ds2 H5
(`s3://kelaidata/data/LSEG/Datastream2/ds2_data.h5`, read row-wise with h5py —
no CSV exports). SOD comes from `--sod-source {flex,prior-target,csv,flat}`
(legacy `--sod` CSV / `--assume-flat-sod` still map to csv/flat); with
`flex` the book is reconciled against yesterday's target file and the submit
aborts on divergence (**exit 4**, thresholds `--recon-max-shares` /
`--recon-max-names`, strict `0/0` defaults). Missing prices, duplicate
tickers, and fractional shares abort the submit. Safety rails on every
submit: **target mode** (below — the never-trade-past-the-target invariant),
`--dry-run` (build + print + trade file, no gRPC, no ledger write, no claim),
and `--max-orders` / `--max-gross-notional` caps — refusals **exit 5**. Each
submit prints and writes a **trade file** CSV (`--trade-file-out`, default
`s3://kelaitrading/trades/<strategy-id>/<yyyymmdd>/trades_<submit_id>.csv`
with `--strategy-id`, else `<data-dir>/trades/…`). Requires
`pip install "ki-ops[kelaidata]"` (h5py, numpy, boto3); S3 downloads are
ETag-cached under `data/kotl/cache/`.

**Target mode** ([`kotl/target_mode.py`](src/ki_ops/kotl/target_mode.py)) —
the portfolio file is a **target book**, and cumulative orders sent can never
exceed it, no matter how many times the pipeline runs (Airflow retry,
duplicate DAG run, manual CLI re-run). On every live submit the intended
delta is reduced by what was **already sent today** for `(trade_date, env)`:

- **everything sent** → clean no-op, **exit 0** (an accidental re-run is
  green and harmless; the JSON reports `target_covered`);
- **partial prior send** → only the residual top-up goes out;
- **overshoot** (a regenerated *lower* target) → clips to **zero** with a
  loud warning — KOTL never auto-generates a corrective/reverse order; the
  audit CSV (`target_mode_<submit_id>.csv`, next to the trade file) lists the
  exact excess per symbol for a manual unwind.

Already-sent comes from the ledger (accepted orders only; with
`--sod-source flex` each order's fills are subtracted since `ReplayPositions`
already reflects them) and is **cross-checked both ways** against live
`GetOrderInfo2` before any send — a ledger order missing from Flex, a
quantity mismatch, or a KOTL-stamped Flex order the ledger doesn't know (the
lost-ledger-write double-send scenario) refuses the submit (**exit 5**).
Recovery: `--sent-source flex` recomputes already-sent from Flex itself
(KOTL-stamped orders only) — the target cap still applies; no flag bypasses
it. Submission is also **claimed atomically once per day**: a
`kotl_submit_claims` row keyed `(trade_date, env)` is inserted immediately
before `CreateOrders` (MySQL PK — concurrent duplicate runs lose the race;
CSV store uses an `O_EXCL` claim file, single-host only). A second run with a
residual remaining refuses (**exit 5**) unless `--force`, and `--force` now
means "another **residual-capped** attempt" — it can never resend what
already went out. Dry-runs never claim and skip the live cross-check.

**Pre-submit security resolution** ([`kotl/flex_symbols.py`](src/ki_ops/kotl/flex_symbols.py)) —
FlexTrade's recommended workflow: on live envs every payload symbol is checked
through `SecurityService.BatchLookup` **before** `CreateOrders` and rewritten
to the canonical master symbol (undotted ds2 class shares map via the dotted
ticker alias: `BFB` → lookup `BF.B` → canonical `BF/B.US`). FlexTrade's
preferred identifier is the **SEDOL**, tried first: `--sedol-source`
(default `snowflake`, env `KOTL_SEDOL_SOURCE`) maps each book infocode to its
SEDOL through the **daily kelai security master**
`KELAI.LSEG.SECURITY_MASTER_DT` (canary: `KELAI.LSEG_CANARY`) built by the
kelaidata Airflow pipeline — connection via the same service-account
conventions ([`kotl/security_master.py`](src/ki_ops/kotl/security_master.py),
`pip install "ki-ops[secmaster]"`); pass a CSV path (`infocode,sedol`) for an
offline map or `none` for symbol-only. SEDOL hits catch ticker-change renames
the ds2 vocabulary misses (`FISV` → `FI.US`, printed with the company name for
review); an **exchange guard** rejects any resolution not ending in the
expected market suffix (`.US`) — live-verified necessity: some UAT master
records key placeholder instruments by the SEDOL string itself, and
dual-listed SEDOLs can point at the Canadian line (`CCJ` → `CCO.CN`) — those
fall back to the plain `TICKER.US` lookup instead of trading the wrong
listing. Resolutions are cached in `<data-dir>/flex_symbols_cache.json`; unresolved
names are re-checked every run. Names absent from the master **block the
submit** (**exit 6**) unless `--unresolved skip` (submit resolved names only);
either way `unresolved_<submit_id>.csv` is written next to the trade file —
that CSV is the list to send FlexTrade so they add the securities to the
master. Working orders store the canonical Flex symbol (what `GetOrderInfo2`
echoes back); ds2 tickers remain the pricing keys. Note: Flex ViewService
view type 5 is an alternative positions view to `ReplayPositions` for SOD —
noted for reference, the SOD source is unchanged.

```bash
ki-ops kotl submit-kelai --trade-date 2026-08-06 --sod sod.csv          # S3 defaults, FAKE adapter
ki-ops kotl submit-kelai --trade-date 2026-08-06 --assume-flat-sod \
  --shares local_20260806.csv --ds2 local_ds2_data.h5                   # local override
ki-ops kotl submit-kelai --trade-date 2026-08-06 --strategy-id USATop2000_neutralized \
  --sod-source flex --flex-env UAT --dry-run                            # live UAT rehearsal
ki-ops kotl submit-rebalance --trade-date 2026-08-06
ki-ops kotl refresh --trade-date 2026-08-06 --fixture examples/kotl/refresh_partial.json
# or kelai get_orders export (JSON/CSV, GetOrderInfo2 flatten shape):
ki-ops kotl refresh --trade-date 2026-08-06 --fixture examples/kotl/get_order_info2_sample.json
ki-ops kotl refresh --trade-date 2026-08-06 --source live --flex-env UAT # live fills
ki-ops kotl status --trade-date 2026-08-06
ki-ops kotl status --trade-date 2026-08-06 --json
ki-ops kotl eod --trade-date 2026-08-06 [--fixture PATH | --source live] [--tolerance N] [--json] [--notify] [--eod-dir PATH]
```

**End-of-day loop:** `kotl eod` refreshes fills (fixture/export, or live
GetOrderInfo2 with `--source live`), rebuilds the status report, checks flatness
(`sum(|leaves|) <= --tolerance`), and freezes an **immutable** snapshot under
`<data-dir>/eod/<trade_date>/` — `working_orders.csv`, `report.json`, and
`eod_fills_<date>.csv` (`symbol,side,filled_qty,avg_fill_px`, non-zero fills
only) for next-morning SOD recon. Stdout ends with a summary JSON (`flat`,
`open_count`, `cancelled_count`, `total_abs_leaves`, paths). Exit `0` when
flat, **exit `3` when not flat** (distinct from the pre-trade gate's `2` =
blocked). `--notify` emails/Slacks the summary via the same plumbing as
`extras notify-perturbs` (`config/notify.env`); a notify failure is reported
in the summary but never changes the exit code. With `--store mysql` the
summary is additionally persisted into `kotl_eod_snapshots`.

Offline demo uses fake submit + refresh fixtures under `examples/kotl/`:

- `refresh_partial.json` — simple `{trade_date, fills[]}` keyed by symbol
- `get_order_info2_sample.json` — kelai `get_orders` / Brooklyn enum ints (matches real UAT export shape)

`refresh` auto-detects fixture format. Data dir default: `data/kotl/` (gitignored).

## Branching and releasing (wheel for kelaidata / MWAA)

Branch flow: feature branches PR into **`uat`**, never into `main`. `main`
is protected (PRs only, no direct pushes, admins included) and the required
[`uat-gate`](.github/workflows/uat-gate.yml) check fails any PR into `main`
whose head is not `uat`, so the only path to `main` is a `uat -> main` PR.

Releases are automatic. Every merge to `main` runs the
[release workflow](.github/workflows/release.yml), which:

1. Derives the next tag by bumping the patch of the latest `v*` tag
   (`v0.2.0 -> v0.2.1`). For a **major/minor bump**, run the workflow
   manually (workflow_dispatch) with an explicit `version` input
   (e.g. `1.0.0`); later merges derive from that tag.
2. Runs the test suite, tags the merge commit, and builds the wheel at the
   tag. The version comes from the tag via setuptools-scm (`pyproject.toml`
   has no static version), and `setup.py` embeds the source commit into the
   wheel as `ki_ops.__git_sha__` (surfaced by `ki-ops --version`).
3. Fails unless the wheel is `py3-none-any` (the MWAA wheelhouse install is
   offline; a platform wheel would silently fail to resolve there) and
   unless the embedded sha matches the released commit.
4. Attaches `ki_ops-<version>-py3-none-any.whl` (+ sdist + `SHA256SUMS`) to
   a GitHub release.

Wheels built by hand from untagged commits get scm dev versions like
`0.2.1.dev3+g1a2b3c4` — self-identifying, and never a match for the exact
`ki-ops==X.Y.Z` pin kelaidata installs with.

ki-ops ships to the kelaidata Airflow environment as a pure-python wheel in
the MWAA wheelhouse (`plugins.zip`, installed offline with
`--find-links /usr/local/airflow/plugins --no-index`). It is never imported
as source by kelaidata — the DAG shells out to the pinned `ki-ops` CLI. On
the kelaidata side: `scripts/fetch_ki_ops_wheel.sh vX.Y.Z` downloads the
wheel from the GitHub release into its local `plugins/` wheelhouse, and
`ki-ops==X.Y.Z` is pinned in `requirements_airflow.txt`. Runtime
dependencies (`PyYAML`; `h5py`/`numpy`/`boto3` for the `[kelaidata]` extra)
already have wheels in that wheelhouse. `scripts/mwaa_release.py build` then
verifies every vendored ki-ops wheel against the SHA256SUMS of its GitHub
release before packaging — a wheel that is not a released tag cannot ship.

## Layout

```
docs/
  working-order-book-spec.md   # KOTL v1 spec (sent / done / left)
  kotl-spec-notion-import.md   # same spec, Notion import copy
config/
  poc_pos_and_px.yaml          # LSEG POC manifest (sod, trades, prices, security_master, …)
  notify.env.example           # email/Slack template → copy to notify.env (gitignored)
  risk_management_poc.yaml     # POC / production-scale limits (~$90M GMV)
  risk_management_small_book.yaml  # small example-book limits
src/ki_ops/
  engine.py, checks/, intents.py, portfolio.py, models.py, config.py
  alpha.py, poc_data.py, cli.py, listing.py, audit.py
  extras/                      # EMS, fills, risk snapshot, email/Slack perturb job
  kotl/                        # submit_id, flex_order_id, sent/done/left (see docs/)
examples/
  sod_lseg_*.csv, trade_intents_lseg_*.csv
  lseg_security_master_dt.csv, lseg_ticker_mapping_dt.csv
  lseg_base_data_us_dt_*.csv, lseg_datastream2_px_*.csv
  sod_positions.csv, target_intents.csv
  extras/                      # EMS Portfolio CSV, small security master
dags/                          # example Airflow DAG (copy into AIRFLOW_HOME/dags)
tests/
```

Sidecars (not the LSEG perturb path): `ki-ops extras check-ems`, `ki-ops extras summarize-trades`, `ki-ops extras risk-snapshot`, `ki-ops extras notify-perturbs`, `ki-ops extras notify-config`, and `ki-ops poc-alpha` (dollar-panel parquet).
