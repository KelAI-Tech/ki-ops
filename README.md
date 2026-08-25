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

Manifest vs stdout path keys:

| Manifest (`poc_pos_and_px.yaml`) | Stdout JSON |
|----------------------------------|-------------|
| `ticker_map` (security master) | `security_master` — printed as `{path}: tradability_isactive`. `input_hashes` still hash the real file path |
| `ticker_mapping` | `ticker_mapping_dt` |

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

Stdout JSON includes:

- `perturb`: `"baseline"` / `"zero-turnover"` / `"var-checks"`
- `as_of`, `sod_source`
- Paths: `config`, `security_master` (annotated), `ticker_mapping_dt`, `adv`, `prices_csv`, `sod_csv`, `trade_intents_file` (input trades for baseline; `<trades>_zero.csv` or `<trades>_var_checks.csv` for the other two)
- Hashes: `config_hash`, `input_hashes`
- Caps echoed: `max_turnover`, `max_net_exposure`, `max_adv_participation`, `max_order_size`
- Book: SOD GMV / NMV / net exposure, `turnover`, `projected_portfolio_value`, `projected_net_exposure`
- Gate: `passed` (`true` / `false` / `"with warnings"`)
- Findings: `violation_codes` / `warning_codes` (unique index) plus full `violations` / `warnings`

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

## Email + Slack job (Airflow-style)

`ki-ops extras notify-perturbs` runs the three perturb commands, captures JSON stdout, and **emails** it. Slack is optional (only if `KI_OPS_SLACK_WEBHOOK_URL` is set). Airflow is not required; [`dags/ki_ops_poc_perturbs.py`](dags/ki_ops_poc_perturbs.py) is an example DAG if you add Airflow later.

On macOS, Mail.app is used when SMTP is unset. **From** is `robert@kelaitech.com` (not iCloud). That address must exist under Mail → Settings → Accounts. If only iCloud is added, send is refused.

```bash
# one-line ping — From robert@kelaitech.com
ki-ops extras notify-perturbs --test-email --to robert@kelaitech.com --from-addr robert@kelaitech.com

# full perturb JSON to email (Slack skipped unless a webhook is set)
ki-ops extras notify-perturbs --to robert@kelaitech.com
```

SMTP (servers / Linux) instead of Mail.app:

```bash
export KI_OPS_SMTP_HOST=smtp.gmail.com
export KI_OPS_SMTP_PORT=587
export KI_OPS_SMTP_USER=...
export KI_OPS_SMTP_PASSWORD=...
export KI_OPS_SMTP_FROM=robert@kelaitech.com
export KI_OPS_EMAIL_TO=robert@kelaitech.com
ki-ops extras notify-perturbs --test-email
```

## Layout

```
config/
  poc_pos_and_px.yaml          # LSEG POC file manifest
  risk_management_poc.yaml     # POC / production-scale limits (~$90M GMV)
  risk_management_small_book.yaml  # small example-book limits
src/ki_ops/
  engine.py, checks/, intents.py, portfolio.py, models.py, config.py
  alpha.py, poc_data.py, cli.py, listing.py, audit.py
  extras/                      # EMS, fills, risk snapshot, email/Slack perturb job
examples/
  sod_lseg_*.csv, trade_intents_lseg_*.csv
  lseg_security_master_dt.csv, lseg_ticker_mapping_dt.csv
  lseg_base_data_us_dt_*.csv, lseg_datastream2_px_*.csv
  sod_positions.csv, target_intents.csv
  extras/                      # EMS Portfolio CSV, small security master
dags/                          # example Airflow DAG (copy into AIRFLOW_HOME/dags)
tests/
```

Sidecars (not the LSEG perturb path): `ki-ops extras check-ems`, `ki-ops extras summarize-trades`, `ki-ops extras risk-snapshot`, `ki-ops extras notify-perturbs`, and `ki-ops poc-alpha` (dollar-panel parquet).
