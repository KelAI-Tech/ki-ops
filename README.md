# KI Ops

Deterministic Python library for SOD/target rebalance analysis and pre-trade risk checks. Owned by Robert Bai. Script/library first.

## Core workflow

```
trade_intents = target_intents − SOD positions
```

1. **SOD position file** — start-of-day holdings
2. **Target intents** — desired positions
3. **Trade intents** — per-symbol quantity delta (`BUY` if target > SOD, `SELL` if target < SOD)
4. **Pre-trade checks** — block the batch if risk limits are breached

Names present in SOD but missing from targets are flattened to quantity `0` by default (full exit).

## Setup

Requires Python 3.10+ and (on macOS) Xcode Command Line Tools for `python3`/`git`.

```bash
cd ~/ki-ops
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## File formats

**SOD** (`symbol,quantity,market_price[,cost_basis][,cash]`):

```csv
symbol,quantity,market_price,cost_basis,cash
AAPL,50,190.00,180.00,20000
MSFT,20,420.00,400.00,
```

**Targets** (`symbol,quantity[,market_price]`):

```csv
symbol,quantity,market_price
AAPL,40,191.00
MSFT,25,418.00
NVDA,10,120.00
```

## Library usage

```python
from ki_ops import (
    PreTradeEngine,
    load_sod_positions_csv,
    load_target_intents_csv,
)

sod = load_sod_positions_csv("examples/sod_positions.csv")
targets = load_target_intents_csv("examples/target_intents.csv")
result = PreTradeEngine.from_config_path("config/risk_management.yaml").evaluate_from_targets(
    sod, targets
)
print(result.allowed, result.trade_intents, result.to_dict())
```

## CLI

```bash
# Simplest: defaults to examples/ + config/risk_management.yaml
ki-ops
# or
python -m ki_ops
```

```bash
# Explicit files
ki-ops run path/to/sod.csv path/to/targets.csv
ki-ops derive-trades examples/sod_positions.csv examples/target_intents.csv
```

Exit code `2` means pre-trade checks blocked the batch.

### LSEG POC

Theoretical LSEG 8/6 trades (12% one-way vs 8/5 SOD): `sod_lseg_20260805.csv` + `trade_intents_lseg_20260806.csv`. Trade `quantity` is **signed whole-share qty** (dollar Δ / Datastream2 CLOSE 2026-08-04, half-up).

```bash
# Same SOD + trade CSVs for all POC risk checks
ki-ops run-perturb                 # baseline (~12% TO; no active limit breaches)
ki-ops run-perturb-zero            # same names, qty 0 → turnover 0; writes <trades>_zero.csv
ki-ops run-perturb-turnover        # scale trades to ~26% one-way (25% cap); writes <trades>_scaled.csv
```

Defaults: `config/poc_pos_and_px.yaml` (SOD, trade intents, Datastream2 px, and INFOCODE→ticker map under `examples/`).
Override individual files with `--sod`, `--trades`, `--prices`, or point at another manifest with `--poc-data`.

Optional day-over-day parquet panel:

```bash
ki-ops poc-alpha --start 2026-07-01 --end 2026-07-31
```

### Extras (sidecars)

EMS drop checks, filled-trade summaries, and factor/sector risk snapshots live under `ki_ops.extras` and `ki-ops extras …`:

```bash
ki-ops extras check-ems --as-of 2026-08-06
ki-ops extras summarize-trades path/to/fills.csv
ki-ops extras risk-snapshot
```

Example inputs: `examples/extras/`.

## Config

Risk limits live in [`config/risk_management.yaml`](config/risk_management.yaml).
LSEG POC input paths (SOD, trades, px, ticker map) live in [`config/poc_pos_and_px.yaml`](config/poc_pos_and_px.yaml).

## Layout

```
config/risk_management.yaml
config/risk_management_poc.yaml
config/poc_pos_and_px.yaml
src/ki_ops/
  alpha.py       # alpha dollar panel → theoretical SOD / day-over-day POC
  intents.py     # SOD + targets → trade intents
  config.py
  models.py
  portfolio.py
  engine.py
  poc_data.py
  checks/rules.py
  cli.py
  extras/        # EMS, filled trades, risk snapshot (optional)
tests/
  extras/
examples/
  sod_positions.csv
  target_intents.csv
  sod_lseg_20260805.csv
  trade_intents_lseg_20260806.csv
  ds2_px_20260804.csv
  lseg_security_master.csv
  df_combo_lseg_*_nosv.parquet
  extras/
    Portfolio_20260806.csv
    security_master.csv
```
