"""Render a digest dict to HTML (email) and Markdown (archive / follow-ups)."""

from __future__ import annotations

import re
from datetime import date

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import feedback
from .config import TEMPLATES_DIR


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR),
                      autoescape=select_autoescape(["html", "j2"]))
    env.filters["slug"] = slug
    return env


def date_long(d: dict) -> str:
    day = date.fromisoformat(d["date"])
    return f"{day:%A}, {day.day} {day:%B %Y}"


def html(d: dict) -> str:
    def fb(ref: str, action: str, concept: str = "") -> str:
        return feedback.link(d["date"], ref, action, concept)

    return _env().get_template("email.html.j2").render(
        d=d, date_long=date_long(d), fb=fb, fb_on=feedback.links_enabled())


def markdown(d: dict) -> str:
    out = [f"# Daily briefing — {date_long(d)}", "", d["intro"], ""]
    if d.get("feedback_ack"):
        out += ["*Your feedback:* " + " ".join(d["feedback_ack"]), ""]
    for f in d.get("follow_ups", []):
        out += [f"## Follow-up: {f['title']}", "", f["body"], ""]
    if d.get("explainer"):
        e = d["explainer"]
        out += [f"## Sunday explainer: {e['title']}", "", e["body"], ""]
    if d.get("week_in_ai"):
        out += ["## The week in AI", "", d["week_in_ai"], ""]
    for sec in d["sections"]:
        out += [f"## {sec['title']}", ""]
        for s in sec["stories"]:
            out += [f"### {s['headline']}", ""]
            if s.get("previously"):
                out += [f"*Previously:* {s['previously']}", ""]
            out += [s["what_happened"], "", f"**Why it matters:** {s['why_it_matters']}", ""]
            for b in s["background"]:
                out += [f"> **{b['name']}** — {b['explanation']}", ""]
            if s["known_concepts"]:
                out += ["Recall: " + ", ".join(s["known_concepts"]), ""]
            out += [" · ".join(f"[{l['source']}]({l['url']})" for l in s["links"]), ""]
    if d["long_reads"]:
        out += ["## Long reads", ""]
        out += [f"- [{r['title']}]({r['url']}) ({r['source']}) — {r['why_read']}" for r in d["long_reads"]]
        out.append("")
    if d["glossary"]:
        out += ["## Glossary", ""]
        out += [f"- **{k}** — {v}" for k, v in d["glossary"].items()]
        out.append("")
    out.append("Feedback: reply to this email in your own words; it is applied in the next briefing.")
    return "\n".join(out) + "\n"
