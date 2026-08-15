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

### Alpha dollar panel POC (July 2026)

Derive theoretical SOD/targets from prior-day alpha **dollar** notionals (parquet: dates × security ids), run day-over-day pre-trade checks, and report **one-way** turnover \((Σ|Δ\$|/2) / GMV\). Without a price feed, unit price `1` is used so quantity equals dollars. Security ids act as symbols.

```bash
# Defaults: examples/poc_2026_07_alpha_dollars_active.parquet + config/risk_management_poc.yaml
ki-ops poc-alpha

# Optional date slice / report path
ki-ops poc-alpha examples/poc_2026_07_alpha_dollars_active.parquet \
  --start 2026-07-01 --end 2026-07-31 \
  --report-csv examples/poc_2026_07_turnover_report.csv
```

### EMS trade intents vs parquet SOD

SOD is the latest **parquet** dollar row strictly before `--as-of`. Trade intents are the headerless EMS drop (`ticker,qty,algo`). Missing marks: `px ≈ |SOD alpha $| / |qty|` when a `security_id,symbol` map is provided. No pickle file is used.

```bash
# Pretend the drop is for 2026-08-06; SOD = 2026-08-05 parquet row
ki-ops check-ems --as-of 2026-08-06
ki-ops check-ems examples/Portfolio_20260806.csv --as-of 2026-08-06 --id-map path/to/id_ticker_map.csv
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
  poc_2026_07_alpha_dollars_active.parquet
  poc_2026_07_turnover_report.csv
```
