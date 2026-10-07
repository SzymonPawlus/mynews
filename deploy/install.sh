#!/usr/bin/env bash
# Run on the VPS as the user that will own the service, from the repo root (~/mynews).
set -euo pipefail
cd "$(dirname "$0")/.."
[[ "$PWD" == "$HOME/mynews" ]] || { echo "clone the repo to ~/mynews (units assume that path)"; exit 1; }

command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
command -v claude >/dev/null || curl -fsSL https://claude.ai/install.sh | bash
export PATH="$HOME/.local/bin:$PATH"

uv sync --frozen
[[ -f .env ]] || { cp deploy/.env.example .env; chmod 600 .env; echo "created .env - fill it in"; }

mkdir -p ~/.config/systemd/user
cp deploy/mynews.service deploy/mynews.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mynews.timer
# keep user timers running without an active login session
loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER"

echo
echo "Next:"
echo "  1. claude setup-token        # paste result into .env as CLAUDE_CODE_OAUTH_TOKEN"
echo "  2. edit .env (SMTP, optional GUARDIAN_API_KEY)"
echo "  3. uv run mynews ingest --check"
echo "  4. systemctl --user start mynews.service && journalctl --user -u mynews -f"
