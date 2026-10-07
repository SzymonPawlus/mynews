"""Receive an email reply (piped in by Postfix) and store it as a feedback comment.

Accepted only when the From address is the reader's (MAIL_TO, or MYNEWS_FEEDBACK_FROM)
AND the local OpenDKIM verifier recorded a DKIM pass for that sender's domain, so a
forged From header is not enough. Anything else is silently dropped (no bounce).
"""

from __future__ import annotations

import email
import os
import re
import sys
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr

from . import feedback
from .db import connect

QUOTE_MARKERS = [
    re.compile(r"^On .{5,200}wrote:\s*$"),           # Gmail / Apple Mail (EN)
    re.compile(r"^W dniu .{5,200}(pisze|napisał\w*):\s*$"),  # Gmail (PL)
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.I),
    re.compile(r"^-{2,}\s*Oryginalna wiadomość\s*-{2,}", re.I),
    re.compile(r"^From: .+"),                        # Outlook-style quoted header block
    re.compile(r"^_{10,}$"),
]
DIGEST_DATE = re.compile(r"(\d{1,2}) (January|February|March|April|May|June|July|August|"
                         r"September|October|November|December) (\d{4})")
MONTHS = {m: i for i, m in enumerate(
    "January February March April May June July August September October November December".split(), 1)}


def allowed_senders() -> set[str]:
    raw = os.environ.get("MYNEWS_FEEDBACK_FROM") or os.environ.get("MAIL_TO", "")
    return {parseaddr(a)[1].lower() for a in raw.split(",") if parseaddr(a)[1]}


def authserv_id() -> str:
    """Our verifier's id: OpenDKIM is configured with AuthservID = the sending domain."""
    explicit = os.environ.get("MYNEWS_AUTHSERV_ID", "").strip()
    return explicit or parseaddr(os.environ.get("MAIL_FROM", ""))[1].rsplit("@", 1)[-1].lower()


def dkim_ok(msg: EmailMessage, sender: str) -> bool:
    """True if OUR OpenDKIM verifier recorded dkim=pass for the sender's domain.

    Only the topmost Authentication-Results header carrying our authserv-id is trusted:
    OpenDKIM prepends its own and strips incoming ones that claim our id, so a forger
    cannot inject a fake "dkim=pass".
    """
    ours = authserv_id()
    domain = sender.rsplit("@", 1)[-1]
    for header in msg.get_all("Authentication-Results", []):
        clean = re.sub(r"\([^)]*\)", "", str(header))  # drop comments like "(2048-bit key; ...)"
        if not ours or clean.split(";", 1)[0].strip().lower() != ours:
            continue
        for m in re.finditer(r"dkim=pass\b[^;]*?header\.d=([\w.-]+)", clean, re.I):
            d = m.group(1).lower()
            if domain == d or domain.endswith("." + d):
                return True
        return False  # topmost header with our id decides
    return False


def reply_text(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain",))
    if part is None:
        part = msg.get_body(preferencelist=("html",))
        if part is None:
            return ""
        text = re.sub(r"<[^>]+>", " ", part.get_content())
    else:
        text = part.get_content()
    keep = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(">") or any(p.match(s) for p in QUOTE_MARKERS):
            break
        keep.append(line)
    return "\n".join(keep).strip()


def digest_date(subject: str) -> str:
    m = DIGEST_DATE.search(subject or "")
    if not m:
        return ""
    return f"{int(m.group(3)):04d}-{MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"


def handle(raw: bytes) -> str:
    msg = email.message_from_bytes(raw, policy=policy.default)
    sender = parseaddr(str(msg.get("From", "")))[1].lower()
    if sender not in allowed_senders():
        return f"dropped: sender {sender!r} not allowed"
    if os.environ.get("MYNEWS_INBOUND_REQUIRE_DKIM", "true").lower() != "false" and not dkim_ok(msg, sender):
        return f"dropped: no DKIM pass for {sender}"
    text = reply_text(msg)
    if not text:
        return "dropped: empty reply"
    with connect() as conn:
        feedback.record_comment(conn, text, "email", digest_date(str(msg.get("Subject", ""))))
    return f"stored feedback from {sender} ({len(text)} chars)"


def main() -> int:
    try:
        print(handle(sys.stdin.buffer.read()), file=sys.stderr)
    except Exception as e:  # never bounce: a bounce would leak information to spoofers
        print(f"inbound error: {e}", file=sys.stderr)
    return 0
