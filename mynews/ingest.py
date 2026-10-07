"""Fetch items from all sources into SQLite. Zero LLM tokens."""

from __future__ import annotations

import html
import os
import re
import sqlite3
import time
from calendar import timegm
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx

from .config import Source
from .db import now_iso

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) mynews/0.1 (personal news digest)"
TIMEOUT = 25.0
SUMMARY_CHARS = 400
BODY_WORDS = 500
RETENTION = timedelta(days=60)  # unused items older than this are pruned

TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|mc_|ref$|cmpid|ocid|at_)", re.I)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


@dataclass
class RawItem:
    url: str
    source: str
    section: str
    weekly: bool
    title: str
    summary: str
    published: str
    body: str | None = None


@dataclass
class SourceResult:
    source: Source
    items: list[RawItem]
    error: str | None = None


def clean_text(text: str | None, limit: int | None = None) -> str:
    if not text:
        return ""
    text = WS_RE.sub(" ", html.unescape(TAG_RE.sub(" ", text))).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def truncate_words(text: str, n: int = BODY_WORDS) -> str:
    words = text.split()
    return " ".join(words[:n]) + (" …" if len(words) > n else "")


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if not TRACKING_PARAMS.match(k)]
    netloc = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower() or "https", netloc, path, urlencode(query), ""))


def _entry_time(entry) -> str:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = entry.get(key)
        if t:
            return datetime.fromtimestamp(timegm(t), timezone.utc).isoformat(timespec="seconds")
    return now_iso()


def _matches(source: Source, title: str, summary: str) -> bool:
    """Acronym keywords ("AI", "GDP") match case-sensitively, everything else doesn't."""
    if any(k.lower() in title.lower() for k in source.exclude):
        return False
    if not source.include:
        return True
    hay = f" {title} {summary} "
    return any(k in hay if k.strip().isupper() else k.lower() in hay.lower()
               for k in source.include)


HREF_RE = re.compile(r'href="(https?://[^"]+)"')


def _original_link(summary_html: str, fallback: str) -> str:
    """Link-blog feeds (Techmeme) link to their own page; the article is the first
    outbound link with a path in the entry body."""
    own = urlsplit(fallback).netloc
    for href in HREF_RE.findall(summary_html):
        parts = urlsplit(href)
        if parts.netloc != own and parts.path.strip("/"):
            return href
    return fallback


def fetch_rss(client: httpx.Client, source: Source) -> list[RawItem]:
    resp = client.get(source.url)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"unparseable feed: {feed.bozo_exception}")
    items = []
    for entry in feed.entries:
        link = entry.get("link")
        title = clean_text(entry.get("title"))
        if not link or not title:
            continue
        raw_summary = entry.get("summary") or entry.get("description") or ""
        summary = clean_text(raw_summary, SUMMARY_CHARS)
        if source.original_link:
            link = _original_link(raw_summary, link)
        if source.name.startswith("gnews"):
            # Google News: "Headline - Publisher"; summary is just a link list
            title, _, publisher = title.rpartition(" - ")
            summary = publisher
        if not _matches(source, title, summary):
            continue
        items.append(RawItem(
            url=canonical_url(link), source=source.name, section=source.section,
            weekly=source.weekly, title=title, summary=summary, published=_entry_time(entry),
        ))
        if source.max_items and len(items) >= source.max_items:
            break
    return items


def fetch_guardian(client: httpx.Client, source: Source) -> list[RawItem]:
    key = os.environ.get("GUARDIAN_API_KEY")
    if not key:
        raise RuntimeError("skipped: GUARDIAN_API_KEY not set")
    resp = client.get("https://content.guardianapis.com/search", params={
        "section": source.query_section, "order-by": "newest", "page-size": 30,
        "show-fields": "trailText,bodyText", "api-key": key,
    })
    resp.raise_for_status()
    items = []
    for r in resp.json()["response"]["results"]:
        fields = r.get("fields", {})
        items.append(RawItem(
            url=canonical_url(r["webUrl"]), source=source.name, section=source.section,
            weekly=source.weekly, title=clean_text(r["webTitle"]),
            summary=clean_text(fields.get("trailText"), SUMMARY_CHARS),
            published=r["webPublicationDate"].replace("Z", "+00:00"),
            body=truncate_words(fields.get("bodyText", "")) or None,
        ))
    return items


# --- Wikipedia "Current events" portal -------------------------------------

