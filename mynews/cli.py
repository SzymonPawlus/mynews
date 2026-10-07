"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import date
from html import escape as html_escape

from . import ingest, rank, render
from .config import DATA_DIR, OUT_DIR, SECTION_TITLES, load_env, load_profile, load_sources
from .db import connect


def cmd_ingest(args) -> int:
    sources = load_sources()
    if args.only:
        sources = [s for s in sources if s.name in args.only]
    results = ingest.fetch_all(sources)
    if args.check:
        width = max(len(r.source.name) for r in results)
        for r in sorted(results, key=lambda r: (r.source.section, r.source.name)):
            status = "ok " if r.error is None else "ERR"
            line = f"{status} {r.source.section:8} {r.source.name:{width}} {len(r.items):4} items"
            if r.error:
                line += f"  {r.error}"
            elif r.items and args.verbose:
                line += f"  e.g. {r.items[0].title[:70]!r}"
            print(line)
    if not args.dry_run:
        with connect() as conn:
            new = ingest.store(conn, results)
        print(f"stored {new} new items from {len(results)} sources", file=sys.stderr)
    return 0


def cmd_preview(args) -> int:
    profile = load_profile()
    with connect() as conn:
        r = rank.rank(conn, load_sources(), profile["digest"]["candidates_per_section"],
                      profile["digest"]["long_reads"])
    for sec, clusters in r.sections.items():
        print(f"\n== {SECTION_TITLES[sec]} ==")
        for c in clusters:
            flag = " (repeat)" if c.repeat else ""
            print(f"{c.score:5.2f} [{len(c.sources)} src]{flag} {c.lead.title[:100]}")
            if args.verbose:
                for it in c.items:
                    if it is not c.lead:
                        print(f"{'':16}~ {it.source}: {it.title[:80]}")
    print("\n== Long reads ==")
    for it in r.long_reads:
        print(f"      {it.source}: {it.title[:90]} ({it.published:%Y-%m-%d})")
    return 0


def _write_outputs(d: dict) -> tuple[str, str]:
    html, md = render.html(d), render.markdown(d)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{d['date']}.html").write_text(html)
    (OUT_DIR / f"{d['date']}.md").write_text(md)
    return html, md


def _send(d: dict, html: str, md: str) -> None:
    from . import mail
    n = sum(len(s["stories"]) for s in d["sections"])
    mail.send(f"Daily briefing — {render.date_long(d)} ({n} stories)", html, md)


def cmd_run(args) -> int:
    try:
        return _run(args)
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        if not args.no_send:
            try:
                from . import mail
                mail.send("mynews: digest run FAILED", f"<pre>{html_escape(tb)}</pre>", tb)
            except Exception as e:
                print(f"could not send failure email: {e}", file=sys.stderr)
        return 1


def _run(args) -> int:
    from . import synth
    from .llm import get_backend
    if not args.skip_ingest:
        cmd_ingest(argparse.Namespace(check=False, dry_run=False, verbose=False, only=None))
    with connect() as conn:
        d = synth.build(conn, get_backend(), sunday=True if args.sunday else None)
    html, md = _write_outputs(d)
    print(f"wrote {OUT_DIR / d['date']}.html", file=sys.stderr)
    if not args.no_send:
        _send(d, html, md)
        print("email sent", file=sys.stderr)
    return 0


def cmd_render(args) -> int:
    day = args.date or date.today().isoformat()
    with connect() as conn:
        row = conn.execute("SELECT json FROM digests WHERE date = ?", (day,)).fetchone()
    if not row:
        print(f"no digest for {day}", file=sys.stderr)
        return 1
    d = json.loads(row["json"])
    html, md = _write_outputs(d)
    if args.send:
        _send(d, html, md)
    print(f"wrote {OUT_DIR / day}.html", file=sys.stderr)
    return 0


def cmd_concept(args) -> int:
    with connect() as conn:
        if args.action == "list":
            for r in conn.execute("SELECT * FROM concepts ORDER BY understood, times_seen DESC, name"):
                mark = "✓" if r["understood"] else " "
                print(f"{mark} {r['times_seen']:3}x  {r['name']}  (last {r['last_seen']})")
            return 0
        if not args.names:
            print("give concept name(s)", file=sys.stderr)
            return 1
        for name in args.names:
            if args.action == "show":
                r = conn.execute("SELECT * FROM concepts WHERE name = ?", (name,)).fetchone()
                print(f"{r['name']}: {r['explanation']}" if r else f"unknown: {name}")
                continue
            sql = {"understood": "UPDATE concepts SET understood = 1 WHERE name = ?",
                   "unlearn": "UPDATE concepts SET understood = 0 WHERE name = ?",
                   "forget": "DELETE FROM concepts WHERE name = ?"}[args.action]
            n = conn.execute(sql, (name,)).rowcount
            print(f"{args.action}: {name}" if n else f"unknown: {name}")
    return 0


