"""``ki-ops gate`` — dollar/shares pre-trade gate for the kelaidata pipeline.

All fixtures are local temp files; no network. The ds2 H5 fixture builder is
shared with the kelaidata source tests (AAPL=101, MSFT=102, TSLA=103;
2026-08-06 prior close: AAPL 191.5, MSFT 505, TSLA 252).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("h5py")
pytest.importorskip("numpy")

from ki_ops.cli import main as cli_main
from ki_ops.gate import (
    dollar_book_path,
    find_prior_file,
    load_dollar_book,
    shares_candidate_paths,
)
from tests.kotl.test_kelaidata_source import make_ds2_h5

TD = date(2026, 8, 6)
STRATEGY = "df_combo_test_neutralized"


def write_dollar_book(path: Path, rows: dict[str, str]) -> Path:
    lines = ["SecurityID,$_value"] + [f"{sid},{val}" for sid, val in rows.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


def write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "risk.yaml"
    cfg.write_text(
        "risk_management:\n"
        "  max_net_exposure: 0.10\n"
        "  max_turnover: 0.25\n"
        "  max_position_concentration: 0.5\n"
    )
    return cfg


def run_gate_cli(tmp_path: Path, capsys, *extra: str) -> tuple[int, dict]:
    cfg = write_config(tmp_path)
    argv = [
        "gate",
        "--strategy-id",
        STRATEGY,
        "--trade-date",
        TD.isoformat(),
        "--config",
        str(cfg),
        "--cache-dir",
        str(tmp_path / "cache"),
        *extra,
    ]
    rc = cli_main(argv)
    return rc, json.loads(capsys.readouterr().out)


def test_dollar_book_conventions():
    assert dollar_book_path("sid", TD) == "s3://kelaitrading/portfolio/dollar/sid/Portfolio_20260806.csv"
    assert dollar_book_path("sid", TD, env="dev") == (
        "s3://kelaitrading/portfolio_dev/dollar/sid/Portfolio_20260806.csv"
    )
    assert shares_candidate_paths("sid", TD) == [
        "s3://kelaitrading/portfolio/shares/sid/Portfolio_20260806.csv",
        "s3://kelaitrading/portfolio/shares/Portfolio_20260806.csv",
    ]


def test_load_dollar_book(tmp_path):
    f = write_dollar_book(tmp_path / "Portfolio_20260806.csv", {"101": "50000", "102": "-50000", "103": "0"})
    book = load_dollar_book(f)
    assert book == {"101": Decimal("50000"), "102": Decimal("-50000")}  # zero rows dropped

    dup = tmp_path / "dup.csv"
    dup.write_text("SecurityID,$_value\n101,5\n101,6\n")
    with pytest.raises(ValueError, match="Duplicate SecurityIDs"):
        load_dollar_book(dup)


def test_find_prior_file_local(tmp_path):
    current = write_dollar_book(tmp_path / "Portfolio_20260806.csv", {"101": "1"})
    assert find_prior_file(current, TD) is None
    older = write_dollar_book(tmp_path / "Portfolio_20260804.csv", {"101": "1"})
    newer = write_dollar_book(tmp_path / "Portfolio_20260805.csv", {"101": "1"})
    write_dollar_book(tmp_path / "Portfolio_20260807.csv", {"101": "1"})  # future: ignored
    assert find_prior_file(current, TD) == str(newer)
    assert find_prior_file(current, date(2026, 8, 5)) == str(older)


def test_gate_pass_with_prior(tmp_path, capsys):
    dollar = write_dollar_book(tmp_path / "Portfolio_20260806.csv", {"101": "50000", "102": "-50000"})
    write_dollar_book(tmp_path / "Portfolio_20260805.csv", {"101": "48000", "102": "-48000"})
    out_json = tmp_path / "verdict.json"

    rc, payload = run_gate_cli(
        tmp_path, capsys, "--dollar-file", str(dollar), "--json-out", str(out_json)
    )
    assert rc == 0
    assert payload["passed"] is True
    assert payload["violation_codes"] == []
    assert payload["dollar"]["net_exposure"] == "0.0000"
    assert payload["dollar"]["turnover"] == "0.0417"  # 4000 traded / 96000 prior GMV
    assert payload["ki_ops_version"] == "0.2.0"
    assert payload["corp_action_check"] == "not_implemented"
    assert payload["shares"] is None  # offline run: shares check skipped
    assert payload["input_hashes"].keys() >= {"config", "dollar", "prior"}
    assert json.loads(out_json.read_text()) == payload


def test_gate_s3_config_is_fetched(tmp_path, capsys, monkeypatch):
    """An ``s3://`` --config is resolved through fetch() (cache), not open()."""
    import ki_ops.gate as gate_mod

    cfg = write_config(tmp_path)
    s3_uri = "s3://kelaitrading/config/ki_ops/risk.yaml"
    real_fetch = gate_mod.fetch

    def fake_fetch(path, *, cache_dir):
        if str(path) == s3_uri:
            return cfg
        return real_fetch(path, cache_dir=cache_dir)

    monkeypatch.setattr(gate_mod, "fetch", fake_fetch)
    dollar = write_dollar_book(tmp_path / "Portfolio_20260806.csv", {"101": "50000", "102": "-50000"})
    argv = [
        "gate",
        "--strategy-id", STRATEGY,
        "--trade-date", TD.isoformat(),
        "--config", s3_uri,
        "--cache-dir", str(tmp_path / "cache"),
        "--dollar-file", str(dollar),
    ]
    rc = cli_main(argv)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["config"] == s3_uri  # verdict records the S3 URI, not the cache path
    assert payload["max_net_exposure"] == "0.1000"
    assert "config" in payload["input_hashes"]


