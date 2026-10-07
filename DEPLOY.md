# Deploying mynews to a VPS

These steps are written so you can follow them by hand, or open Claude Code on the VPS and say: *"Follow DEPLOY.md"*. Steps marked **(human)** need you, because they involve secrets or an interactive login.

Target setup: Linux VPS with systemd, the repo at `~/mynews`, and a **user** systemd timer that runs the digest every day at 06:00 Europe/Warsaw. No root daemon and no mail server are needed.

## 0. Prerequisites

- A regular (non-root) user account with `git` and `curl` installed.
- Outbound HTTPS (port 443) for news sources and the Claude API.
- A Claude Pro/Max subscription.
- A domain whose DNS you control, and outbound port 25 allowed by the VPS provider.
- Debian/Ubuntu (for `mail-setup.sh`; the rest works on any systemd Linux).

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

## 3. Email: own send-only mail server

mynews sends through a local Postfix that listens only on localhost and signs mail with DKIM. It sends from a dedicated **subdomain** (e.g. `news.example.pl`), so the root domain's existing mail setup (MX/SPF at the registrar) is not touched. If Claude Code is running this, ask the user which sending subdomain and recipient address to use.

1. Install and configure Postfix and OpenDKIM (Debian/Ubuntu):

   ```bash
   sudo ./deploy/mail-setup.sh news.example.pl
   ```

   The script is idempotent. It checks whether outbound port 25 is reachable and prints the exact DNS records to add, including the DKIM public key.

2. **(human)** Add the printed records in the DNS zone of the parent domain (OVH: *Web Cloud → Domain names → DNS zone → Add an entry*):
   - `A` record: `news` → VPS IPv4
   - `TXT` record: `news` → `v=spf1 ip4:<VPS IP> -all`
   - `TXT` record: `mynews._domainkey.news` → `v=DKIM1;h=sha256;k=rsa;p=…`
   - `TXT` record: `_dmarc.news` → `v=DMARC1; p=none; adkim=s; aspf=s`

3. **(human)** Set **reverse DNS (PTR)** for the VPS IPv4 to `news.example.pl` in the VPS provider's panel (OVH: *Bare Metal Cloud → IP → ⋯ → Modify the reverse*). The A record must already resolve, or OVH refuses the change.

4. Fill in `.env`:

   ```
   SMTP_HOST=localhost
   SMTP_PORT=25
   SMTP_STARTTLS=false
   SMTP_USER=
   SMTP_PASSWORD=
   MAIL_FROM=mynews <mynews@news.example.pl>
   MAIL_TO=<your inbox>
   ```

5. After DNS propagates (minutes to a few hours), check:

   ```bash
   getent hosts news.example.pl                        # → VPS IP
   opendkim-testkey -d news.example.pl -s mynews -vvv  # → "key OK"
   ```

The first messages may land in spam: mark them "Not spam" a couple of times. In Gmail, open *Show original* and confirm **SPF, DKIM and DMARC: PASS**. After a week of clean delivery you can tighten DMARC to `p=quarantine`.

Optional: get a free Guardian API key (https://open-platform.theguardian.com/access/) and set it as `GUARDIAN_API_KEY` to enable the Guardian business/world sources.

(Alternative: any authenticated SMTP relay works instead of Postfix, e.g. a Gmail app password. See the comments in `deploy/.env.example`.)

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
| Mail not arriving | `sudo journalctl -u postfix -n 50` / `mailq`. "Connection timed out" to port 25 means the provider blocks it. |
| Gmail rejects: "not authenticated" / "PTR" | The PTR record must equal the sending subdomain, and the A record must point back to the same IP. Check SPF/DKIM with `opendkim-testkey`. |
| DKIM "none" in Show original | `systemctl status opendkim`, `ss -ltn \| grep 8891`; re-run `mail-setup.sh`. |
| Lands in spam | Normal for a new sender. Mark "Not spam", keep sending daily; confirm all three checks PASS. |
| Timer doesn't fire after logout | `loginctl show-user $USER -p Linger` must say `Linger=yes`. |
| Wrong send time | The timer uses `Europe/Warsaw` explicitly; check with `systemctl --user list-timers`. |
| A run failed | Failures are emailed with the traceback. You can also run `journalctl --user -u mynews`. |

## Updating

```bash
cd ~/mynews && git pull && uv sync --frozen
cp deploy/mynews.service deploy/mynews.timer ~/.config/systemd/user/ && systemctl --user daemon-reload
```

`config/profile.yaml` and `config/sources.yaml` are tracked in git. Edit them on the VPS (or commit changes and pull); the next run picks them up.
