"""Send the digest via SMTP: local send-only Postfix, or any authenticated relay."""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return default if not value else value in {"1", "true", "yes", "on"}


def send(subject: str, html_body: str, text_body: str) -> None:
    host = os.environ.get("SMTP_HOST", "localhost")
    port = int(os.environ.get("SMTP_PORT", "25"))
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("MAIL_FROM") or (f"mynews <{user}>" if user else None)
    to = os.environ.get("MAIL_TO") or user
    if not sender or not to:
        raise RuntimeError("set MAIL_FROM and MAIL_TO in .env")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1].rstrip(">"))
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    ctx = ssl.create_default_context()
    if port == 465:
        smtp = smtplib.SMTP_SSL(host, port, context=ctx, timeout=60)
    else:
        smtp = smtplib.SMTP(host, port, timeout=60)
    with smtp as s:
        if port != 465 and _flag("SMTP_STARTTLS", default=host not in LOCAL_HOSTS):
            s.starttls(context=ctx)
        if user:
            s.login(user, password)
        s.send_message(msg)