def test_sma_ima_config_matches_mandate():
    """config/risk_management_sma_ima.yaml carries the SMA/IMA mandate limits."""
    from ki_ops.config import load_risk_settings

    repo_yaml = Path(__file__).resolve().parents[1] / "config" / "risk_management_sma_ima.yaml"
    settings = load_risk_settings(repo_yaml)
    assert settings.max_net_exposure == Decimal("0.02")  # |net| <= 10% AUM at 5x leverage
    assert settings.max_position_concentration == Decimal("0.05")  # IMA 5% of GMV
    assert settings.max_turnover == Decimal("0.25")  # two-way, kelaisim convention
    assert settings.max_adv_participation == Decimal("0.10")  # warn-only
    assert settings.max_portfolio_value == Decimal("125000000")  # 500% of $25M AUM


def test_gate_net_exposure_block(tmp_path, capsys):
    dollar = write_dollar_book(tmp_path / "Portfolio_20260806.csv", {"101": "80000", "102": "-20000"})
    write_dollar_book(tmp_path / "Portfolio_20260805.csv", {"101": "79000", "102": "-21000"})
    rc, payload = run_gate_cli(tmp_path, capsys, "--dollar-file", str(dollar))
    assert rc == 2
    assert payload["passed"] is False
    assert "MAX_NET_EXPOSURE" in payload["violation_codes"]


def test_gate_concentration_block(tmp_path, capsys):
    dollar = write_dollar_book(
        tmp_path / "Portfolio_20260806.csv",
        {"101": "70000", "102": "-22000", "103": "-22000", "104": "-22000"},
    )
    write_dollar_book(
        tmp_path / "Portfolio_20260805.csv",
        {"101": "70000", "102": "-22000", "103": "-22000", "104": "-22000"},
    )
    rc, payload = run_gate_cli(tmp_path, capsys, "--dollar-file", str(dollar))
    assert rc == 2
    assert "MAX_POSITION_CONCENTRATION" in payload["violation_codes"]
    assert [v["symbol"] for v in payload["violations"]] == ["101"]


def test_gate_turnover_block_vs_prior(tmp_path, capsys):
    dollar = write_dollar_book(tmp_path / "Portfolio_20260806.csv", {"101": "-50000", "102": "50000"})
    prior = write_dollar_book(tmp_path / "other_name.csv", {"101": "50000", "102": "-50000"})
    rc, payload = run_gate_cli(
        tmp_path, capsys, "--dollar-file", str(dollar), "--prior-file", str(prior)
    )
    assert rc == 2
    assert "MAX_TURNOVER" in payload["violation_codes"]
    assert payload["dollar"]["turnover"] == "2.0000"