WIKI_CATEGORY_SECTION = {
    "business and economy": "economy",
    "science and technology": "science",
    "health and environment": "science",
}
WIKI_LINK = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]")
WIKI_EXT = re.compile(r"\[(https?://\S+)(?: ([^\]]*))?\]")
WIKI_TPL = re.compile(r"\{\{[^}]*\}\}")
WIKI_REF = re.compile(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>", re.S)


def _wiki_plain(text: str) -> str:
    text = WIKI_REF.sub("", text)
    text = WIKI_EXT.sub("", text)
    text = WIKI_TPL.sub("", text)
    text = WIKI_LINK.sub(r"\1", text)
    return clean_text(text.replace("'''", "").replace("''", ""))


def parse_current_events(wikitext: str, day: datetime, source: Source) -> list[RawItem]:
    items: list[RawItem] = []
    category = ""
    parents: dict[int, str] = {}
    for line in wikitext.splitlines():
        stripped = line.strip()
        if stripped.startswith("'''") and not stripped.startswith("*"):
            category = _wiki_plain(stripped).lower().rstrip(":")
            continue
        m = re.match(r"^(\*+)\s*(.*)$", stripped)
        if not m:
            continue
        depth, content = len(m.group(1)), m.group(2)
        text = _wiki_plain(content)
        if not text:
            continue
        ext = WIKI_EXT.search(content)
        if not ext:  # topic line (parent), not an event
            for d in [d for d in parents if d >= depth]:
                del parents[d]
            parents[depth] = text
            continue
        context = " / ".join(parents[d] for d in sorted(parents) if d < depth)
        title = text if len(text) <= 160 else text[:160].rsplit(" ", 1)[0] + "…"
        items.append(RawItem(
            url=canonical_url(ext.group(1)), source=source.name,
            section=WIKI_CATEGORY_SECTION.get(category, "world"), weekly=False,
            title=title, summary=(f"[{context}] " if context else "") + clean_text(text, SUMMARY_CHARS),
            published=day.replace(hour=12).isoformat(timespec="seconds"),
        ))
        for d in [d for d in parents if d >= depth]:
            del parents[d]
    return items


def fetch_wikipedia_current_events(client: httpx.Client, source: Source) -> list[RawItem]:
    items = []
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    for day in (today - timedelta(days=1), today):
        page = f"Portal:Current_events/{day:%Y_%B_}{day.day}"
        resp = client.get("https://en.wikipedia.org/w/api.php", params={
            "action": "parse", "page": page, "prop": "wikitext", "format": "json",
            "formatversion": 2,
        })
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:  # today's page may not exist yet
            continue
        items += parse_current_events(data["parse"]["wikitext"], day, source)
    return items


FETCHERS = {
    "rss": fetch_rss,
    "guardian": fetch_guardian,
    "wikipedia_current_events": fetch_wikipedia_current_events,
}


def fetch_source(client: httpx.Client, source: Source, attempts: int = 2) -> SourceResult:
    error = None
    for attempt in range(attempts):
        try:
            return SourceResult(source, FETCHERS[source.kind](client, source))
        except RuntimeError as e:  # configuration problem, retrying won't help
            return SourceResult(source, [], str(e))
        except Exception as e:  # one dead feed must not kill the run
            error = f"{type(e).__name__}: {e}".splitlines()[0][:300]
            if attempt + 1 < attempts:
                time.sleep(3)
    return SourceResult(source, [], error)


def fetch_all(sources: list[Source]) -> list[SourceResult]:
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
                      follow_redirects=True) as client:
        with ThreadPoolExecutor(max_workers=8) as pool:
            return list(pool.map(lambda s: fetch_source(client, s), sources))


def store(conn: sqlite3.Connection, results: list[SourceResult]) -> int:
    fetched_at = now_iso()
    new = 0
    for res in results:
        for it in res.items:
            cur = conn.execute(
                """INSERT OR IGNORE INTO items
                   (url, source, section, weekly, title, summary, published, fetched_at, body)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (it.url, it.source, it.section, int(it.weekly), it.title, it.summary,
                 it.published, fetched_at, it.body),
            )
            new += cur.rowcount
        conn.execute(
            "INSERT INTO source_runs (source, ran_at, ok, n_items, error) VALUES (?, ?, ?, ?, ?)",
            (res.source.name, fetched_at, int(res.error is None), len(res.items), res.error),
        )
    conn.execute(
        "DELETE FROM items WHERE published < ? AND id NOT IN (SELECT item_id FROM used_items)",
        ((datetime.now(timezone.utc) - RETENTION).isoformat(),))
    conn.commit()
    return new
