"""Fetch article bodies for the few stories that were picked. Zero LLM tokens."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import httpx
import trafilatura

from .ingest import TIMEOUT, USER_AGENT, truncate_words
from .rank import Cluster, Item

MIN_BODY_WORDS = 80  # less than this is usually a paywall or cookie wall


def _fetchable(it: Item) -> bool:
    # Google News links are JS redirects; trafilatura only sees a consent page
    return "news.google.com" not in it.url


def _extract(client: httpx.Client, url: str) -> str | None:
    try:
        resp = client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError:
        return None
    text = trafilatura.extract(resp.text, include_comments=False, include_tables=False)
    if not text or len(text.split()) < MIN_BODY_WORDS:
        return None
    return text


def body_for(client: httpx.Client, items: list[Item], words: int) -> tuple[Item, str] | None:
    """First member (by source weight) with a usable body. Sets item.body when fetched."""
    for it in sorted(items, key=lambda i: -i.weight):
        if not it.body and _fetchable(it):
            it.body = _extract(client, it.url)
            it.fetched = it.body is not None
        if it.body:
            return it, truncate_words(it.body, words)
    return None


def enrich(conn: sqlite3.Connection, groups: list[list[Item]], words: int) -> list[tuple[Item, str] | None]:
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
                      follow_redirects=True) as client:
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda g: body_for(client, g, words), groups))
    for group in groups:  # cache newly fetched bodies (sqlite: main thread only)
        for it in group:
            if it.fetched:
                conn.execute("UPDATE items SET body = ? WHERE id = ?", (it.body, it.id))
    conn.commit()
    return results


def enrich_clusters(conn: sqlite3.Connection, clusters: list[Cluster], words: int):
    return enrich(conn, [c.items for c in clusters], words)
