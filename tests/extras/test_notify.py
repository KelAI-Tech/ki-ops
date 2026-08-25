"""Email / Slack notify helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from ki_ops.extras.notify import (
    ENV_EMAIL_TO,
    ENV_SLACK_WEBHOOK,
    ENV_SMTP_FROM,
    ENV_SMTP_HOST,
    dispatch,
    format_combined_stdout,
    load_notify_settings,
    slack_text,
    SLACK_TEXT_LIMIT,
)


def test_load_notify_settings_parses_recipients():
    cfg = load_notify_settings(
        {
            ENV_SMTP_HOST: "smtp.example.com",
            ENV_SMTP_FROM: "ops@example.com",
            ENV_EMAIL_TO: "a@x.com, b@x.com",
            ENV_SLACK_WEBHOOK: "https://hooks.slack.com/services/T/B/xxx",
        }
    )
    assert cfg.email_enabled
    assert cfg.slack_enabled
    assert cfg.email_to == ("a@x.com", "b@x.com")
    assert cfg.email_transport == "smtp"


def test_load_notify_settings_defaults_to_robert():
    cfg = load_notify_settings({"KI_OPS_EMAIL_TRANSPORT": "mail.app"})
    assert cfg.email_to == ("robert@kelaitech.com",)
    assert cfg.smtp_from == "robert@kelaitech.com"
    assert cfg.email_enabled


def test_slack_text_truncates():
    body = "x" * (SLACK_TEXT_LIMIT + 50)
    out = slack_text(body)
    assert len(out) <= SLACK_TEXT_LIMIT
    assert "truncated" in out


def test_format_combined_stdout():
    text = format_combined_stdout([("run-perturb-baseline", 0, '{"passed": true}\n')])
    assert "exit=0" in text
    assert '"passed": true' in text


def test_dispatch_dry_run_does_not_send():
    cfg = load_notify_settings({})
    sent = dispatch(
        settings=cfg,
        subject="t",
        body="b",
        dry_run=True,
    )
    assert sent == {"email": False, "slack": False}


def test_dispatch_email_and_slack(monkeypatch):
    cfg = load_notify_settings(
        {
            ENV_SMTP_HOST: "smtp.example.com",
            ENV_SMTP_FROM: "ops@example.com",
            ENV_EMAIL_TO: "desk@example.com",
            ENV_SLACK_WEBHOOK: "https://hooks.slack.com/services/T/B/xxx",
        }
    )
    with patch("ki_ops.extras.notify.send_email") as email, patch("ki_ops.extras.notify.send_slack") as slack:
        sent = dispatch(settings=cfg, subject="sub", body="body")
    email.assert_called_once()
    slack.assert_called_once()
    assert sent == {"email": True, "slack": True}


def test_dispatch_email_only_skips_slack():
    cfg = load_notify_settings(
        {
            ENV_SMTP_HOST: "smtp.example.com",
            ENV_SMTP_FROM: "ops@example.com",
            ENV_EMAIL_TO: "desk@example.com",
        }
    )
    with patch("ki_ops.extras.notify.send_email") as email, patch("ki_ops.extras.notify.send_slack") as slack:
        sent = dispatch(settings=cfg, subject="sub", body="body")
    email.assert_called_once()
    slack.assert_not_called()
    assert sent == {"email": True, "slack": False}


def test_dispatch_requires_email_config():
    cfg = load_notify_settings({"KI_OPS_EMAIL_TRANSPORT": "smtp", ENV_EMAIL_TO: ""})
    with pytest.raises(RuntimeError, match="email requested"):
        dispatch(settings=cfg, subject="s", body="b", send_slack_ok=False)
