"""Strategy identity + per-strategy config on S3.

A strategy's trading config lives under the environment's portfolio root:

    <root>/combo_weights/<strategy_id>/strategy_config.json

``strategy_config.json`` holds the params ``dollar_to_shares`` reads nightly
in the kelaidata pipeline — most importantly ``booksize`` (target gross $ the
shares trade file is scaled to). The sibling ``config.json`` (when present)
carries the combo-build parameters (method, train/test windows, *backtest*
booksize — not the trade scaler).

Strategies are addressed by their hashed pipeline id or by the friendly prod
name hardcoded in the kelaidata DAGs (``KelAIV2`` / ``KelaiV0`` / ``KelaiV1``).
The gate/kotl ``--strategy-id`` convention appends ``_neutralized`` for the
tradeable book folder; the config folder is the *base* id, so that suffix is
stripped on resolution.

All S3 access goes through an injectable client so tests never need the
network (``boto3`` comes from the ``kelaidata`` optional dependency).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ki_ops.kotl.kelaidata_source import parse_s3_url

COMBO_WEIGHTS_DIR = "combo_weights"
STRATEGY_CONFIG_FILE = "strategy_config.json"
COMBO_CONFIG_FILE = "config.json"
NEUTRALIZED_SUFFIX = "_neutralized"

# Friendly prod names → hashed pipeline strategy ids (kelaidata DAG constants:
# lseg_strategy_pipeline_dag.py / alpha_update_dag.py). Keys are lowercase;
# lookup is case-insensitive.
STRATEGY_ALIASES: dict[str, str] = {
    # LSEG live book (lseg_strategy_pipeline_combo)
    "kelaiv2": (
        "df_combo_lseg_v2b_"
        "f6a4c6adfcaef5d4a9ffeff804a1fd73bca42f35817427ac4efbcca396d30f01"
    ),
    # Polygon strategies (strategy_pipeline_combo)
    "kelaiv0": (
        "df_combo_os_"
        "0d5bc50e092bdb8b97392e890bc615c93572aa4d005750e52d4c3a2652b35e3f"
    ),
    "kelaiv1": (
        "df_v2_stability_no_"
        "e2ca49cb33c2022d8c494c817f3d627a7b352d44cf54b5434fe84f832654e926"
    ),
}


class StrategyNotFoundError(Exception):
    """The strategy has no ``strategy_config.json`` under the portfolio root."""


def resolve_strategy_id(name_or_id: str) -> str:
    """Friendly name or raw id → base strategy id (``_neutralized`` stripped)."""
    text = name_or_id.strip()
    if not text:
        raise ValueError("empty strategy name/id")
    alias = STRATEGY_ALIASES.get(text.lower())
    if alias is not None:
        return alias
    if text.endswith(NEUTRALIZED_SUFFIX):
        text = text[: -len(NEUTRALIZED_SUFFIX)]
    return text


def _s3_client(s3=None):
    if s3 is not None:
        return s3
    import boto3

    return boto3.client("s3")


def _root_parts(portfolio_root: str) -> tuple[str, str]:
    """``s3://bucket[/prefix]`` → ``(bucket, prefix)`` (prefix may be '')."""
    root = portfolio_root.rstrip("/")
    rest = root[len("s3://"):] if root.startswith("s3://") else root
    bucket, _, prefix = rest.partition("/")
    if not bucket:
        raise ValueError(f"not a valid portfolio root: {portfolio_root}")
    return bucket, prefix


def strategy_config_url(portfolio_root: str, strategy_id: str) -> str:
    bucket, prefix = _root_parts(portfolio_root)
    parts = [p for p in (prefix, COMBO_WEIGHTS_DIR, strategy_id, STRATEGY_CONFIG_FILE) if p]
    return f"s3://{bucket}/" + "/".join(parts)


def combo_config_url(portfolio_root: str, strategy_id: str) -> str:
    bucket, prefix = _root_parts(portfolio_root)
    parts = [p for p in (prefix, COMBO_WEIGHTS_DIR, strategy_id, COMBO_CONFIG_FILE) if p]
    return f"s3://{bucket}/" + "/".join(parts)


def root_config_url(portfolio_root: str) -> str:
    bucket, prefix = _root_parts(portfolio_root)
    parts = [p for p in (prefix, COMBO_CONFIG_FILE) if p]
    return f"s3://{bucket}/" + "/".join(parts)


def _get_json(url: str, s3) -> Any:
    bucket, key = parse_s3_url(url)
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8")
    return json.loads(body)


