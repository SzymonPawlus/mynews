#!/usr/bin/env bash
# HTTPS for the feedback links: Caddy (automatic Let's Encrypt) -> mynews serve on 127.0.0.1:8787.
# Debian/Ubuntu. Idempotent.
#
#   sudo ./deploy/web-setup.sh news.example.pl
set -euo pipefail

DOMAIN=${1:?usage: sudo $0 <domain, e.g. news.example.pl>}
PORT=${MYNEWS_WEB_PORT:-8787}
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }

if ! command -v caddy >/dev/null; then
    if ! apt-get install -y caddy 2>/dev/null; then  # older releases: official Caddy repo
        apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
        curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
            | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
        curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
            > /etc/apt/sources.list.d/caddy-stable.list
        apt-get update
        apt-get install -y caddy
    fi
fi

CADDYFILE=/etc/caddy/Caddyfile
BLOCK="# mynews feedback endpoint
$DOMAIN {
	handle /fb* {
		reverse_proxy 127.0.0.1:$PORT
	}
	handle /health {
		reverse_proxy 127.0.0.1:$PORT
	}
	handle {
		respond 404
	}
}"

if grep -q "^$DOMAIN {" "$CADDYFILE" 2>/dev/null; then
    echo "Caddyfile already has a block for $DOMAIN, leaving it alone"
elif grep -q "/usr/share/caddy" "$CADDYFILE" 2>/dev/null || [[ ! -s $CADDYFILE ]]; then
    cp -n "$CADDYFILE" "$CADDYFILE.orig" 2>/dev/null || true
    echo "$BLOCK" > "$CADDYFILE"          # replace the stock "welcome page" config
else
    printf '\n%s\n' "$BLOCK" >> "$CADDYFILE"  # keep the user's other sites
fi

caddy validate --config "$CADDYFILE" --adapter caddyfile
systemctl enable --now caddy
systemctl reload caddy

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow 80/tcp && ufw allow 443/tcp
fi

echo
echo "Caddy serves https://$DOMAIN/fb -> 127.0.0.1:$PORT"
echo "Needs: A record $DOMAIN -> this VPS, ports 80 and 443 open (certificate is issued automatically)."
echo "In ~/mynews/.env set: MYNEWS_PUBLIC_URL=https://$DOMAIN"
echo "Check: curl https://$DOMAIN/health   # -> ok"
