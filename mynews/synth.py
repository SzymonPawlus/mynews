"""Two Claude calls per day: select (headlines only) -> write (bodies of picked stories)."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field

from . import enrich, feedback
from .config import PROMPTS_DIR, SECTIONS, SECTION_TITLES, load_profile, load_sources
from .db import now_iso
from .llm import LLMBackend, LLMError
from .rank import Cluster, Item, rank

SNIPPET_CHARS = 220
STORY_WORDS = 500
LONG_READ_WORDS = 150
SUNDAY_LONG_READ_WORDS = 700
MAX_EXPLAINED_IN_PROMPT = 200
THREAD_MAX_AGE = timedelta(days=30)


# ---------------------------------------------------------------- schemas

class Pick(BaseModel):
    id: str
    section: str = Field(description="one of: " + ", ".join(SECTIONS))


class Selection(BaseModel):
    picks: list[Pick]
    long_reads: list[str] = Field(description="ids of chosen long reads, e.g. L2")


class Concept(BaseModel):
    name: str
    explanation: str


class Thread(BaseModel):
    slug: str
    title: str
    summary: str


class Story(BaseModel):
    id: str
    section: str
    headline: str
    what_happened: str
    why_it_matters: str
    background: list[Concept]
    known_concepts: list[str]
    thread: Thread | None


class LongRead(BaseModel):
    id: str
    why_read: str


class Explainer(BaseModel):
    concept: str
    title: str
    body: str


class FollowUp(BaseModel):
    key: str = Field(description="the DATE:REF key of the requested follow-up")
    title: str
    body: str


class Digest(BaseModel):
    intro: str
    follow_ups: list[FollowUp]
    stories: list[Story]
    long_reads: list[LongRead]
    week_in_ai: str | None
    explainer: Explainer | None


# ---------------------------------------------------------------- helpers

def _profile_text(profile: dict) -> str:
    return yaml.safe_dump(profile["reader"], sort_keys=False, allow_unicode=True).strip()


def _prompt(name: str, **kw) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text().format(**kw)


def _snippet(text: str, n: int = SNIPPET_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


def _strip_heading(text: str) -> str:
    """The template adds its own headings; drop a leading markdown heading if the model wrote one."""
    lines = text.strip().splitlines()
    while lines and (lines[0].startswith("#") or not lines[0].strip()):
        lines.pop(0)
    return "\n".join(lines).strip()


def _publisher(it: Item) -> str:
    if it.source.startswith("gnews") and it.summary:
        return it.summary  # Google News items carry the publisher name as summary
    return urlsplit(it.url).netloc.removeprefix("www.") or it.source


def _call(llm: LLMBackend, model_cls: type[BaseModel], *, system: str, user: str,
          model: str, label: str) -> BaseModel:
    last: Exception | None = None
    for attempt in range(2):
        try:
            res = llm.complete(system=system, user=user, schema=model_cls.model_json_schema(),
                               model=model, label=label)
            return model_cls.model_validate(res.data)
        except (LLMError, ValueError) as e:
            last = e
            print(f"warning: {label} attempt {attempt + 1} failed: {str(e)[:300]}", file=sys.stderr)
    raise LLMError(f"{label} failed twice: {last}")


# ---------------------------------------------------------------- stage 1: select

def select(llm: LLMBackend, profile: dict, ranking, model: str,
           reader_feedback: str = "") -> tuple[list[tuple[Cluster, str]], list[Item]]:
    quotas = profile["digest"]["quotas"]
    by_id: dict[str, Cluster] = {}
    lines = ["Quotas (max stories): " + ", ".join(f"{s} {quotas[s]}" for s in SECTIONS),
             f"Long reads: up to {profile['digest']['long_reads']}", ""]
    if reader_feedback:
        lines += ["## Reader feedback (use it to judge what this reader wants)", reader_feedback, ""]
    for sec in SECTIONS:
        lines.append(f"## Section: {sec}")
        for n, c in enumerate(ranking.sections[sec], 1):
            cid = f"{sec[0]}{n}"
            by_id[cid] = c
            snippet = _snippet(c.lead.summary)
            text_flag = "" if c.has_text else ", headline only"
            lines.append(f"[{cid}] ({len(c.sources)} sources{text_flag}) {c.lead.title}"
                         + (f" — {snippet}" if snippet else ""))
        lines.append("")
    lr_by_id = {f"L{n}": it for n, it in enumerate(ranking.long_reads, 1)}
    lines.append("## Long reads")
    for lid, it in lr_by_id.items():
        lines.append(f"[{lid}] {it.source}: {it.title} — {_snippet(it.summary)}")

    sel = _call(llm, Selection, system=_prompt("select", profile=_profile_text(profile)),
                user="\n".join(lines), model=model, label="select")

    picks, seen = [], set()
    counts = {s: 0 for s in SECTIONS}
    for p in sel.picks:
        c = by_id.get(p.id)
        sec = p.section if p.section in SECTIONS else (c.section if c else None)
        if c is None or p.id in seen or counts[sec] >= quotas[sec]:
            continue
        seen.add(p.id)
        counts[sec] += 1
        picks.append((c, sec))
    long_reads = [lr_by_id[i] for i in sel.long_reads if i in lr_by_id][: profile["digest"]["long_reads"]]
    return picks, long_reads


# ---------------------------------------------------------------- ledger / threads

def _concept_lists(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    understood = [r["name"] for r in conn.execute(
        "SELECT name FROM concepts WHERE understood = 1 ORDER BY name")]
    explained = [r["name"] for r in conn.execute(
        "SELECT name FROM concepts WHERE understood = 0 ORDER BY last_seen DESC LIMIT ?",
        (MAX_EXPLAINED_IN_PROMPT,))]
    return understood, explained


def _open_threads(conn: sqlite3.Connection, now: datetime) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM threads WHERE updated_at >= ? ORDER BY updated_at DESC LIMIT 20",
        ((now - THREAD_MAX_AGE).isoformat(),)).fetchall()


def _explainer_concept(conn: sqlite3.Connection, today: date) -> str | None:
    row = conn.execute(
        """SELECT name FROM concepts WHERE understood = 0 AND times_seen >= 2 AND last_seen >= ?
           ORDER BY times_seen DESC, last_seen DESC LIMIT 1""",
        ((today - timedelta(days=14)).isoformat(),)).fetchone()
    return row["name"] if row else None


# ---------------------------------------------------------------- stage 2: write

def write(llm: LLMBackend, conn: sqlite3.Connection, profile: dict, picks, long_reads,
          today: date, sunday: bool, model: str,
          pending: feedback.Pending | None = None) -> tuple[Digest, dict]:
    now = datetime.now(timezone.utc)
    bodies = enrich.enrich_clusters(conn, [c for c, _ in picks], STORY_WORDS)
    lr_words = SUNDAY_LONG_READ_WORDS if sunday else LONG_READ_WORDS
    lr_bodies = enrich.enrich(conn, [[it] for it in long_reads], lr_words)

    understood, explained = _concept_lists(conn)
    threads = _open_threads(conn, now)
    explainer = _explainer_concept(conn, today) if sunday else None

    L = [f"Date: {today:%A %Y-%m-%d}. Edition: {'SUNDAY' if sunday else 'weekday'}.", ""]
    L.append("## Understood concepts (never explain, no need to link)")
    L.append(", ".join(understood) or "(none)")
    L.append("## Already explained concepts (don't re-explain; list in known_concepts when relevant)")
    L.append(", ".join(explained) or "(none)")
    L.append("## Open threads")
    L += [f"- {t['slug']}: {t['title']} — {t['summary']}" for t in threads] or ["(none)"]
    notes = feedback.notes_text(conn)
    if notes:
        L.append("## Reader's standing preferences (from their feedback)")
        L.append(notes)
    if sunday:
        L.append(f"## Sunday explainer concept: {explainer or 'choose the most useful concept from this week'}")
    if pending and pending.follow_ups:
        L.append("\n## Follow-ups requested by the reader")
        for fu in pending.follow_ups:
            L.append(f"### [{fu['key']}] {fu['headline']}")
            L.append(f"Previously told: {fu['what_happened']}")
            if fu["text"]:
                L.append(f"Source text:\n{fu['text']}")
            L.append("")
    L.append("\n## Stories")
    story_ids: dict[str, tuple[Cluster, str]] = {}
    for n, ((c, sec), body) in enumerate(zip(picks, bodies), 1):
        sid = f"s{n}"
        story_ids[sid] = (c, sec)
        L.append(f"### [{sid}] section: {sec}")
        L.append("Coverage: " + "; ".join(f'{i.source}: "{i.title}"' for i in c.items[:6]))
        if body:
            L.append(f"Text (from {body[0].source}):\n{body[1]}")
        else:
            L.append(f"Text: (headline only) {c.lead.summary}")
        L.append("")
    if long_reads:
        L.append("## Long reads")
        for n, (it, body) in enumerate(zip(long_reads, lr_bodies), 1):
            L.append(f"### [L{n}] {it.source}: {it.title}")
            L.append(body[1] if body else it.summary)
            L.append("")

    system = _prompt("write", profile=_profile_text(profile),
                     language=profile["digest"]["language"],
                     minutes=profile["digest"]["target_read_minutes"])
    digest = _call(llm, Digest, system=system, user="\n".join(L), model=model, label="write")
    return digest, {"story_ids": story_ids, "long_reads": long_reads,
                    "threads": {t["slug"]: dict(t) for t in threads}}


# ---------------------------------------------------------------- persist + assemble

def persist(conn: sqlite3.Connection, digest: Digest, ctx: dict, today: date,
            pending: feedback.Pending | None = None) -> dict:
    """Update ledger/threads/used items and return the render-ready digest dict."""
    ts = now_iso()
    day = today.isoformat()
    old_threads = ctx["threads"]
    stories_out = {s: [] for s in SECTIONS}
    glossary: dict[str, str] = {}
    ref = 0

    for st in digest.stories:
        if st.id not in ctx["story_ids"]:
            continue
        cluster, sec = ctx["story_ids"][st.id]
        for it in cluster.items:
            conn.execute("INSERT OR IGNORE INTO used_items (item_id, date) VALUES (?, ?)", (it.id, day))
        for con in st.background:
            conn.execute(
                """INSERT INTO concepts (name, explanation, first_seen, last_seen) VALUES (?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET last_seen = excluded.last_seen,
                                                   times_seen = times_seen + 1""",
                (con.name, con.explanation, day, day))
        known = []
        for name in st.known_concepts:
            row = conn.execute("SELECT name, explanation, understood FROM concepts WHERE name = ?",
                               (name,)).fetchone()
            if row is None:
                continue
            conn.execute("UPDATE concepts SET last_seen = ?, times_seen = times_seen + 1 WHERE name = ?",
                         (day, row["name"]))
            if not row["understood"]:
                known.append(row["name"])
                glossary[row["name"]] = row["explanation"]
        previously = None
        if st.thread:
            prev = old_threads.get(st.thread.slug)
            previously = prev["summary"] if prev else None
            conn.execute(
                """INSERT INTO threads (slug, title, summary, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(slug) DO UPDATE SET title = excluded.title,
                       summary = excluded.summary, updated_at = excluded.updated_at""",
                (st.thread.slug, st.thread.title, st.thread.summary, ts))
        seen_urls, links = set(), []
        for it in sorted(cluster.items, key=lambda i: -i.weight):
            if it.url not in seen_urls:
                seen_urls.add(it.url)
                links.append({"source": _publisher(it), "title": it.title, "url": it.url})
        ref += 1
        stories_out[sec].append({
            "ref": str(ref), "section": sec,
            "item_ids": [i.id for i in sorted(cluster.items, key=lambda i: -i.weight)],
            "headline": st.headline, "what_happened": st.what_happened,
            "why_it_matters": st.why_it_matters,
            "background": [b.model_dump() for b in st.background],
            "known_concepts": known,
            "thread": st.thread.model_dump() if st.thread else None,
            "previously": previously,
            "links": links[:5],
        })

    long_reads = []
    lr_map = {f"L{n}": it for n, it in enumerate(ctx["long_reads"], 1)}
    for lr in digest.long_reads:
        it = lr_map.get(lr.id)
        if it:
            conn.execute("INSERT OR IGNORE INTO used_items (item_id, date) VALUES (?, ?)", (it.id, day))
            long_reads.append({"source": it.source, "title": it.title, "url": it.url,
                               "why_read": lr.why_read})

    explainer = digest.explainer.model_dump() if digest.explainer else None
    if explainer:
        explainer["body"] = _strip_heading(explainer["body"])
    requested = {fu["key"] for fu in pending.follow_ups} if pending else set()
    out = {
        "date": day,
        "intro": digest.intro,
        "feedback_ack": pending.ack if pending else [],
        "follow_ups": [{"title": f.title, "body": _strip_heading(f.body)}
                       for f in digest.follow_ups if f.key in requested],
        "sections": [{"key": s, "title": SECTION_TITLES[s], "stories": stories_out[s]}
                     for s in SECTIONS if stories_out[s]],
        "long_reads": long_reads,
        "week_in_ai": _strip_heading(digest.week_in_ai) if digest.week_in_ai else None,
        "explainer": explainer,
        "glossary": dict(sorted(glossary.items())),
    }
    conn.execute("INSERT OR REPLACE INTO digests (date, json, created_at) VALUES (?, ?, ?)",
                 (day, json.dumps(out, ensure_ascii=False), ts))
    conn.commit()
    return out


def build(conn: sqlite3.Connection, llm: LLMBackend, today: date | None = None,
          sunday: bool | None = None) -> dict:
    profile = load_profile()
    today = today or date.today()
    sunday = today.weekday() == 6 if sunday is None else sunday
    ranking = rank(conn, load_sources(), profile["digest"]["candidates_per_section"],
                   profile["digest"]["long_reads"])
    model_select = os.environ.get("MYNEWS_MODEL_SELECT", "sonnet")
    model_write = os.environ.get("MYNEWS_MODEL_WRITE", "sonnet")
    pending = feedback.gather(conn, llm, model_select)
    notes = feedback.notes_text(conn)
    parts = [f"Standing notes:\n{notes}" if notes else "", feedback.ratings_text(conn, today)]
    reader_feedback = "\n".join(p for p in parts if p)
    picks, long_reads = select(llm, profile, ranking, model_select, reader_feedback)
    if not picks:
        raise LLMError("selection returned no stories")
    digest, ctx = write(llm, conn, profile, picks, long_reads, today, sunday, model_write, pending)
    out = persist(conn, digest, ctx, today, pending)
    feedback.mark_processed(conn, pending)
    return out