def _is_missing_key_error(exc: Exception) -> bool:
    """botocore ClientError / NoSuchKey for a key that does not exist."""
    if exc.__class__.__name__ == "NoSuchKey":
        return True
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    code = str(response.get("Error", {}).get("Code", ""))
    return code in ("NoSuchKey", "NoSuchBucket", "404", "NotFound")


def fetch_strategy_config(
    portfolio_root: str, strategy_id: str, *, s3=None
) -> dict[str, Any]:
    """``strategy_config.json`` for *strategy_id* (raises StrategyNotFoundError)."""
    s3 = _s3_client(s3)
    url = strategy_config_url(portfolio_root, strategy_id)
    try:
        data = _get_json(url, s3)
    except Exception as exc:
        if _is_missing_key_error(exc):
            raise StrategyNotFoundError(
                f"no {STRATEGY_CONFIG_FILE} at {url} — unknown strategy "
                f"{strategy_id!r} under {portfolio_root}"
            ) from exc
        raise
    if not isinstance(data, dict):
        raise ValueError(f"{url} is not a JSON object")
    return data


def fetch_combo_config(
    portfolio_root: str, strategy_id: str, *, s3=None
) -> dict[str, Any] | None:
    """Combo-build ``config.json`` for *strategy_id*, or ``None`` when absent."""
    s3 = _s3_client(s3)
    url = combo_config_url(portfolio_root, strategy_id)
    try:
        data = _get_json(url, s3)
    except Exception as exc:
        if _is_missing_key_error(exc):
            return None
        raise
    return data if isinstance(data, dict) else None


def fetch_root_booksize(portfolio_root: str, *, s3=None) -> float | None:
    """``portfolio.booksize`` from the root ``config.json`` (the pipeline's
    fallback when a strategy has no ``strategy_config.json`` override)."""
    s3 = _s3_client(s3)
    try:
        data = _get_json(root_config_url(portfolio_root), s3)
    except Exception as exc:
        if _is_missing_key_error(exc):
            return None
        raise
    if not isinstance(data, dict):
        return None
    portfolio = data.get("portfolio")
    if not isinstance(portfolio, dict):
        return None
    booksize = portfolio.get("booksize")
    return float(booksize) if booksize is not None else None


def list_strategies(portfolio_root: str, *, s3=None) -> list[str]:
    """Strategy ids with a ``strategy_config.json`` under ``combo_weights/``."""
    s3 = _s3_client(s3)
    bucket, prefix = _root_parts(portfolio_root)
    list_prefix = "/".join(p for p in (prefix, COMBO_WEIGHTS_DIR) if p) + "/"
    out: list[str] = []
    token: str | None = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": list_prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", ()):  # keys under combo_weights/
            key = obj["Key"]
            tail = key[len(list_prefix):]
            parts = tail.split("/")
            if len(parts) == 2 and parts[1] == STRATEGY_CONFIG_FILE and parts[0]:
                out.append(parts[0])
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return sorted(out)


@dataclass(frozen=True)
class ResizeResult:
    strategy_id: str
    config_url: str
    backup_url: str
    old_booksize: float | None
    new_booksize: float
    config: dict[str, Any]


def resize_strategy_booksize(
    portfolio_root: str,
    strategy_id: str,
    new_booksize: float,
    *,
    s3=None,
    now: datetime | None = None,
) -> ResizeResult:
    """Set ``booksize`` in ``strategy_config.json`` on S3 (other keys kept).

    The current file is first copied to ``strategy_config.json.bak-<UTC>`` in
    the same folder, then the updated JSON is written in place. Takes effect
    at the next nightly ``dollar_to_shares`` run that reads the config.
    """
    if new_booksize <= 0:
        raise ValueError(f"booksize must be positive, got {new_booksize}")
    s3 = _s3_client(s3)
    config = fetch_strategy_config(portfolio_root, strategy_id, s3=s3)
    old = config.get("booksize")
    old_booksize = float(old) if old is not None else None

    url = strategy_config_url(portfolio_root, strategy_id)
    bucket, key = parse_s3_url(url)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    backup_key = f"{key}.bak-{stamp}"
    s3.copy_object(
        Bucket=bucket,
        Key=backup_key,
        CopySource={"Bucket": bucket, "Key": key},
    )

    updated = dict(config)
    updated["booksize"] = float(new_booksize)
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=(json.dumps(updated, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        ContentType="application/json",
    )
    return ResizeResult(
        strategy_id=strategy_id,
        config_url=url,
        backup_url=f"s3://{bucket}/{backup_key}",
        old_booksize=old_booksize,
        new_booksize=float(new_booksize),
        config=updated,
    )
