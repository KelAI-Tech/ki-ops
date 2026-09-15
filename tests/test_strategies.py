"""Strategy identity + strategy_config.json S3 access."""

import json
from datetime import datetime, timezone

import pytest

from tests.fake_s3 import FakeS3
from ki_ops.strategies import (
    STRATEGY_ALIASES,
    StrategyNotFoundError,
    fetch_combo_config,
    fetch_root_booksize,
    fetch_strategy_config,
    list_strategies,
    resize_strategy_booksize,
    resolve_strategy_id,
    strategy_config_url,
)

ROOT = "s3://kelaitrading/portfolio_canary"
V2B = STRATEGY_ALIASES["kelaiv2"]


def _fake(objects=None, **kwargs) -> FakeS3:
    return FakeS3(objects, **kwargs)


def _key(strategy_id: str, filename: str = "strategy_config.json") -> tuple[str, str]:
    return ("kelaitrading", f"portfolio_canary/combo_weights/{strategy_id}/{filename}")


# --- resolution -------------------------------------------------------------


def test_alias_resolution_case_insensitive():
    assert resolve_strategy_id("KelAIV2") == V2B
    assert resolve_strategy_id("kelaiv2") == V2B
    assert resolve_strategy_id("KELAIV0") == STRATEGY_ALIASES["kelaiv0"]
    assert resolve_strategy_id("KelaiV1") == STRATEGY_ALIASES["kelaiv1"]


def test_raw_id_passes_through():
    assert resolve_strategy_id("df_combo_custom_abc123") == "df_combo_custom_abc123"


def test_neutralized_suffix_stripped():
    assert resolve_strategy_id(f"{V2B}_neutralized") == V2B


def test_empty_strategy_raises():
    with pytest.raises(ValueError, match="empty strategy"):
        resolve_strategy_id("  ")


def test_strategy_config_url():
    assert strategy_config_url(ROOT, "abc") == (
        "s3://kelaitrading/portfolio_canary/combo_weights/abc/strategy_config.json"
    )


# --- fetch ------------------------------------------------------------------


def test_fetch_strategy_config():
    s3 = _fake({_key(V2B): b'{"booksize": 2000000.0}'})
    assert fetch_strategy_config(ROOT, V2B, s3=s3) == {"booksize": 2000000.0}


def test_fetch_strategy_config_missing_raises():
    with pytest.raises(StrategyNotFoundError, match="unknown strategy"):
        fetch_strategy_config(ROOT, "nope", s3=_fake())


def test_fetch_combo_config_absent_is_none():
    s3 = _fake({_key(V2B): b'{"booksize": 2000000.0}'})
    assert fetch_combo_config(ROOT, V2B, s3=s3) is None


def test_fetch_combo_config_present():
    s3 = _fake(
        {
            _key(V2B): b'{"booksize": 2000000.0}',
            _key(V2B, "config.json"): b'{"method": "amm_mvo", "booksize": 20000000}',
        }
    )
    combo = fetch_combo_config(ROOT, V2B, s3=s3)
    assert combo["method"] == "amm_mvo"


def test_fetch_root_booksize():
    s3 = _fake(
        {("kelaitrading", "portfolio_canary/config.json"): b'{"portfolio": {"booksize": 100000.0}}'}
    )
    assert fetch_root_booksize(ROOT, s3=s3) == 100000.0
    assert fetch_root_booksize(ROOT, s3=_fake()) is None


# --- list -------------------------------------------------------------------


def test_list_strategies_only_config_folders():
    s3 = _fake(
        {
            _key("strat_a"): b"{}",
            _key("strat_b"): b"{}",
            _key("strat_b", "weights.csv"): b"x",
            _key("no_config", "config.json"): b"{}",
            # a nested key that must not count
            ("kelaitrading", "portfolio_canary/combo_weights/deep/sub/strategy_config.json"): b"{}",
        }
    )
    assert list_strategies(ROOT, s3=s3) == ["strat_a", "strat_b"]


def test_list_strategies_paginates():
    objects = {_key(f"strat_{i:02d}"): b"{}" for i in range(7)}
    s3 = _fake(objects, page_size=3)
    assert list_strategies(ROOT, s3=s3) == [f"strat_{i:02d}" for i in range(7)]


# --- resize -----------------------------------------------------------------


def test_resize_updates_booksize_and_backs_up():
    s3 = _fake({_key(V2B): b'{"booksize": 2000000.0, "note": "keep me"}'})
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
    result = resize_strategy_booksize(ROOT, V2B, 3_000_000, s3=s3, now=now)

    assert result.old_booksize == 2000000.0
    assert result.new_booksize == 3000000.0
    assert result.backup_url.endswith("strategy_config.json.bak-20260914T120000Z")

    bucket, key = _key(V2B)
    updated = json.loads(s3.objects[(bucket, key)])
    assert updated == {"booksize": 3000000.0, "note": "keep me"}

    backup = json.loads(s3.objects[(bucket, f"{key}.bak-20260914T120000Z")])
    assert backup == {"booksize": 2000000.0, "note": "keep me"}
    # backup copied before the new file was written
    assert s3.copy_calls and s3.put_calls


def test_resize_missing_strategy_raises_before_any_write():
    s3 = _fake()
    with pytest.raises(StrategyNotFoundError):
        resize_strategy_booksize(ROOT, "nope", 1_000_000, s3=s3)
    assert not s3.put_calls and not s3.copy_calls


def test_resize_rejects_non_positive():
    s3 = _fake({_key(V2B): b'{"booksize": 2000000.0}'})
    with pytest.raises(ValueError, match="positive"):
        resize_strategy_booksize(ROOT, V2B, 0, s3=s3)
    assert not s3.put_calls
