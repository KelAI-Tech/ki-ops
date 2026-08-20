# KI Ops

Deterministic pre-trade risk checks for rebalance trade intents.

```
trade_intents = target − SOD
```

The primary demo path is an LSEG long/short POC: start-of-day dollar notionals, signed share trade intents, Datastream2 CLOSE prices, and YAML risk limits.

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

Inputs are listed in [`config/poc_pos_and_px.yaml`](config/poc_pos_and_px.yaml):

| File | Role |
|------|------|
| `examples/sod_lseg_20260805.csv` | SOD dollar notionals (`infocode,ticker,notional`) |
| `examples/trade_intents_lseg_20260806.csv` | Signed whole-share trade qtys |
| `examples/ds2_px_20260804.csv` | Datastream2 CLOSE 2026-08-04 |
| `examples/lseg_security_master.csv` | INFOCODE → ticker |
| `config/risk_management_poc.yaml` | Risk limits (~$90M GMV book) |

At run time, SOD `$` → shares via CLOSE (`qty = notional / px`). Order / turnover notionals use `abs(qty) × px`. One-way turnover is `(buys+sells)/2 / GMV`.

```bash
source .venv/bin/activate   # or /data/robert/venvs/ki-ops/bin/activate
cd /path/to/ki-ops

ki-ops run-perturb              # baseline (~12% TO; clean pass)
ki-ops run-perturb-zero         # all trade qty → 0 (TO 0); writes <trades>_zero.csv
ki-ops run-perturb-turnover     # scale to ~26% TO vs 25% cap; writes <trades>_scaled.csv
```

Stdout includes config/paths, SOD GMV / net MV, turnover, `passed` (`true` / `false` / `"with warnings"`), blocks, and warnings.

Overrides: `--sod`, `--trades`, `--prices`, `--poc-data`, `--config`.

### Risk notes (POC)

- `max_turnover` — one-way; **blocks**
- `max_portfolio_value` — projected GMV; **blocks**
- `max_position_concentration` — |MV| / GMV; **blocks**
- `max_position_size` — abs **share** qty (notional / px); **warning only** (does not block)
- Order size min/max — off (`enforce_order_size_limits: false`)

## Generic SOD / targets

Still supported for smaller example books:

```bash
ki-ops run examples/sod_positions.csv examples/target_intents.csv
ki-ops derive-trades examples/sod_positions.csv examples/target_intents.csv
```

```python
from ki_ops import PreTradeEngine, load_sod_positions_csv, load_target_intents_csv

sod = load_sod_positions_csv("examples/sod_positions.csv")
targets = load_target_intents_csv("examples/target_intents.csv")
result = PreTradeEngine.from_config_path("config/risk_management.yaml").evaluate_from_targets(
    sod, targets
)
print(result.to_dict())  # "passed", violations, warnings, turnover, …
```

Exit code `2` means a **blocking** check failed.

## Optional: alpha panel / extras

Day-over-day parquet checks need a local alpha dollar panel (not shipped — pass the path as the first argument):

```bash
ki-ops poc-alpha path/to/panel.parquet --start 2026-07-01 --end 2026-07-31
```

Sidecars:

```bash
ki-ops extras check-ems --as-of 2026-08-06
ki-ops extras summarize-trades path/to/fills.csv
ki-ops extras risk-snapshot
```

Extras live under `ki_ops.extras` with inputs in `examples/extras/`.

## Layout

```
config/
  poc_pos_and_px.yaml          # LSEG POC file manifest
  risk_management_poc.yaml     # POC limits
  risk_management.yaml         # small example-book limits
src/ki_ops/
  engine.py, checks/, intents.py, portfolio.py, models.py, config.py
  alpha.py, poc_data.py, cli.py
  extras/                      # EMS, fills, risk snapshot
examples/
  sod_lseg_*.csv, trade_intents_lseg_*.csv, ds2_px_*.csv, lseg_security_master.csv
  sod_positions.csv, target_intents.csv
  extras/
tests/
```
