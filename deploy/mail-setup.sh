#!/usr/bin/env bash
# Send-only mail server for mynews: Postfix (listens on localhost only) + OpenDKIM signing.
# Debian/Ubuntu. Idempotent: safe to re-run.
#
#   sudo ./deploy/mail-setup.sh news.example.pl
#
# Use a dedicated SUBDOMAIN as the sending domain, so existing mail on the root domain
# (MX/SPF at your provider) is left untouched.
set -euo pipefail

DOMAIN=${1:?usage: sudo $0 <sending subdomain, e.g. news.example.pl>}
SELECTOR=mynews
KEYDIR=/etc/opendkim/keys/$DOMAIN

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
command -v apt-get >/dev/null || { echo "this script supports Debian/Ubuntu only"; exit 1; }

IP=$(curl -4 -fsS https://api.ipify.org)
echo "public IPv4: $IP"

# --- packages ----------------------------------------------------------------
echo "postfix postfix/main_mailer_type select Internet Site" | debconf-set-selections
echo "postfix postfix/mailname string $DOMAIN" | debconf-set-selections
DEBIAN_FRONTEND=noninteractive apt-get install -y postfix opendkim opendkim-tools curl

# --- DKIM key ----------------------------------------------------------------
install -d -o opendkim -g opendkim -m 750 "$KEYDIR"
if [[ ! -f $KEYDIR/$SELECTOR.private ]]; then
    opendkim-genkey -b 2048 -h sha256 -d "$DOMAIN" -s "$SELECTOR" -D "$KEYDIR"
fi
chown opendkim:opendkim "$KEYDIR"/*
chmod 600 "$KEYDIR/$SELECTOR.private"

# --- OpenDKIM ----------------------------------------------------------------
[[ -f /etc/opendkim.conf && ! -f /etc/opendkim.conf.orig ]] && cp /etc/opendkim.conf /etc/opendkim.conf.orig
cat > /etc/opendkim.conf <<EOF
# managed by mynews/deploy/mail-setup.sh
Syslog                  yes
UMask                   007
UserID                  opendkim
Mode                    s
Canonicalization        relaxed/simple
OversignHeaders         From
Domain                  $DOMAIN
Selector                $SELECTOR
KeyFile                 $KEYDIR/$SELECTOR.private
Socket                  inet:8891@localhost
PidFile                 /run/opendkim/opendkim.pid
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

# --- Postfix: send-only, loopback, IPv4 (rDNS is set for IPv4) ----------------
echo "$DOMAIN" > /etc/mailname
postconf -e \
    "myhostname = $DOMAIN" \
    'myorigin = $myhostname' \
    'mydestination = localhost' \
    'inet_interfaces = loopback-only' \
    'inet_protocols = ipv4' \
    'relayhost =' \
    'smtp_tls_security_level = may' \
    'smtpd_milters = inet:localhost:8891' \
    'non_smtpd_milters = $smtpd_milters' \
    'milter_default_action = accept' \
    'milter_protocol = 6'

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

cat <<EOF

================================================================================
Add these DNS records (at your DNS provider, in the zone of the parent domain).
Names are fully qualified; in most panels enter only the part before the parent
domain (e.g. "news" / "mynews._domainkey.news" / "_dmarc.news").

  A    $DOMAIN
       $IP

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
  MAIL_TO=<your inbox>

After DNS propagates, verify:
  opendkim-testkey -d $DOMAIN -s $SELECTOR -vvv     # expect "key OK"
  (cd ~/mynews && uv run mynews render --send)       # then in Gmail: "Show original"
                                                     # SPF, DKIM and DMARC should all say PASS
================================================================================
EOF
