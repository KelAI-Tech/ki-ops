"""ki-ops strat CLI: list / config / booksize / resize."""

import json

import pytest

from ki_ops.cli import main
from ki_ops.strategies import STRATEGY_ALIASES
from tests.fake_s3 import FakeS3

V2B = STRATEGY_ALIASES["kelaiv2"]

CANARY_PREFIX = "portfolio_canary"
PROD_PREFIX = "portfolio"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("KI_OPS_ENV", raising=False)


@pytest.fixture()
def s3(monkeypatch):
    fake = FakeS3(
        {
            (
                "kelaitrading",
                f"{CANARY_PREFIX}/combo_weights/{V2B}/strategy_config.json",
            ): b'{"booksize": 2000000.0}',
            (
                "kelaitrading",
                f"{CANARY_PREFIX}/combo_weights/{V2B}/config.json",
            ): b'{"method": "amm_mvo", "booksize": 20000000}',
            (
                "kelaitrading",
                f"{CANARY_PREFIX}/combo_weights/other_strat/strategy_config.json",
            ): b'{"booksize": 500000.0}',
            (
                "kelaitrading",
                f"{CANARY_PREFIX}/config.json",
            ): b'{"portfolio": {"booksize": 100000.0}}',
            (
                "kelaitrading",
                f"{PROD_PREFIX}/combo_weights/{V2B}/strategy_config.json",
            ): b'{"booksize": 9000000.0}',
        }
    )
    monkeypatch.setattr("ki_ops.strategies._s3_client", lambda s3=None: s3 or fake)
    return fake


def _json_out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_list_default_canary(s3, capsys):
    assert main(["strat", "list", "--json"]) == 0
    out = _json_out(capsys)
    assert out["env"] == "canary"
    assert out["portfolio_root"] == "s3://kelaitrading/portfolio_canary"
    assert out["strategies"] == sorted([V2B, "other_strat"])


def test_config_by_friendly_name(s3, capsys):
    assert main(["strat", "config", "KelAIV2", "--json"]) == 0
    out = _json_out(capsys)
    assert out["strategy_id"] == V2B
    assert out["strategy_config"] == {"booksize": 2000000.0}
    assert out["combo_config"]["method"] == "amm_mvo"
    assert out["strategy_config_url"].endswith(f"{V2B}/strategy_config.json")


def test_config_neutralized_id_maps_to_base_folder(s3, capsys):
    assert main(["strat", "config", f"{V2B}_neutralized", "--json"]) == 0
    assert _json_out(capsys)["strategy_id"] == V2B


def test_config_unknown_strategy_exits_1(s3, capsys):
    assert main(["strat", "config", "does_not_exist", "--json"]) == 1
    assert "unknown strategy" in capsys.readouterr().err


def test_config_env_var_selects_prod(s3, capsys, monkeypatch):
    monkeypatch.setenv("KI_OPS_ENV", "prod")
    assert main(["strat", "config", "KelAIV2", "--json"]) == 0
    out = _json_out(capsys)
    assert out["env"] == "prod"
    assert out["strategy_config"] == {"booksize": 9000000.0}


def test_config_ki_env_flag_beats_env_var(s3, capsys, monkeypatch):
    monkeypatch.setenv("KI_OPS_ENV", "prod")
    assert main(["strat", "config", "KelAIV2", "--ki-env", "canary", "--json"]) == 0
    assert _json_out(capsys)["strategy_config"] == {"booksize": 2000000.0}


def test_booksize_single(s3, capsys):
    assert main(["strat", "booksize", "KelAIV2", "--json"]) == 0
    out = _json_out(capsys)
    assert out["booksizes"] == [{"strategy_id": V2B, "booksize": 2000000.0}]
    assert out["root_config_booksize_fallback"] == 100000.0


def test_booksize_all(s3, capsys):
    assert main(["strat", "booksize", "--json"]) == 0
    out = _json_out(capsys)
    by_id = {row["strategy_id"]: row["booksize"] for row in out["booksizes"]}
    assert by_id == {V2B: 2000000.0, "other_strat": 500000.0}


def test_booksize_table_output(s3, capsys):
    assert main(["strat", "booksize", "KelAIV2"]) == 0
    out = capsys.readouterr().out
    assert "2,000,000" in out
    assert "env=canary" in out


def test_resize_dry_run_writes_nothing(s3, capsys):
    assert main(
        ["strat", "resize", "KelAIV2", "--booksize", "3000000", "--dry-run", "--json"]
    ) == 0
    out = _json_out(capsys)
    assert out["dry_run"] is True
    assert out["old_booksize"] == 2000000.0
    assert out["new_booksize"] == 3000000.0
    assert not s3.put_calls and not s3.copy_calls


def test_resize_refused_without_yes_when_not_a_tty(s3, capsys):
    # pytest's stdin is not a tty, so the interactive confirm is unavailable
    assert main(["strat", "resize", "KelAIV2", "--booksize", "3000000"]) == 2
    assert "needs interactive confirmation or --yes" in capsys.readouterr().err
    assert not s3.put_calls


def test_resize_with_yes_writes_and_backs_up(s3, capsys):
    assert main(
        ["strat", "resize", "KelAIV2", "--booksize", "3000000", "--yes", "--json"]
    ) == 0
    out = _json_out(capsys)
    assert out["old_booksize"] == 2000000.0
    assert out["new_booksize"] == 3000000.0
    assert ".bak-" in out["backup_url"]

    key = f"{CANARY_PREFIX}/combo_weights/{V2B}/strategy_config.json"
    assert json.loads(s3.objects[("kelaitrading", key)]) == {"booksize": 3000000.0}
    assert s3.copy_calls  # backup happened


def test_resize_rejects_non_positive(s3, capsys):
    assert main(["strat", "resize", "KelAIV2", "--booksize", "0", "--yes"]) == 2
    assert "must be positive" in capsys.readouterr().err
    assert not s3.put_calls


def test_resize_unknown_strategy_exits_1(s3, capsys):
    assert main(["strat", "resize", "nope", "--booksize", "1000", "--yes"]) == 1
    assert not s3.put_calls


def test_portfolio_root_override(s3, capsys):
    assert main(
        [
            "strat",
            "config",
            "KelAIV2",
            "--portfolio-root",
            "s3://kelaitrading/portfolio",
            "--json",
        ]
    ) == 0
    assert _json_out(capsys)["strategy_config"] == {"booksize": 9000000.0}
