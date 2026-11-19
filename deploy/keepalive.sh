#!/bin/sh
# Keeps the instance from being reclaimed, and keeps the DNS record honest.
#
# Oracle may reclaim an Always Free instance it decides is idle. Its own
# definition: over a continuous 7-day window, 95th-percentile CPU under 20%,
# network under 20%, and - on Ampere A1 shapes only - memory under 20%. ALL of
# those must hold, so defeating any one of them is enough.
#
# A clip finder that nobody scans with for a week meets all three comfortably,
# which is how you lose the machine. This does two cheap things on a timer:
# fetches its own page over the public hostname, which is real network traffic
# through the real path, and asks DuckDNS to confirm the address.
#
# This is not an attempt to look busier than it is to get something for free -
# it is a health check that happens to satisfy the policy. It costs a few
# kilobytes an hour and it verifies the thing visitors actually use, so a
# certificate that failed to renew or a container that died shows up in the
# log rather than when a friend complains.
#
# Install (see ORACLE.md):
#   sudo cp keepalive.sh /usr/local/bin/clipsvalorant-keepalive
#   sudo chmod +x /usr/local/bin/clipsvalorant-keepalive
#   then the systemd timer, or a crontab line:
#   17 * * * * /usr/local/bin/clipsvalorant-keepalive >> /var/log/clipsvalorant-keepalive.log 2>&1

set -eu

# Read SITE_HOST, DUCKDNS_DOMAIN and DUCKDNS_TOKEN without sourcing the file,
# so a stray shell character in a secret cannot be executed.
ENV_FILE=/opt/clipsvalorant/deploy/.env
val() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -n 1 | cut -d= -f2- || true; }

SITE_HOST=$(val SITE_HOST)
DUCKDNS_DOMAIN=$(val DUCKDNS_DOMAIN)
DUCKDNS_TOKEN=$(val DUCKDNS_TOKEN)

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# 1. Point the hostname at wherever this machine currently is. Oracle's
#    ephemeral public IP survives a reboot but not every kind of recreation,
#    and a stale record is indistinguishable from a dead site.
if [ -n "${DUCKDNS_DOMAIN:-}" ] && [ -n "${DUCKDNS_TOKEN:-}" ]; then
	# ip= left empty tells DuckDNS to use the source address of this request.
	out=$(curl -fsS --max-time 30 \
		"https://www.duckdns.org/update?domains=${DUCKDNS_DOMAIN}&token=${DUCKDNS_TOKEN}&ip=" \
		|| echo "REQUEST_FAILED")
	echo "$(stamp) duckdns: ${out}"
fi

# 2. Fetch the health endpoint over the public hostname. Deliberately not
#    localhost: this way it exercises DNS, Caddy, the certificate and the app,
#    which is the whole chain a visitor depends on.
if [ -n "${SITE_HOST:-}" ]; then
	code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 \
		"https://${SITE_HOST}/healthz" || echo "000")
	echo "$(stamp) healthz: ${code}"
	[ "$code" = "200" ] || echo "$(stamp) WARNING: the site did not answer 200"
fi
