#!/bin/sh
# Postfix delivers replies to mynews@<domain> here (via ~/.forward). Never fails, never bounces.
cd "$HOME/mynews" || exit 0
exec "$HOME/.local/bin/uv" run --frozen --quiet mynews inbound
