"""Email + Slack delivery for perturb stdout (no extra package deps)."""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from email.message import EmailMessage
from pathlib import Path
from typing import Mapping, Sequence

from ki_ops.config import REPO_ROOT

SLACK_TEXT_LIMIT = 39_000

ENV_SMTP_HOST = "KI_OPS_SMTP_HOST"
ENV_SMTP_PORT = "KI_OPS_SMTP_PORT"
ENV_SMTP_USER = "KI_OPS_SMTP_USER"
ENV_SMTP_PASSWORD = "KI_OPS_SMTP_PASSWORD"
ENV_SMTP_FROM = "KI_OPS_SMTP_FROM"
ENV_EMAIL_TO = "KI_OPS_EMAIL_TO"
ENV_SMTP_STARTTLS = "KI_OPS_SMTP_STARTTLS"
ENV_SLACK_WEBHOOK = "KI_OPS_SLACK_WEBHOOK_URL"
ENV_EMAIL_TRANSPORT = "KI_OPS_EMAIL_TRANSPORT"
ENV_NOTIFY_ENV_FILE = "KI_OPS_NOTIFY_ENV_FILE"

DEFAULT_NOTIFY_ENV = REPO_ROOT / "config" / "notify.env"

DEFAULT_EMAIL_TO = "robert@kelaitech.com"
DEFAULT_EMAIL_FROM = "robert@kelaitech.com"


@dataclass(frozen=True)
class NotifySettings:
    smtp_host: str | None
    smtp_port: int
    smtp_user: str | None
    smtp_password: str | None
    smtp_from: str | None
    email_to: tuple[str, ...]
    smtp_starttls: bool
    slack_webhook_url: str | None
    email_transport: str

    @property
    def email_enabled(self) -> bool:
        if not self.email_to:
            return False
        if self.email_transport == "smtp":
            return bool(self.smtp_host and self.smtp_from)
        if self.email_transport in {"sendmail", "mail.app"}:
            return True
        return False

    @property
    def slack_enabled(self) -> bool:
        return bool(self.slack_webhook_url)


def _resolve_transport(src: Mapping[str, str], smtp_host: str | None) -> str:
    raw = (src.get(ENV_EMAIL_TRANSPORT) or "").strip().lower()
    if raw:
        return raw
    if smtp_host:
        return "smtp"
    if sys.platform == "darwin":
        return "mail.app"
    return "sendmail"


