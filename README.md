# KI Ops

Deterministic Python library for SOD/target rebalance analysis and pre-trade risk checks. Owned by Robert Bai. Script/library first; same engine can be exposed as an API later.

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

Optional API extras:

```bash
pip install -e ".[api]"
uvicorn ki_ops.api:create_app --factory --reload
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

### Alpha dollar panel

SOD comes from the LSEG parquet (dates × security ids, dollar notionals). Unit price is `1` so quantity equals dollars. One-way turnover is \((\Sigma|\Delta\$|/2) / GMV\).

```bash
# Day-over-day checks on the panel (optional --start/--end)
ki-ops poc-alpha --start 2026-07-01 --end 2026-07-31

# SOD = 2026-08-05 row; EMS drop dated 2026-08-06
ki-ops check-ems --as-of 2026-08-06
```

Theoretical LSEG 8/6 trades (12% one-way vs 8/5 SOD): `sod_lseg_20260805.csv`, `trade_intents_lseg_20260806.csv`, `target_intents_lseg_20260806.csv`.

```bash
# SOD = 2026-08-05 parquet row; trades = 8/6 POC file
ki-ops run-perturb

# Breach scenarios (8/5 parquet SOD + examples/trade_intents_lseg_20260806.csv)
ki-ops run-perturb-turnover    # scale trades to ~26% one-way (25% cap)
ki-ops run-perturb-order-size  # unscaled trades; ARX trips max_order_size
```

### Risk snapshot (factor / sector / beta)

PM view of a market-neutral book. Weights are signed market value / position GMV (cash excluded) so dollar-neutral NAV does not inflate percentages.

Loadings come from a security master CSV — export from Arcana, Barra, Axioma, Wolfe, or an internal model. The example file is illustrative, not a live risk model.

```bash
# Example SOD vs targets
ki-ops risk-snapshot

# Current book only
ki-ops risk-snapshot path/to/sod.csv --universe path/to/security_master.csv

# SOD vs proposed target book
ki-ops risk-snapshot path/to/sod.csv path/to/targets.csv --universe path/to/security_master.csv
```

Security master columns: `symbol,sector,industry` plus `beta_*` (e.g. `beta_spx`) and `factor_*` (e.g. `factor_value`). A bare `beta` column is treated as `beta_spx`.

Library usage:

```python
from ki_ops import build_risk_snapshot, load_security_master_csv, load_sod_positions_csv

sod = load_sod_positions_csv("examples/sod_positions.csv")
universe = load_security_master_csv("examples/security_master.csv")
print(build_risk_snapshot(sod, universe).to_dict())
```

The JSON report includes book KPIs (NAV, GMV, net/gross, dollar beta), GMV-weighted factor / sector / industry / beta exposures with long/short split, per-name contributions, and a `delta` block when targets are supplied.

## Config

Risk limits live in [`config/risk_management.yaml`](config/risk_management.yaml).

## Layout

```
config/risk_management.yaml
config/risk_management_poc.yaml
src/ki_ops/
  alpha.py       # alpha dollar panel → theoretical SOD / day-over-day POC
  intents.py     # SOD + targets → trade intents
  config.py
  models.py
  trades.py
  portfolio.py
  engine.py
  risk.py        # factor / sector / beta snapshot
  checks/rules.py
  cli.py
  api.py
tests/
examples/
  sod_positions.csv
  target_intents.csv
  security_master.csv
  sample_orders.csv
  sample_trades.csv
  Portfolio_20260806.csv
  sod_lseg_20260805.csv
  trade_intents_lseg_20260806.csv
  target_intents_lseg_20260806.csv
  df_combo_lseg_*_nosv.parquet
```
