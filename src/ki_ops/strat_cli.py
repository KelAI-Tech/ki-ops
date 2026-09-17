"""CLI handlers for ``ki-ops strat`` — env-aware strategy config on S3.

Environment (canary vs prod) comes from ``--ki-env`` > ``KI_OPS_ENV`` >
default ``canary`` (see :mod:`ki_ops.opsenv`); ``--portfolio-root`` overrides
the resolved S3 root entirely.

Subcommands:

- ``list`` — strategies with a ``strategy_config.json`` under ``combo_weights/``
- ``config <id|name>`` — print ``strategy_config.json`` (+ combo ``config.json``)
- ``booksize [<id|name>]`` — configured booksize for one strategy or all
- ``resize <id|name> --booksize N`` — update ``booksize`` for the next day
  (backup + confirmation; ``--dry-run`` / ``--yes``)
"""

from __future__ import annotations

import json
import sys

from ki_ops.opsenv import resolve_ops_env
from ki_ops.strategies import (
    StrategyNotFoundError,
    fetch_combo_config,
    fetch_root_booksize,
    fetch_strategy_config,
    list_strategies,
    resize_strategy_booksize,
    resolve_strategy_id,
    strategy_config_url,
)

EXIT_STRATEGY_NOT_FOUND = 1
EXIT_REFUSED = 2


def register_strat_parser(sub) -> None:
    strat = sub.add_parser(
        "strat",
        help="strategy config on S3 (canary/prod via KI_OPS_ENV, default canary)",
    )
    ss = strat.add_subparsers(dest="strat_command", required=True)

    def _add_common(parser) -> None:
        parser.add_argument(
            "--env",
            "--ki-env",
            dest="ki_env",
            choices=("canary", "prod"),
            default=None,
            help="ops environment (default: KI_OPS_ENV env var, else canary)",
        )
        parser.add_argument(
            "--portfolio-root",
            default=None,
            help="override the env's portfolio root (s3://kelaitrading/portfolio_canary "
            "for canary, s3://kelaitrading/portfolio for prod)",
        )
        parser.add_argument("--json", action="store_true", help="JSON output")

    ls = ss.add_parser("list", help="strategies under <root>/combo_weights/")
    _add_common(ls)

    cf = ss.add_parser(
        "config",
        help="print strategy_config.json (+ combo config.json) for a strategy",
    )
    cf.add_argument(
        "strategy",
        help="strategy id or friendly name (KelAIV2/KelaiV0/KelaiV1); "
        "a _neutralized suffix is stripped",
    )
    _add_common(cf)

    bs = ss.add_parser(
        "booksize",
        help="configured booksize (strategy_config.json) for one strategy or all",
    )
    bs.add_argument("strategy", nargs="?", default=None, help="strategy id or name (default: all)")
    _add_common(bs)

    rz = ss.add_parser(
        "resize",
        help="set booksize in strategy_config.json for the next trading day",
    )
    rz.add_argument("strategy", help="strategy id or friendly name")
    rz.add_argument(
        "--booksize",
        type=float,
        required=True,
        help="new target gross $ booksize (read by the next nightly dollar_to_shares)",
    )
    rz.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    rz.add_argument("--dry-run", action="store_true", help="show old → new; write nothing")
    _add_common(rz)


def _resolve_root(args) -> tuple[str, str]:
    """→ ``(env_name, portfolio_root)``."""
    env = resolve_ops_env(getattr(args, "ki_env", None))
    root = getattr(args, "portfolio_root", None) or env.portfolio_root
    return env.name, root


def _print(payload: dict, *, as_json: bool, text: str) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(text)


def _booksize_str(value) -> str:
    if value is None:
        return "-"
    return f"{float(value):,.0f}"


def _cmd_list(args) -> int:
    env_name, root = _resolve_root(args)
    ids = list_strategies(root)
    payload = {"env": env_name, "portfolio_root": root, "strategies": ids}
    lines = [f"strategies under {root}/combo_weights/ (env={env_name}):"]
    lines += [f"  {sid}" for sid in ids] or ["  (none)"]
    _print(payload, as_json=args.json, text="\n".join(lines))
    return 0


