"""Cluster duplicate stories and rank them. Zero LLM tokens.

Main importance signal: how many independent sources carry the same story.
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
from rapidfuzz import fuzz, process

from .config import SECTIONS, Source

DAILY_WINDOW = timedelta(hours=36)
WEEKLY_WINDOW = timedelta(days=7)
NOVELTY_WINDOW = timedelta(days=7)

SIMILARITY = 72        # token_set_ratio threshold for "same story"
MIN_SHARED_TOKENS = 3  # guards token_set_ratio against short-title subsets
REPEAT_SIMILARITY = 80 # vs. titles already sent in previous digests
REPEAT_PENALTY = 0.5
MAX_SINGLE_PER_SOURCE = 3

STOPWORDS = set("""
a an the and or but of to in on at for from by with as is are was were be been being it its
this that these those after before over under about into than then new says said will would
could can may might has have had not no up out more most how why what who when where which
amid via vs i z w na do że się nie jest o po od za dla jak co to ze
""".split())
TOKEN_RE = re.compile(r"[\w$€£%.-]+", re.U)


def tokens(title: str) -> list[str]:
    return [t for t in (m.strip(".-").lower() for m in TOKEN_RE.findall(title))
            if len(t) > 1 and t not in STOPWORDS]


@dataclass
class Item:
    id: int
    url: str
    source: str
    section: str
    title: str
    summary: str
    published: datetime
    weight: float
    body: str | None = None
    fetched: bool = False  # body fetched during this run, not yet cached


@dataclass
class Cluster:
    items: list[Item]
    score: float = 0.0
    repeat: bool = False      # resembles a story already covered recently
    section: str = ""

    @property
    def lead(self) -> Item:
        return max(self.items, key=lambda i: (i.weight, len(i.summary)))

    @property
    def sources(self) -> list[str]:
        """Independent sources. A Google News item repeating another member's exact
        headline is the same article syndicated, not extra coverage."""
        own = {i.title.lower() for i in self.items if not i.source.startswith("gnews")}
        return sorted({i.source for i in self.items
                       if not (i.source.startswith("gnews") and i.title.lower() in own)})

    @property
    def has_text(self) -> bool:
        """Whether enrichment can likely get article text (Google News links can't be fetched)."""
        return any(i.body or "news.google.com" not in i.url for i in self.items)

    @property
    def newest(self) -> datetime:
        return max(i.published for i in self.items)


@dataclass
class Ranking:
    sections: dict[str, list[Cluster]] = field(default_factory=dict)
    long_reads: list[Item] = field(default_factory=list)


def _load(conn: sqlite3.Connection, weights: dict[str, float], now: datetime) -> tuple[list[Item], list[Item]]:
    rows = conn.execute(
        """SELECT * FROM items
           WHERE published >= ? AND id NOT IN (SELECT item_id FROM used_items)""",
        ((now - WEEKLY_WINDOW).isoformat(),),
    ).fetchall()
    daily, weekly = [], []
    for r in rows:
        if r["source"] not in weights:  # source removed from config
            continue
        it = Item(r["id"], r["url"], r["source"], r["section"], r["title"], r["summary"],
                  datetime.fromisoformat(r["published"]), weights[r["source"]], r["body"])
        if r["weekly"]:
            weekly.append(it)
        elif it.published >= now - DAILY_WINDOW:
            daily.append(it)
    return daily, weekly


def cluster(items: list[Item]) -> list[Cluster]:
    """Single-link clustering on fuzzy title similarity (union-find)."""
    if not items:
        return []
    toks = [tokens(i.title) for i in items]
    texts = [" ".join(t) for t in toks]
    sims = process.cdist(texts, texts, scorer=fuzz.token_set_ratio, workers=-1)
    parent = list(range(len(items)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in np.argwhere(np.triu(sims >= SIMILARITY, k=1)):
        if len(set(toks[a]) & set(toks[b])) >= MIN_SHARED_TOKENS:
            parent[find(a)] = find(b)
    groups: dict[int, list[Item]] = {}
    for idx, it in enumerate(items):
        groups.setdefault(find(idx), []).append(it)
    return [Cluster(g) for g in groups.values()]


def _recent_titles(conn: sqlite3.Connection, now: datetime) -> list[str]:
    rows = conn.execute(
        """SELECT i.title FROM used_items u JOIN items i ON i.id = u.item_id
           WHERE u.date >= ?""", ((now - NOVELTY_WINDOW).date().isoformat(),)
    ).fetchall()
    return [" ".join(tokens(r["title"])) for r in rows]


def score(c: Cluster, now: datetime) -> float:
    base = max(i.weight for i in c.items)
    coverage = 0.35 * math.log2(len(c.sources))          # 1 src: 0, 2: .35, 4: .7, 8: 1.05
    age_h = max(0.0, (now - c.newest).total_seconds() / 3600)
    recency = 0.5 + 0.5 * math.exp(-age_h / 24)
    s = (base + coverage) * recency
    return s * REPEAT_PENALTY if c.repeat else s


def rank(conn: sqlite3.Connection, sources: list[Source], per_section: int,
         n_long_reads: int, now: datetime | None = None) -> Ranking:
    now = now or datetime.now(timezone.utc)
    weights = {s.name: s.weight for s in sources}
    daily, weekly = _load(conn, weights, now)

    recent = _recent_titles(conn, now)
    clusters = cluster(daily)
    for c in clusters:
        c.section = max(c.items, key=lambda i: i.weight).section
        if recent:
            best = process.extractOne(" ".join(tokens(c.lead.title)), recent, scorer=fuzz.token_set_ratio)
            c.repeat = best is not None and best[1] >= REPEAT_SIMILARITY
        c.score = score(c, now)

    ranking = Ranking()
    for sec in SECTIONS:
        picked: list[Cluster] = []
        per_source: dict[str, int] = {}
        for c in sorted((c for c in clusters if c.section == sec), key=lambda c: -c.score):
            # single-source stories: cap per source so one busy feed can't fill the section
            if len(c.sources) == 1:
                src = c.lead.source
                if per_source.get(src, 0) >= MAX_SINGLE_PER_SOURCE:
                    continue
                per_source[src] = per_source.get(src, 0) + 1
            picked.append(c)
            if len(picked) >= per_section:
                break
        ranking.sections[sec] = picked

    # long reads: newest essays from weekly sources, at most one per source
    seen: set[str] = set()
    for it in sorted(weekly, key=lambda i: (-i.weight, -i.published.timestamp())):
        if it.source not in seen:
            seen.add(it.source)
            ranking.long_reads.append(it)
    ranking.long_reads = sorted(ranking.long_reads, key=lambda i: -i.published.timestamp())[: n_long_reads * 3]
    return ranking
