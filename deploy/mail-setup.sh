#!/usr/bin/env bash
# Mail for mynews: Postfix + OpenDKIM. Debian/Ubuntu. Idempotent: safe to re-run.
#
#   sudo ./deploy/mail-setup.sh news.example.pl              # send + receive replies (default)
#   sudo ./deploy/mail-setup.sh news.example.pl --send-only  # send only, listen on localhost
#
# - Outgoing mail is DKIM-signed.
# - Inbound (default): accepts mail ONLY for mynews@<domain>, verifies DKIM, and pipes it to
#   `mynews inbound` running as the invoking user (via ~/.forward). All other recipients are
#   rejected, and nothing is relayed.
#
# Use a dedicated SUBDOMAIN, so existing mail on the root domain (MX/SPF at your
# provider) is left untouched.
set -euo pipefail

DOMAIN=${1:?usage: sudo $0 <subdomain, e.g. news.example.pl> [--send-only]}
INBOUND=1
[[ ${2:-} == --send-only ]] && INBOUND=0
SELECTOR=mynews
KEYDIR=/etc/opendkim/keys/$DOMAIN

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
command -v apt-get >/dev/null || { echo "this script supports Debian/Ubuntu only"; exit 1; }
RUN_USER=${SUDO_USER:-}
if [[ $INBOUND == 1 && ( -z $RUN_USER || $RUN_USER == root ) ]]; then
    echo "run via sudo from the user that owns ~/mynews (needed to deliver replies)"; exit 1
fi