def _cmd_config(args) -> int:
    env_name, root = _resolve_root(args)
    sid = resolve_strategy_id(args.strategy)
    try:
        strategy_config = fetch_strategy_config(root, sid)
    except StrategyNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_STRATEGY_NOT_FOUND
    combo_config = fetch_combo_config(root, sid)
    payload = {
        "env": env_name,
        "portfolio_root": root,
        "strategy": args.strategy,
        "strategy_id": sid,
        "strategy_config_url": strategy_config_url(root, sid),
        "strategy_config": strategy_config,
        "combo_config": combo_config,
    }
    text_lines = [
        f"strategy: {args.strategy} → {sid}",
        f"env: {env_name}",
        f"config: {payload['strategy_config_url']}",
        "",
        "strategy_config.json (trading params — booksize scales the nightly shares file):",
        json.dumps(strategy_config, indent=2, sort_keys=True),
    ]
    if combo_config is not None:
        text_lines += [
            "",
            "config.json (combo-build params; its booksize is the backtest size, not the trade scaler):",
            json.dumps(combo_config, indent=2, sort_keys=True),
        ]
    _print(payload, as_json=args.json, text="\n".join(text_lines))
    return 0


def _cmd_booksize(args) -> int:
    env_name, root = _resolve_root(args)
    root_booksize = fetch_root_booksize(root)

    if args.strategy:
        sid = resolve_strategy_id(args.strategy)
        try:
            config = fetch_strategy_config(root, sid)
        except StrategyNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return EXIT_STRATEGY_NOT_FOUND
        rows = [(sid, config.get("booksize"))]
    else:
        rows = []
        for sid in list_strategies(root):
            config = fetch_strategy_config(root, sid)
            rows.append((sid, config.get("booksize")))

    payload = {
        "env": env_name,
        "portfolio_root": root,
        "root_config_booksize_fallback": root_booksize,
        "booksizes": [
            {"strategy_id": sid, "booksize": bk} for sid, bk in rows
        ],
    }
    width = max([len("strategy_id")] + [len(sid) for sid, _ in rows])
    lines = [
        f"booksize (env={env_name}, root={root}):",
        f"{'strategy_id'.ljust(width)}  booksize",
        f"{'-' * width}  --------",
    ]
    lines += [f"{sid.ljust(width)}  {_booksize_str(bk)}" for sid, bk in rows]
    lines.append("")
    lines.append(
        f"root config.json portfolio.booksize fallback: {_booksize_str(root_booksize)}"
    )
    _print(payload, as_json=args.json, text="\n".join(lines))
    return 0


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in ("y", "yes")


def _cmd_resize(args) -> int:
    env_name, root = _resolve_root(args)
    sid = resolve_strategy_id(args.strategy)
    try:
        config = fetch_strategy_config(root, sid)
    except StrategyNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_STRATEGY_NOT_FOUND

    old = config.get("booksize")
    new = float(args.booksize)
    if new <= 0:
        print(f"ERROR: booksize must be positive, got {new}", file=sys.stderr)
        return EXIT_REFUSED
    url = strategy_config_url(root, sid)
    headline = (
        f"resize {sid} (env={env_name}): booksize "
        f"{_booksize_str(old)} → {_booksize_str(new)} at {url}"
    )

    if args.dry_run:
        payload = {
            "env": env_name,
            "strategy_id": sid,
            "config_url": url,
            "old_booksize": old,
            "new_booksize": new,
            "dry_run": True,
        }
        _print(payload, as_json=args.json, text=f"DRY RUN: {headline}\n(nothing written)")
        return 0

    if not args.yes and not _confirm(headline):
        print(
            "REFUSED: resize needs interactive confirmation or --yes",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    result = resize_strategy_booksize(root, sid, new)
    payload = {
        "env": env_name,
        "strategy_id": sid,
        "config_url": result.config_url,
        "backup_url": result.backup_url,
        "old_booksize": result.old_booksize,
        "new_booksize": result.new_booksize,
        "dry_run": False,
    }
    text = "\n".join(
        [
            headline,
            f"backup: {result.backup_url}",
            "takes effect at the next nightly dollar_to_shares run that reads the config",
        ]
    )
    _print(payload, as_json=args.json, text=text)
    return 0


def run_strat(args) -> int:
    cmd = args.strat_command
    if cmd == "list":
        return _cmd_list(args)
    if cmd == "config":
        return _cmd_config(args)
    if cmd == "booksize":
        return _cmd_booksize(args)
    if cmd == "resize":
        return _cmd_resize(args)
    raise SystemExit(f"unknown strat command: {cmd}")
