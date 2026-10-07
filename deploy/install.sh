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
# random secret that signs the one-click feedback links
if ! grep -q '^MYNEWS_FEEDBACK_SECRET=.' .env; then
    secret=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
    sed -i '/^MYNEWS_FEEDBACK_SECRET=/d' .env
    echo "MYNEWS_FEEDBACK_SECRET=$secret" >> .env
fi

mkdir -p ~/.config/systemd/user
cp deploy/mynews.service deploy/mynews.timer deploy/mynews-web.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mynews.timer
systemctl --user enable mynews-web.service   # started once .env is filled in (step 4 below)
# keep user timers running without an active login session
loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER"

echo
echo "Next:"
echo "  1. claude setup-token        # paste result into .env as CLAUDE_CODE_OAUTH_TOKEN"
echo "  2. sudo ./deploy/mail-setup.sh news.<your-domain>   # mail + DNS records to add"
echo "  3. sudo ./deploy/web-setup.sh news.<your-domain>    # HTTPS for feedback links"
echo "  4. edit .env (MAIL_*, MYNEWS_PUBLIC_URL), then: systemctl --user restart mynews-web"
echo "  5. follow the checks in DEPLOY.md"
