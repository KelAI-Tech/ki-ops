"""Airflow DAG: run the three ki-ops perturbs and email + Slack the stdout.

Copy or symlink this file into AIRFLOW_HOME/dags. Airflow is not a ki-ops
dependency — the workers need ``ki-ops`` installed (editable from the repo)
and these env vars:

  KI_OPS_SMTP_HOST, KI_OPS_SMTP_PORT (default 587), KI_OPS_SMTP_USER,
  KI_OPS_SMTP_PASSWORD, KI_OPS_SMTP_FROM, KI_OPS_EMAIL_TO (comma-separated),
  KI_OPS_SLACK_WEBHOOK_URL

Set the worker cwd to the ki-ops repo root so config/poc_pos_and_px.yaml resolves.
Schedule is weekdays 12:00 UTC — change ``schedule`` to match the desk.
"""

from __future__ import annotations

from datetime import datetime

from airflow import DAG
from airflow.operators.python import PythonOperator


def _run_perturbs_and_notify() -> None:
    from ki_ops.extras.perturb_job import run_and_notify_perturbs

    # var-checks is expected to exit 2 (MAX_TURNOVER). Delivery is the task;
    # codes stay in the email/Slack subject.
    summary = run_and_notify_perturbs()
    print(summary)


with DAG(
    dag_id="ki_ops_poc_perturbs",
    start_date=datetime(2026, 8, 1),
    schedule="0 12 * * 1-5",
    catchup=False,
    tags=["ki-ops", "pre-trade"],
    doc_md=__doc__,
) as dag:
    PythonOperator(
        task_id="run_perturbs_email_slack",
        python_callable=_run_perturbs_and_notify,
    )
