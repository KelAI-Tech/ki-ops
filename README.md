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
| `examples/lseg_security_master.csv` | INFOCODE → ticker + listing status (`ISACTIVE`, `STATUSCODE`, `DELISTDATE`) |
| `config/risk_management_poc.yaml` | Risk limits (~$90M GMV book) |

At run time, SOD `$` → shares via CLOSE (`qty = notional / px`). Order / turnover notionals use `abs(qty) × px`.

```bash
source .venv/bin/activate   # or /data/robert/venvs/ki-ops/bin/activate
cd /path/to/ki-ops

ki-ops run-perturb              # baseline (~24% two-way TO); delisted names dropped with a warning
ki-ops run-perturb-zero         # all trade qty → 0 (TO 0); writes <trades>_zero.csv
ki-ops run-perturb-turnover     # scale to ~26% two-way TO vs 25% cap; writes <trades>_scaled.csv
```

Stdout includes config/paths, input SHA-256 hashes, SOD GMV / NMV / net exposure, turnover, `passed` (`true` / `false` / `"with warnings"`), blocks, and warnings.

Write the same JSON to disk with `--json-out FILE`.

Overrides: `--sod`, `--trades`, `--prices`, `--poc-data`, `--config`, `--as-of`.

### Turnover convention (matches kelaisim)

Turnover is **two-way (gross)**, the same convention as kelaisim's `stats.py`:

```
turnover = (buy$ + sell$) / position GMV        # GMV = Σ|position MV|, cash excluded
```

**One-way turnover is exactly half of this** (a full book replace = 200% two-way
= 100% one-way). The YAML `max_turnover: 0.25` is a **two-way** cap, i.e.
≈ 12.5% one-way — tighter than a 25% one-way cap would be. The POC baseline
(previously reported as ~12% one-way) reads ~24% two-way and clears the cap.
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
- `max_position_size` — abs **share** qty (notional / px); **warning only** (does not block)
- Tradability — live trade intents in inactive / delisted names (`ISACTIVE`, `STATUSCODE`, `DELISTDATE`) are **dropped** and **warned**; the rest of the book still sends. Unknown infocodes **warn** (ticket kept).
- Order size min/max — off (`enforce_order_size_limits: false`)

ADV / liquidity caps, point-in-time `TICKER_MAPPING_DT`, and Wolfe `h5` risk loadings are not wired yet (need those data files).

### Decimal vs float

ki-ops does all money math in Python `Decimal` for deterministic, auditable
results (no float representation drift in a pre-trade gate). kelaisim and the
alpha parquets are `float`/numpy — values are converted once at the boundary
(`Decimal(str(float(v)))`), so tiny last-digit differences vs sim-reported
numbers are expected and harmless.

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
result = PreTradeEngine.from_config_path("config/risk_management_small_book.yaml").evaluate_from_targets(
    sod, targets
)
print(result.to_dict())  # "passed", violations, warnings, turnover, …
```

Exit code `2` means a **blocking** check failed.

## Layout

```
config/
  poc_pos_and_px.yaml          # LSEG POC file manifest
  risk_management_poc.yaml     # POC / production-scale limits (~$90M GMV)
  risk_management_small_book.yaml  # small example-book limits
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