def test_gate_missing_prior_warns(tmp_path, capsys):
    dollar = write_dollar_book(tmp_path / "Portfolio_20260806.csv", {"101": "50000", "102": "-50000"})
    rc, payload = run_gate_cli(tmp_path, capsys, "--dollar-file", str(dollar))
    assert rc == 0
    assert payload["passed"] == "with warnings"
    assert "PRIOR_BOOK_MISSING" in payload["warning_codes"]
    assert payload["dollar"]["turnover"] is None


def test_gate_shares_net_breach(tmp_path, capsys):
    """The real-world failure this gate exists for: dollar book neutral, shares book one-sided."""
    dollar = write_dollar_book(tmp_path / "d" / "Portfolio_20260806.csv", {"101": "47875", "102": "-47875"})
    write_dollar_book(tmp_path / "d" / "Portfolio_20260805.csv", {"101": "47000", "102": "-47000"})
    h5 = make_ds2_h5(tmp_path / "ds2_data.h5")
    shares = tmp_path / "s" / "Portfolio_20260806.csv"
    shares.parent.mkdir()
    shares.write_text("AAPL,500,VWAP\nMSFT,10,VWAP\n")  # both long: +100% net

    rc, payload = run_gate_cli(
        tmp_path,
        capsys,
        "--dollar-file",
        str(dollar),
        "--shares-file",
        str(shares),
        "--ds2",
        str(h5),
    )
    assert rc == 2
    assert payload["passed"] is False
    assert payload["violation_codes"] == ["SHARES_MAX_NET_EXPOSURE"]
    assert payload["shares"]["net_exposure"] == "1.0000"
    assert payload["shares"]["px_as_of"] == "2026-08-05"
    assert payload["shares"]["names_dropped"] == 0
    assert "SHARES_PRIOR_MISSING" in payload["warning_codes"]


def test_gate_shares_dropped_names_and_churn(tmp_path, capsys):
    dollar = write_dollar_book(
        tmp_path / "d" / "Portfolio_20260806.csv",
        {"101": "40000", "102": "-40000", "103": "10000", "104": "-10000"},
    )
    h5 = make_ds2_h5(tmp_path / "ds2_data.h5")
    sdir = tmp_path / "s"
    sdir.mkdir()
    # 103 (TSLA) and 104 (unknown infocode) are in the dollar book but not the shares file.
    (sdir / "Portfolio_20260806.csv").write_text("AAPL,200,VWAP\nMSFT,-80,VWAP\n")
    (sdir / "Portfolio_20260805.csv").write_text("AAPL,190,VWAP\nMSFT,-76,VWAP\n")

    rc, payload = run_gate_cli(
        tmp_path,
        capsys,
        "--dollar-file",
        str(dollar),
        "--shares-file",
        str(sdir / "Portfolio_20260806.csv"),
        "--ds2",
        str(h5),
    )
    assert rc == 0
    assert payload["passed"] == "with warnings"
    assert payload["shares"]["names_dropped"] == 2
    assert "SHARES_NAMES_DROPPED" in payload["warning_codes"]
    assert payload["shares"]["churn"] is not None
    assert Decimal(payload["shares"]["churn"]) < Decimal("0.25")


def test_gate_infra_error_exits_1_with_json(tmp_path, capsys):
    rc, payload = run_gate_cli(
        tmp_path, capsys, "--dollar-file", str(tmp_path / "does_not_exist.csv")
    )
    assert rc == 1
    assert payload["passed"] is False
    assert payload["error_type"] == "infra"
    assert "does_not_exist.csv" in payload["error"]
    assert payload["ki_ops_version"] == "0.2.0"


def test_gate_zero_gmv_blocks(tmp_path, capsys):
    dollar = tmp_path / "Portfolio_20260806.csv"
    dollar.write_text("SecurityID,$_value\n101,0\n")
    rc, payload = run_gate_cli(tmp_path, capsys, "--dollar-file", str(dollar))
    assert rc == 2
    assert "ZERO_GMV" in payload["violation_codes"]
