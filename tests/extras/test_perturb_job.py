"""POC perturb notify job."""

from __future__ import annotations

import json
from unittest.mock import patch

from ki_ops.cli import main
from ki_ops.extras.notify import (
    ENV_EMAIL_TO,
    ENV_SLACK_WEBHOOK,
    ENV_SMTP_FROM,
    ENV_SMTP_HOST,
    load_notify_settings,
)
from ki_ops.extras.perturb_job import run_and_notify_perturbs, subject_for


def _fake_main(argv):
    cmd = argv[0]
    payload = {"perturb": cmd, "passed": True if "baseline" in cmd else False}
    print(json.dumps(payload, indent=2))
    return 0 if payload["passed"] else 2


def test_run_and_notify_dry_run_captures_stdout():
    summary = run_and_notify_perturbs(dry_run=True, runner=_fake_main)
    assert summary["dry_run"] is True
    assert summary["max_exit_code"] == 2
    assert [c["command"] for c in summary["commands"]] == [
        "run-perturb-baseline",
        "run-perturb-zero",
        "run-perturb-var-checks",
    ]
    assert "baseline=True" in summary["subject"]


def test_run_and_notify_sends_both_channels():
    cfg = load_notify_settings(
        {
            ENV_SMTP_HOST: "smtp.example.com",
            ENV_SMTP_FROM: "ops@example.com",
            ENV_EMAIL_TO: "desk@example.com",
            ENV_SLACK_WEBHOOK: "https://hooks.slack.com/services/T/B/xxx",
        }
    )
    with patch("ki_ops.extras.perturb_job.dispatch") as disp:
        disp.return_value = {"email": True, "slack": True}
        summary = run_and_notify_perturbs(settings=cfg, runner=_fake_main)
    disp.assert_called_once()
    body = disp.call_args.kwargs["body"]
    assert "run-perturb-baseline" in body
    assert "run-perturb-var-checks" in body
    assert summary["sent"]["email"] is True


def test_subject_for_reads_passed():
    blocks = [("run-perturb-baseline", 0, json.dumps({"passed": "with warnings"}))]
    assert "baseline=with warnings (exit 0)" in subject_for(blocks)


def test_cli_notify_perturbs_dry_run():
    with patch("ki_ops.extras.perturb_job.run_three_perturbs") as run:
        run.return_value = [
            ("run-perturb-baseline", 0, '{"passed": true}\n'),
            ("run-perturb-zero", 0, '{"passed": true}\n'),
            ("run-perturb-var-checks", 2, '{"passed": false}\n'),
        ]
        rc = main(["extras", "notify-perturbs", "--dry-run"])
    assert rc == 0


def test_cli_test_email_skips_perturbs():
    with patch("ki_ops.extras.perturb_job.dispatch") as disp:
        disp.return_value = {"email": True, "slack": False}
        with patch("ki_ops.extras.perturb_job.run_three_perturbs") as run:
            rc = main(["extras", "notify-perturbs", "--test-email", "--to", "robert@kelaitech.com"])
    run.assert_not_called()
    assert rc == 0
    assert disp.call_args.kwargs["subject"] == "ki-ops email test"
    assert disp.call_args.kwargs["body"] == "ki-ops email test.\n"
