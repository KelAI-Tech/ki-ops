"""Airflow DAG: intraday KOTL fills refresh — live GetOrderInfo2 → ki-ops db.

Polls Flex for today's orders and upserts fill state (filled / leaves /
status / avg px) into ``kotl_working_orders`` in MySQL; it also backfills
``flex_batch_id`` from the Flex ``batchId`` for rows submitted before the
batch id was captured at submit time. A run with nothing submitted today is
a clean no-op, so the schedule is safe on non-trading days too.

Copy or symlink this file into AIRFLOW_HOME/dags. Airflow is not a ki-ops
dependency — the workers need ``ki-ops[flex,db]`` installed (editable from
the repo) and these env vars:

  KOTL_FLEX_SDK_PATH        local sync of the Brooklyn SDK
  KOTL_FLEX_ENDPOINT / KOTL_FLEX_TOKEN, or the Secrets Manager secret
                            kelai/flextrade/api-token (default lookup)
  KOTL_DB_HOST / KOTL_DB_PORT / KOTL_DB_USER / KOTL_DB_PASSWORD /
  KOTL_DB_SCHEMA, or KOTL_DB_SECRET (Secrets Manager id, e.g. dev/kelaidb)
  KOTL_REFRESH_FLEX_ENV     UAT (default) or PROD

Schedule: every 15 minutes 13:00–21:45 UTC on weekdays — covers the
9:30–16:00 ET session in both DST regimes, plus a post-close sweep so the
final fills land before ``kotl eod``. The trade date is "today" in
America/New_York.
"""

from __future__ import annotations

from datetime import datetime

from airflow import DAG
from airflow.operators.python import PythonOperator


def _refresh_fills() -> None:
    import json
    import os
    from datetime import datetime as dt
    from zoneinfo import ZoneInfo

    from ki_ops.kotl.flex_live import LiveRefreshSource, load_flex_config
    from ki_ops.kotl.mysql_store import MysqlKotlStore
    from ki_ops.kotl.refresh import refresh_working_orders

    flex_env = (os.environ.get("KOTL_REFRESH_FLEX_ENV") or "UAT").upper()
    trade_date = dt.now(ZoneInfo("America/New_York")).date()

    store = MysqlKotlStore.from_env_or_secret(
        db_secret=os.environ.get("KOTL_DB_SECRET") or None
    )
    try:
        stored = store.load_working_orders(trade_date=trade_date)
        if not stored:
            print(f"no working orders for {trade_date.isoformat()} — nothing to refresh")
            return
        source = LiveRefreshSource(load_flex_config(flex_env=flex_env))
        updated = refresh_working_orders(store, trade_date, source)
        print(
            json.dumps(
                {
                    "trade_date": trade_date.isoformat(),
                    "flex_env": flex_env,
                    "stored_count": len(stored),
                    "updated_count": len(updated),
                    "open_count": sum(1 for o in updated if o.leaves_qty != 0),
                    "missing_batch_id": sum(1 for o in updated if not o.flex_batch_id),
                },
                indent=2,
            )
        )
    finally:
        store.close()


with DAG(
    dag_id="ki_ops_kotl_refresh_fills",
    start_date=datetime(2026, 9, 1),
    schedule="*/15 13-21 * * 1-5",
    catchup=False,
    max_active_runs=1,
    tags=["ki-ops", "kotl", "fills"],
    doc_md=__doc__,
) as dag:
    PythonOperator(
        task_id="refresh_fills_mysql",
        python_callable=_refresh_fills,
    )
