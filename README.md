# mynews

Personal daily news briefing by email: AI progress (big picture), economy (Poland / EU / US / global), science and world news, explained at the reader's level.

**Aggregators read, code filters, Claude explains.**

1. `ingest` pulls ~30 curated RSS/API sources into SQLite (no LLM).
2. `rank` clusters the same story across sources; the number of independent sources covering a story is the main importance signal (no LLM).
3. **select**: one Claude call that sees only headlines and picks the stories (~6K tokens).
4. `enrich` fetches article text for the picked stories only (no LLM).
5. **write**: one Claude call that writes the briefing (~15K in / ~5K out).
6. Render HTML and Markdown, then email it through a local send-only Postfix with DKIM, or any SMTP relay.

Personalisation lives in `config/profile.yaml` and in two tables that grow over time:

- **concept ledger**: every background concept explained once is linked to the glossary afterwards instead of being explained again. Mark concepts as known and they disappear.
- **story threads**: developing stories carry a rolling summary, so the digest can show *Previously / New today*.

On Sundays there's also a "week in AI" overview and an explainer on a concept that kept coming up.

## Commands

```
mynews ingest --check -v      # fetch all sources, per-source status
mynews preview -v             # ranked candidates (no LLM) - tune weights in sources.yaml
mynews run --no-send          # full pipeline, writes out/YYYY-MM-DD.{html,md}
mynews run                    # ... and emails it (failures are emailed too)
mynews run --sunday           # force the Sunday edition
mynews render [DATE] --send   # re-render / resend a stored digest
mynews concept list | show NAME | understood NAME | unlearn NAME | forget NAME
mynews threads [--close SLUG]
mynews usage                  # token usage per LLM call
```

Follow-up questions: open Claude Code in this repo and ask about `out/<date>.md`.

## Configuration

- `config/sources.yaml`: sources, section, weight, cadence (weekly sources become "long reads"), include/exclude keywords.
- `config/profile.yaml`: who you are, expertise levels, per-section quotas, read time.
- `prompts/select.md`, `prompts/write.md`: editorial instructions.
- `.env`: secrets (see `deploy/.env.example`).

LLM calls go through headless Claude Code (`claude -p`) on your subscription. Each call uses a custom system prompt, no tools and structured JSON output. A different backend can be added in `mynews/llm.py`.

## Deploy (VPS, systemd user timer, 06:00 Europe/Warsaw)

Full step-by-step guide: [DEPLOY.md](DEPLOY.md). Short version:

```
git clone https://github.com/SzymonPawlus/mynews.git ~/mynews && cd ~/mynews && ./deploy/install.sh
claude setup-token            # paste into .env as CLAUDE_CODE_OAUTH_TOKEN
sudo ./deploy/mail-setup.sh news.<your-domain>   # send-only Postfix + DKIM, prints DNS records
$EDITOR .env
systemctl --user start mynews.service && journalctl --user -u mynews -f
```