IP=$(curl -4 -fsS https://api.ipify.org)
echo "public IPv4: $IP"

# --- packages ----------------------------------------------------------------
echo "postfix postfix/main_mailer_type select Internet Site" | debconf-set-selections
echo "postfix postfix/mailname string $DOMAIN" | debconf-set-selections
DEBIAN_FRONTEND=noninteractive apt-get install -y postfix opendkim opendkim-tools dns-root-data curl

# --- DKIM key ----------------------------------------------------------------
install -d -o opendkim -g opendkim -m 750 "$KEYDIR"
if [[ ! -f $KEYDIR/$SELECTOR.private ]]; then
    opendkim-genkey -b 2048 -h sha256 -d "$DOMAIN" -s "$SELECTOR" -D "$KEYDIR"
fi
chown opendkim:opendkim "$KEYDIR"/*
chmod 600 "$KEYDIR/$SELECTOR.private"

# --- OpenDKIM: sign outgoing (from localhost); verify incoming --------------
# AuthservID is what `mynews inbound` trusts. OpenDKIM strips incoming
# Authentication-Results headers that claim this id, so they can't be forged.
[[ -f /etc/opendkim.conf && ! -f /etc/opendkim.conf.orig ]] && cp /etc/opendkim.conf /etc/opendkim.conf.orig
cat > /etc/opendkim.conf <<EOF
# managed by mynews/deploy/mail-setup.sh
Syslog                  yes
UMask                   007
UserID                  opendkim
Mode                    sv
Canonicalization        relaxed/simple
OversignHeaders         From
Domain                  $DOMAIN
AuthservID              $DOMAIN
Selector                $SELECTOR
KeyFile                 $KEYDIR/$SELECTOR.private
Socket                  inet:8891@localhost
PidFile                 /run/opendkim/opendkim.pid
TrustAnchorFile         /usr/share/dns/root.key
EOF
# Distro units differ in how they pass the socket; pin it to our config.
install -d /etc/systemd/system/opendkim.service.d
cat > /etc/systemd/system/opendkim.service.d/mynews.conf <<'EOF'
[Service]
RuntimeDirectory=opendkim
PIDFile=/run/opendkim/opendkim.pid
ExecStart=
ExecStart=/usr/sbin/opendkim -x /etc/opendkim.conf
EOF

# --- Postfix -----------------------------------------------------------------
echo "$DOMAIN" > /etc/mailname
postconf -e \
    "myhostname = $DOMAIN" \
    'myorigin = $myhostname' \
    'inet_protocols = ipv4' \
    'relayhost =' \
    'smtp_tls_security_level = may' \
    'smtpd_milters = inet:localhost:8891' \
    'non_smtpd_milters = $smtpd_milters' \
    'milter_default_action = accept' \
    'milter_protocol = 6' \
    'disable_vrfy_command = yes' \
    'smtpd_helo_required = yes'

if [[ $INBOUND == 1 ]]; then
    HOME_DIR=$(getent passwd "$RUN_USER" | cut -d: -f6)
    # accept only mynews@DOMAIN from the outside world
    echo "mynews@$DOMAIN OK" > /etc/postfix/mynews_recipients
    postmap /etc/postfix/mynews_recipients
    postconf -e \
        'inet_interfaces = all' \
        'mydestination = $myhostname, localhost' \
        'smtpd_recipient_restrictions = permit_mynetworks, check_recipient_access hash:/etc/postfix/mynews_recipients, reject' \
        'smtpd_tls_security_level = may' \
        'message_size_limit = 5242880'
    # mynews@ -> the user, whose ~/.forward pipes into `mynews inbound`
    sed -i '/^mynews:/d' /etc/aliases
    echo "mynews: $RUN_USER" >> /etc/aliases
    newaliases
    echo "\"|$HOME_DIR/mynews/deploy/inbound.sh\"" > "$HOME_DIR/.forward"
    chown "$RUN_USER": "$HOME_DIR/.forward"
    chmod 644 "$HOME_DIR/.forward"
    if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
        ufw allow 25/tcp
    fi
else
    postconf -e 'inet_interfaces = loopback-only' 'mydestination = localhost'
fi

systemctl daemon-reload
systemctl enable --now opendkim postfix
systemctl restart opendkim postfix

# --- checks ------------------------------------------------------------------
echo
if timeout 8 bash -c '</dev/tcp/gmail-smtp-in.l.google.com/25' 2>/dev/null; then
    echo "OK: outbound port 25 is open"
else
    echo "WARNING: cannot reach gmail-smtp-in.l.google.com:25 - provider may block port 25"
fi

DKIM_VALUE=$(tr -d '\n\t' < "$KEYDIR/$SELECTOR.txt" | grep -o '"[^"]*"' | tr -d '"' | tr -d ' \n')
MX_LINE=""
[[ $INBOUND == 1 ]] && MX_LINE="
  MX   $DOMAIN
       10 $DOMAIN.      (receives your feedback replies)
"

cat <<EOF

================================================================================
Add these DNS records (at your DNS provider, in the zone of the parent domain).
Names are fully qualified; in most panels enter only the part before the parent
domain (e.g. "news" / "mynews._domainkey.news" / "_dmarc.news").

  A    $DOMAIN
       $IP
$MX_LINE
  TXT  $DOMAIN
       v=spf1 ip4:$IP -all

  TXT  $SELECTOR._domainkey.$DOMAIN
       $DKIM_VALUE

  TXT  _dmarc.$DOMAIN
       v=DMARC1; p=none; adkim=s; aspf=s

And at your VPS provider set reverse DNS (PTR) for $IP to:  $DOMAIN

Then in ~/mynews/.env:
  SMTP_HOST=localhost
  SMTP_PORT=25
  SMTP_STARTTLS=false
  SMTP_USER=
  SMTP_PASSWORD=
  MAIL_FROM=mynews <mynews@$DOMAIN>
  MAIL_TO=<your inbox>          # replies are accepted only from this address

After DNS propagates, verify:
  opendkim-testkey -d $DOMAIN -s $SELECTOR -vvv     # expect "key OK"
  (cd ~/mynews && uv run mynews render --send)       # then in Gmail: "Show original"
                                                     # SPF, DKIM and DMARC should all say PASS
  # reply to that email with any text, then:
  (cd ~/mynews && uv run mynews feedback list)       # your reply should be listed with "*"
================================================================================
EOF
