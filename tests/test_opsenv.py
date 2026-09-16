"""KI_OPS_ENV presets: canary (default) vs prod."""

import pytest

from ki_ops.opsenv import OPS_ENVS, resolve_flex_env, resolve_ops_env


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("KI_OPS_ENV", raising=False)
    monkeypatch.delenv("KOTL_FLEX_ENV", raising=False)


def test_default_is_canary():
    env = resolve_ops_env()
    assert env.name == "canary"
    assert env.portfolio_root == "s3://kelaitrading/portfolio_canary"
    assert env.kotl_db_secret == "kelai/kotl/db-canary"
    assert env.kotl_db_schema == "kotl"
    assert env.flex_env == "UAT"


def test_env_var_selects_prod(monkeypatch):
    monkeypatch.setenv("KI_OPS_ENV", "prod")
    env = resolve_ops_env()
    assert env.name == "prod"
    assert env.portfolio_root == "s3://kelaitrading/portfolio"
    assert env.kotl_db_secret == "kelai/kotl/db-prod"
    assert env.flex_env == "PROD"


def test_explicit_name_beats_env_var(monkeypatch):
    monkeypatch.setenv("KI_OPS_ENV", "prod")
    assert resolve_ops_env("canary").name == "canary"


def test_name_is_case_insensitive():
    assert resolve_ops_env("PROD").name == "prod"
    assert resolve_ops_env("  Canary ").name == "canary"


def test_unknown_env_raises():
    with pytest.raises(ValueError, match="unknown ops environment"):
        resolve_ops_env("staging")


def test_unknown_env_var_raises(monkeypatch):
    monkeypatch.setenv("KI_OPS_ENV", "dev")
    with pytest.raises(ValueError, match="unknown ops environment"):
        resolve_ops_env()


def test_flex_env_defaults_to_preset():
    assert resolve_flex_env(None, OPS_ENVS["canary"]) == "UAT"
    assert resolve_flex_env(None, OPS_ENVS["prod"]) == "PROD"


def test_flex_env_kotl_var_beats_preset(monkeypatch):
    monkeypatch.setenv("KOTL_FLEX_ENV", "PROD")
    assert resolve_flex_env(None, OPS_ENVS["canary"]) == "PROD"


def test_flex_env_flag_beats_everything(monkeypatch):
    monkeypatch.setenv("KOTL_FLEX_ENV", "PROD")
    assert resolve_flex_env("UAT", OPS_ENVS["prod"]) == "UAT"


def test_flex_env_invalid_raises():
    with pytest.raises(ValueError, match="unknown Flex environment"):
        resolve_flex_env("FAKE", OPS_ENVS["canary"])
