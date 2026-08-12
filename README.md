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

## Config

Risk limits live in [`config/risk_management.yaml`](config/risk_management.yaml).

## Layout

```
config/risk_management.yaml
src/ki_ops/
  intents.py     # SOD + targets → trade intents
  config.py
  models.py
  trades.py
  portfolio.py
  engine.py
  checks/rules.py
  cli.py
  api.py
tests/
examples/
```