def load_dotenv_file(path: str | Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file (``#`` comments, no export prefix)."""
    out: dict[str, str] = {}
    p = Path(path)
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].strip()
        if "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key:
            out[key] = val
    return out


def notify_env_candidates(explicit: str | Path | None = None) -> list[Path]:
    if explicit:
        return [Path(explicit)]
    from_env = (os.environ.get(ENV_NOTIFY_ENV_FILE) or "").strip()
    if from_env:
        return [Path(from_env)]
    return [Path.cwd() / "config" / DEFAULT_NOTIFY_ENV.name, DEFAULT_NOTIFY_ENV]


def merged_notify_env(
    env: Mapping[str, str] | None = None,
    *,
    env_file: str | Path | None = None,
) -> dict[str, str]:
    """File values first; process env overrides (so ``export KI_OPS_*`` wins)."""
    merged: dict[str, str] = {}
    for candidate in notify_env_candidates(env_file):
        if candidate.is_file():
            merged.update(load_dotenv_file(candidate))
            break
    merged.update(dict(env if env is not None else os.environ))
    return merged


def load_notify_settings(
    env: Mapping[str, str] | None = None,
    *,
    env_file: str | Path | None = None,
) -> NotifySettings:
    src = merged_notify_env(env, env_file=env_file)
    to_raw = (src.get(ENV_EMAIL_TO) or DEFAULT_EMAIL_TO).strip()
    to = tuple(p.strip() for p in to_raw.split(",") if p.strip())
    port_raw = (src.get(ENV_SMTP_PORT) or "587").strip() or "587"
    starttls_raw = (src.get(ENV_SMTP_STARTTLS) or "1").strip().lower()
    smtp_host = (src.get(ENV_SMTP_HOST) or "").strip() or None
    return NotifySettings(
        smtp_host=smtp_host,
        smtp_port=int(port_raw),
        smtp_user=(src.get(ENV_SMTP_USER) or "").strip() or None,
        smtp_password=src.get(ENV_SMTP_PASSWORD) or None,
        smtp_from=(src.get(ENV_SMTP_FROM) or DEFAULT_EMAIL_FROM).strip() or DEFAULT_EMAIL_FROM,
        email_to=to,
        smtp_starttls=starttls_raw not in {"0", "false", "no"},
        slack_webhook_url=(src.get(ENV_SLACK_WEBHOOK) or "").strip() or None,
        email_transport=_resolve_transport(src, smtp_host),
    )


def with_recipients(settings: NotifySettings, to_raw: str | None) -> NotifySettings:
    if not to_raw or not to_raw.strip():
        return settings
    to = tuple(p.strip() for p in to_raw.split(",") if p.strip())
    return replace(settings, email_to=to)


def with_sender(settings: NotifySettings, from_raw: str | None) -> NotifySettings:
    if not from_raw or not from_raw.strip():
        return settings
    return replace(settings, smtp_from=from_raw.strip())


def format_combined_stdout(blocks: Sequence[tuple[str, int, str]]) -> str:
    parts: list[str] = []
    for command, rc, stdout in blocks:
        parts.append(f"===== {command}  exit={rc} =====\n{stdout.rstrip()}\n")
    return "\n".join(parts).rstrip() + "\n"


def slack_text(body: str) -> str:
    if len(body) <= SLACK_TEXT_LIMIT:
        return body
    keep = SLACK_TEXT_LIMIT - 80
    return body[:keep] + "\n… truncated for Slack; full JSON is in the email.\n"


def format_slack_message(*, subject: str, body: str) -> str:
    return f"*{subject}*\n```\n{slack_text(body).rstrip()}\n```"


def _apple_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def send_via_mail_app(
    *, to: Sequence[str], subject: str, body: str, sender: str
) -> None:
    """Send via Mail.app using ``sender``, which must be an added Mail account."""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as fh:
        fh.write(body)
        body_path = fh.name
    recipients = "\n".join(
        f'    make new to recipient at end of to recipients with properties {{address:"{_apple_quote(addr)}"}}'
        for addr in to
    )
    wanted = _apple_quote(sender)
    script = f'''
set wanted to "{wanted}"
set bodyText to read POSIX file "{_apple_quote(body_path)}" as «class utf8»
tell application "Mail"
  set matched to false
  repeat with acct in accounts
    try
      set addrs to email addresses of acct
      if addrs is missing value then
      else if class of addrs is list then
        if addrs contains wanted then set matched to true
      else
        if (addrs as string) is wanted then set matched to true
      end if
    end try
  end repeat
  if matched is false then error "no Mail.app account for " & wanted & " (only other accounts are configured). Add this address in Mail → Settings → Accounts, then retry."
  set msg to make new outgoing message with properties {{subject:"{_apple_quote(subject)}", sender:wanted, visible:false}}
  tell msg
    set content to bodyText
    set sender to wanted
{recipients}
  end tell
  send msg
end tell
'''
    try:
        proc = subprocess.run(
            ["osascript", "-"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"Mail.app send failed: {err or proc.returncode}")
    finally:
        try:
            os.unlink(body_path)
        except OSError:
            pass


def send_via_sendmail(*, settings: NotifySettings, subject: str, body: str) -> None:
    from_addr = settings.smtp_from or DEFAULT_EMAIL_FROM
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(settings.email_to)
    msg.set_content(body)
    proc = subprocess.run(
        ["/usr/sbin/sendmail", "-t", "-oi"],
        input=bytes(msg),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"sendmail failed: {err or proc.returncode}")


def send_via_smtp(*, settings: NotifySettings, subject: str, body: str) -> None:
    if not settings.smtp_host or not settings.smtp_from:
        raise RuntimeError(f"SMTP needs {ENV_SMTP_HOST} and {ENV_SMTP_FROM}")
    if settings.smtp_user and not (settings.smtp_password or "").strip():
        raise RuntimeError(
            f"{ENV_SMTP_PASSWORD} is empty in config/notify.env — set a Google App Password and save the file"
        )
    password = (settings.smtp_password or "").replace(" ", "")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from
    msg["To"] = ", ".join(settings.email_to)
    msg.set_content(body)
    if settings.smtp_port == 465:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context) as smtp:
            if settings.smtp_user:
                smtp.login(settings.smtp_user, password)
            smtp.send_message(msg)
        return
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=60) as smtp:
        if settings.smtp_starttls:
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_user:
            smtp.login(settings.smtp_user, password)
        smtp.send_message(msg)


