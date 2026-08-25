"""Run the three POC perturbs and email + Slack the JSON stdout."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from typing import Callable, Sequence

from ki_ops.cli import main as cli_main
from ki_ops.extras.notify import (
    NotifySettings,
    dispatch,
    format_combined_stdout,
    load_notify_settings,
    with_recipients,
    with_sender,
)

PERTURB_COMMANDS: tuple[str, ...] = (
    "run-perturb-baseline",
    "run-perturb-zero",
    "run-perturb-var-checks",
)


def capture_cli(argv: Sequence[str], *, runner: Callable[..., int] | None = None) -> tuple[int, str]:
    fn = runner or cli_main
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = int(fn(list(argv)))
    return rc, buf.getvalue()


def run_three_perturbs(*, runner: Callable[..., int] | None = None) -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    for command in PERTURB_COMMANDS:
        rc, stdout = capture_cli([command], runner=runner)
        out.append((command, rc, stdout))
    return out


def subject_for(blocks: Sequence[tuple[str, int, str]]) -> str:
    import json

    bits = []
    for command, rc, stdout in blocks:
        passed = "?"
        try:
            payload = json.loads(stdout)
            passed = str(payload.get("passed", "?"))
        except json.JSONDecodeError:
            pass
        short = command.replace("run-perturb-", "")
        bits.append(f"{short}={passed} (exit {rc})")
    return "ki-ops perturbs: " + "; ".join(bits)


def run_and_notify_perturbs(
    *,
    settings: NotifySettings | None = None,
    send_email: bool = True,
    send_slack: bool = True,
    dry_run: bool = False,
    test_email: bool = False,
    to: str | None = None,
    sender: str | None = None,
    runner: Callable[..., int] | None = None,
) -> dict:
    cfg = settings if settings is not None else load_notify_settings()
    cfg = with_recipients(cfg, to)
    cfg = with_sender(cfg, sender)
    if test_email:
        blocks: list[tuple[str, int, str]] = []
        body = "ki-ops email test.\n"
        subject = "ki-ops email test"
    else:
        blocks = run_three_perturbs(runner=runner)
        body = format_combined_stdout(blocks)
        subject = subject_for(blocks)
    sent = dispatch(
        settings=cfg,
        subject=subject,
        body=body,
        send_email_ok=send_email,
        send_slack_ok=send_slack,
        dry_run=dry_run,
    )
    max_rc = max((rc for _, rc, _ in blocks), default=0)
    return {
        "subject": subject,
        "sent": sent,
        "dry_run": dry_run,
        "test_email": test_email,
        "to": list(cfg.email_to),
        "from": cfg.smtp_from,
        "email_transport": cfg.email_transport,
        "max_exit_code": max_rc,
        "commands": [{"command": c, "exit": rc} for c, rc, _ in blocks],
    }
