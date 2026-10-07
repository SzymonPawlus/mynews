# Deploying mynews to a VPS

These steps are written so you can follow them by hand, or open Claude Code on the VPS and say: *"Follow DEPLOY.md"*. Steps marked **(human)** need you, because they involve secrets or an interactive login.

Target setup: Linux VPS with systemd, the repo at `~/mynews`, and a **user** systemd timer that runs the digest every day at 06:00 Europe/Warsaw. No root daemon and no mail server are needed.

## 0. Prerequisites

- A regular (non-root) user account with `git` and `curl` installed.
- Outbound HTTPS (port 443) for news sources and the Claude API, and port 587 for SMTP to Gmail.
- A Claude Pro/Max subscription, and a Gmail account with 2-step verification turned on.

## 1. Clone and install

```bash
git clone https://github.com/SzymonPawlus/mynews.git ~/mynews
cd ~/mynews
./deploy/install.sh
```

`install.sh`:

- installs `uv` and Claude Code into `~/.local/bin` if they're missing;
- runs `uv sync --frozen` (uv downloads a suitable Python if needed);
- creates `.env` from `deploy/.env.example` with mode 600;
- installs and enables `mynews.timer` as a user unit;
- enables lingering, so the timer runs without you being logged in. This may ask for sudo.

## 2. Claude token **(human)**

```bash
claude setup-token
```

This opens a login flow and prints a long-lived OAuth token for your subscription. Put it in `~/mynews/.env`:

```
CLAUDE_CODE_OAUTH_TOKEN=<token>
```

If Claude Code is driving this setup, run the command yourself in the session with `! claude setup-token`. Never paste the token into chat.

## 3. Email **(human)**

1. Create a Gmail app password at https://myaccount.google.com/apppasswords
2. Fill in `.env`:

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASSWORD=<16-character app password, no spaces>
MAIL_TO=you@gmail.com
```

Optional: get a free Guardian API key (https://open-platform.theguardian.com/access/) and set it as `GUARDIAN_API_KEY` to enable the Guardian business/world sources.

## 4. Verify

Run these from `~/mynews` in order. Each should succeed before you go on.

```bash
# sources reachable from the VPS (a few ERR lines are tolerable; Guardian shows "skipped" without a key)
uv run mynews ingest --check

# ranking works (no LLM)
uv run mynews preview | head -40

# full pipeline without email: checks the Claude token and structured output
set -a; . ./.env; set +a
uv run mynews run --no-send --skip-ingest
uv run mynews usage            # expect 2 calls: select + write

# email delivery: resend the digest just built
uv run mynews render --send
```

Then test the real service exactly as the timer will run it:

```bash
systemctl --user start mynews.service
journalctl --user -u mynews -n 50 --no-pager
systemctl --user list-timers mynews.timer
```

**Note:** the test runs above count as today's digest, and their stories are marked as used. If you want a clean start, reset the ledger before the first scheduled run:

```bash
sqlite3 data/mynews.sqlite "DELETE FROM digests; DELETE FROM used_items; DELETE FROM concepts; DELETE FROM threads;"
```

(or `rm data/mynews.sqlite`; it's recreated on the next run).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `claude: command not found` in the journal | Check that `~/.local/bin/claude` exists; the unit's `PATH` includes `~/.local/bin`. Set `CLAUDE_BIN=/path/to/claude` in `.env` if it's installed elsewhere. |
| `select`/`write` fails with an auth error | The token is missing or expired: run `claude setup-token` again and update `.env`. |
| `SMTPAuthenticationError` | Use an app password, not your Gmail password; 2-step verification must be on. |
| Timer doesn't fire after logout | `loginctl show-user $USER -p Linger` must say `Linger=yes`. |
| Wrong send time | The timer uses `Europe/Warsaw` explicitly; check with `systemctl --user list-timers`. |
| A run failed | Failures are emailed with the traceback. You can also run `journalctl --user -u mynews`. |

## Updating

```bash
cd ~/mynews && git pull && uv sync --frozen
cp deploy/mynews.service deploy/mynews.timer ~/.config/systemd/user/ && systemctl --user daemon-reload
```

`config/profile.yaml` and `config/sources.yaml` are tracked in git. Edit them on the VPS (or commit changes and pull); the next run picks them up.
