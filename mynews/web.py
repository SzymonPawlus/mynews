"""Tiny feedback endpoint for the one-click links in the email.

Runs on localhost behind a TLS reverse proxy (Caddy). Only links signed with
MYNEWS_FEEDBACK_SECRET are accepted, so no login is needed.

  GET  /fb?d=DATE&r=REF&a=ACTION[&c=CONCEPT]&s=SIG[&undo=1]   record / undo a click
  POST /fb/comment  (d, r, s, text)                             free-text comment
"""

from __future__ import annotations

import os
import sys
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from . import feedback
from .db import connect

LABELS = {
    "up": "👍 More stories like this",
    "down": "👎 Fewer stories like this",
    "more": "🔍 A deeper follow-up will be in your next briefing",
    "got": "✓ Marked as understood",
}

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mynews feedback</title>
<style>
  :root {{ --text:#1f2328; --muted:#59636e; --accent:#0b5cad; --bg:#ffffff; --soft:#f3f5f8; --line:#d0d7de; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --text:#e6edf3; --muted:#9198a1; --accent:#58a6ff; --bg:#0d1117; --soft:#161b22; --line:#30363d; }}
  }}
  body {{ margin:0; background:var(--bg); color:var(--text);
         font:16px/1.5 -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }}
  main {{ max-width:560px; margin:0 auto; padding:28px 16px; }}
  .done {{ font-size:20px; margin:0 0 6px; }}
  .about {{ color:var(--muted); margin:0 0 18px; }}
  a {{ color:var(--accent); }}
  textarea {{ width:100%; box-sizing:border-box; min-height:110px; padding:10px; font:inherit;
             color:var(--text); background:var(--soft); border:1px solid var(--line); border-radius:8px; }}
  button {{ margin-top:10px; padding:9px 18px; font:inherit; color:#fff; background:var(--accent);
           border:0; border-radius:8px; cursor:pointer; }}
  .small {{ font-size:14px; color:var(--muted); }}
</style></head>
<body><main>{body}</main></body></html>"""


def _page(body: str, status: int = 200) -> tuple[int, str]:
    return status, PAGE.format(body=body)


def _comment_form(day: str, ref: str, prompt: str) -> str:
    sig = feedback.sign(day, ref, "comment")
    return (f'<form method="post" action="/fb/comment">'
            f'<input type="hidden" name="d" value="{escape(day)}">'
            f'<input type="hidden" name="r" value="{escape(ref)}">'
            f'<input type="hidden" name="s" value="{sig}">'
            f'<p class="small">{prompt}</p>'
            f'<textarea name="text" placeholder="e.g. I care about the Polish angle of stories like this"></textarea>'
            f'<button type="submit">Send</button></form>')


class Handler(BaseHTTPRequestHandler):
    server_version = "mynews"

    def _send(self, status: int, html: str) -> None:
        data = html.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # one line per request on stderr (journald)
        sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/health":
            return self._send(200, "ok")
        if url.path != "/fb":
            return self._send(*_page("<p>Not found.</p>", 404))
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        day, ref, action = q.get("d", ""), q.get("r", ""), q.get("a", "")
        concept, sig = q.get("c", ""), q.get("s", "")
        if action not in feedback.CLICK_KINDS or not feedback.verify(day, ref, action, concept, sig):
            return self._send(*_page("<p>This link is invalid or has expired.</p>", 403))

        with connect() as conn:
            st = feedback.story(conn, day, ref)
            if q.get("undo"):
                feedback.undo_click(conn, day, ref, action, concept)
                done = "Undone."
            else:
                feedback.record_click(conn, day, ref, action, concept)
                done = LABELS[action]
        if action == "got" and concept:
            about = escape(concept)
        else:
            about = f"“{escape(st['headline'])}”" if st else ""
        undo_q = urlencode({**q, "undo": 1})
        body = (f'<p class="done">{done}</p>'
                + (f'<p class="about">{about}</p>' if about else "")
                + ("" if q.get("undo") else f'<p class="small"><a href="/fb?{undo_q}">Undo</a></p>')
                + _comment_form(day, ref, "Anything to add? It will be applied in your next briefing."))
        self._send(*_page(body))

    def do_POST(self):
        if urlsplit(self.path).path != "/fb/comment":
            return self._send(*_page("<p>Not found.</p>", 404))
        length = min(int(self.headers.get("Content-Length") or 0), 16_000)
        q = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode(errors="replace")).items()}
        day, ref, sig = q.get("d", ""), q.get("r", ""), q.get("s", "")
        if not feedback.verify(day, ref, "comment", "", sig):
            return self._send(*_page("<p>This form is invalid or has expired.</p>", 403))
        with connect() as conn:
            feedback.record_comment(conn, q.get("text", ""), "web", day, ref)
        self._send(*_page('<p class="done">Thanks, noted.</p>'
                          '<p class="about">It will be taken into account in your next briefing.</p>'))


def serve(host: str = "127.0.0.1", port: int | None = None) -> None:
    if not feedback._secret():
        raise SystemExit("MYNEWS_FEEDBACK_SECRET is not set")
    port = port or int(os.environ.get("MYNEWS_WEB_PORT", "8787"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"mynews feedback endpoint on http://{host}:{port}", file=sys.stderr)
    httpd.serve_forever()