def cmd_threads(args) -> int:
    with connect() as conn:
        if args.close:
            conn.execute("DELETE FROM threads WHERE slug = ?", (args.close,))
            return 0
        for r in conn.execute("SELECT * FROM threads ORDER BY updated_at DESC"):
            print(f"{r['slug']}  ({r['updated_at'][:10]})\n  {r['title']}: {r['summary']}\n")
    return 0


def cmd_usage(args) -> int:
    path = DATA_DIR / "usage.jsonl"
    if not path.exists():
        return 0
    for line in path.read_text().splitlines()[-args.n:]:
        u = json.loads(line)
        print(f"{u['at'][:16]}  {u['label']:7} {u['model']:7} in {u['input']:6}  out {u['output']:5}"
              f"  ~${u['equiv_cost_usd'] or 0:.3f} API-equiv  {u['duration_s']}s")
    return 0


def cmd_serve(args) -> int:
    from . import web
    web.serve(port=args.port)
    return 0


def cmd_inbound(args) -> int:
    from . import inbound
    return inbound.main()


def cmd_feedback(args) -> int:
    from . import feedback
    with connect() as conn:
        if args.action == "comment":
            feedback.record_comment(conn, " ".join(args.text), "cli")
            print("noted; applied in the next run")
            return 0
        rows = conn.execute("SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (args.n,)).fetchall()
        for r in reversed(rows):
            st = feedback.story(conn, r["date"], r["ref"])
            what = r["text"] or r["concept"] or (st["headline"] if st else "")
            mark = " " if r["processed"] else "*"
            print(f"{mark} {r['at'][:16]} {r['channel']:5} {r['kind']:7} {r['date']} {what[:90]}")
    return 0


def cmd_notes(args) -> int:
    from .db import now_iso
    with connect() as conn:
        if args.action == "add":
            conn.execute("INSERT INTO notes (text, created_at) VALUES (?, ?)",
                         (" ".join(args.args), now_iso()))
        elif args.action == "rm":
            for nid in args.args:
                conn.execute("UPDATE notes SET active = 0 WHERE id = ?", (int(nid),))
        else:
            for r in conn.execute("SELECT * FROM notes WHERE active = 1 ORDER BY id"):
                print(f"[{r['id']}] {r['text']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_env()
    p = argparse.ArgumentParser(prog="mynews")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("ingest", help="fetch all sources into the database")
    pi.add_argument("--check", action="store_true", help="print per-source status")
    pi.add_argument("--dry-run", action="store_true", help="don't write to the database")
    pi.add_argument("-v", "--verbose", action="store_true")
    pi.add_argument("--only", nargs="+", help="only these source names")
    pi.set_defaults(func=cmd_ingest)

    pp = sub.add_parser("preview", help="show ranked candidates (no LLM)")
    pp.add_argument("-v", "--verbose", action="store_true", help="show cluster members")
    pp.set_defaults(func=cmd_preview)

    pr = sub.add_parser("run", help="ingest, synthesize, render and email today's digest")
    pr.add_argument("--no-send", action="store_true", help="don't email, only write out/")
    pr.add_argument("--skip-ingest", action="store_true")
    pr.add_argument("--sunday", action="store_true", help="force the Sunday edition")
    pr.set_defaults(func=cmd_run)

    pre = sub.add_parser("render", help="re-render a stored digest (and optionally resend)")
    pre.add_argument("date", nargs="?", help="YYYY-MM-DD, default today")
    pre.add_argument("--send", action="store_true")
    pre.set_defaults(func=cmd_render)

    pc = sub.add_parser("concept", help="manage the concept ledger")
    pc.add_argument("action", choices=["list", "show", "understood", "unlearn", "forget"])
    pc.add_argument("names", nargs="*")
    pc.set_defaults(func=cmd_concept)

    pt = sub.add_parser("threads", help="list story threads")
    pt.add_argument("--close", metavar="SLUG", help="stop following a thread")
    pt.set_defaults(func=cmd_threads)

    pu = sub.add_parser("usage", help="show recent LLM token usage")
    pu.add_argument("-n", type=int, default=20)
    pu.set_defaults(func=cmd_usage)

    ps = sub.add_parser("serve", help="run the feedback web endpoint (behind a TLS proxy)")
    ps.add_argument("--port", type=int)
    ps.set_defaults(func=cmd_serve)

    pin = sub.add_parser("inbound", help="read an email reply from stdin (Postfix pipe)")
    pin.set_defaults(func=cmd_inbound)

    pf = sub.add_parser("feedback", help="list feedback (* = not yet applied) or add a comment")
    pf.add_argument("action", nargs="?", choices=["list", "comment"], default="list")
    pf.add_argument("text", nargs="*")
    pf.add_argument("-n", type=int, default=30)
    pf.set_defaults(func=cmd_feedback)

    pn = sub.add_parser("notes", help="standing preferences distilled from feedback")
    pn.add_argument("action", nargs="?", choices=["list", "add", "rm"], default="list")
    pn.add_argument("args", nargs="*")
    pn.set_defaults(func=cmd_notes)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