def send_email(*, settings: NotifySettings, subject: str, body: str) -> None:
    if not settings.email_enabled:
        raise RuntimeError(
            "email not configured: set KI_OPS_EMAIL_TO (or --to), and either "
            f"{ENV_SMTP_HOST}+{ENV_SMTP_FROM}, or use Mail.app (macOS) / sendmail"
        )
    if settings.email_transport == "mail.app":
        send_via_mail_app(
            to=settings.email_to,
            subject=subject,
            body=body,
            sender=settings.smtp_from or DEFAULT_EMAIL_FROM,
        )
        return
    if settings.email_transport == "sendmail":
        send_via_sendmail(settings=settings, subject=subject, body=body)
        return
    send_via_smtp(settings=settings, subject=subject, body=body)


def send_slack(*, webhook_url: str, subject: str, body: str) -> None:
    payload = json.dumps({"text": format_slack_message(subject=subject, body=body)}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Slack webhook HTTP {exc.code}: {detail}") from exc


def dispatch(
    *,
    settings: NotifySettings,
    subject: str,
    body: str,
    send_email_ok: bool = True,
    send_slack_ok: bool = True,
    dry_run: bool = False,
) -> dict[str, bool]:
    """Send to whichever channels are requested and configured.

    Slack is skipped (not an error) when no webhook is set.
    """
    sent = {"email": False, "slack": False}
    want_email = send_email_ok
    want_slack = send_slack_ok and settings.slack_enabled
    if dry_run:
        return sent
    if not want_email and not want_slack:
        raise RuntimeError("nothing to send: email disabled and Slack is not configured")
    if want_email:
        if not settings.email_enabled:
            raise RuntimeError(
                "email requested but not configured "
                f"({ENV_SMTP_HOST} / {ENV_SMTP_FROM} / {ENV_EMAIL_TO}, or Mail.app on macOS)"
            )
        send_email(settings=settings, subject=subject, body=body)
        sent["email"] = True
    if want_slack:
        send_slack(webhook_url=settings.slack_webhook_url or "", subject=subject, body=body)
        sent["slack"] = True
    return sent


def describe_settings(settings: NotifySettings) -> dict[str, object]:
    """Non-secret summary for ``notify-config`` / debugging."""
    return {
        "email_transport": settings.email_transport,
        "email_enabled": settings.email_enabled,
        "smtp_host": settings.smtp_host,
        "smtp_port": settings.smtp_port,
        "smtp_user": settings.smtp_user,
        "smtp_from": settings.smtp_from,
        "email_to": list(settings.email_to),
        "smtp_password_set": bool(settings.smtp_password),
        "slack_enabled": settings.slack_enabled,
        "slack_webhook_set": bool(settings.slack_webhook_url),
    }
