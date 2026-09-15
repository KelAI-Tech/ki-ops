"""Ops environment presets: ``canary`` vs ``prod`` (``KI_OPS_ENV``, default canary).

The kelaidata stack isolates canary from prod by namespace, not by a single
flag (see ``kelaidata/infra/mwaa/startup-{canary,prod}.sh``):

- portfolio artifacts: ``s3://kelaitrading/portfolio_canary`` vs
  ``s3://kelaitrading/portfolio``
- KOTL ledger MySQL: Secrets Manager ``kelai/kotl/db-canary`` vs
  ``kelai/kotl/db-prod`` (schema ``kotl`` in both)
- Flex OMS: ``UAT`` vs ``PROD``

These presets bundle that mapping for the env-aware ops commands
(``ki-ops strat …``, ``ki-ops kotl fills``). Precedence everywhere:
explicit CLI flags > existing env vars (``KOTL_DB_*``, ``KOTL_FLEX_ENV``) >
preset. The gate's ``--env prod|dev`` axis is a different, older knob and is
deliberately untouched.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

ENV_VAR = "KI_OPS_ENV"
DEFAULT_ENV = "canary"


@dataclass(frozen=True)
class OpsEnv:
    """One trading environment's namespace bundle."""

    name: str
    portfolio_root: str
    kotl_db_secret: str
    kotl_db_schema: str
    flex_env: str


OPS_ENVS: dict[str, OpsEnv] = {
    "canary": OpsEnv(
        name="canary",
        portfolio_root="s3://kelaitrading/portfolio_canary",
        kotl_db_secret="kelai/kotl/db-canary",
        kotl_db_schema="kotl",
        flex_env="UAT",
    ),
    "prod": OpsEnv(
        name="prod",
        portfolio_root="s3://kelaitrading/portfolio",
        kotl_db_secret="kelai/kotl/db-prod",
        kotl_db_schema="kotl",
        flex_env="PROD",
    ),
}


def resolve_ops_env(name: str | None = None) -> OpsEnv:
    """Explicit *name* (CLI flag) > ``KI_OPS_ENV`` > default ``canary``."""
    value = (name or os.environ.get(ENV_VAR) or DEFAULT_ENV).strip().lower()
    env = OPS_ENVS.get(value)
    if env is None:
        options = ", ".join(sorted(OPS_ENVS))
        raise ValueError(f"unknown ops environment {value!r} (expected one of: {options})")
    return env


def resolve_flex_env(flag: str | None, env: OpsEnv) -> str:
    """Flex environment: ``--flex-env`` flag > ``KOTL_FLEX_ENV`` > preset."""
    value = (flag or os.environ.get("KOTL_FLEX_ENV") or env.flex_env).strip().upper()
    if value not in ("UAT", "PROD"):
        raise ValueError(f"unknown Flex environment {value!r} (expected UAT or PROD)")
    return value
