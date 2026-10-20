#!/usr/bin/env bash
# One-shot setup for a fresh Ubuntu 24.04 Hetzner box. Idempotent — safe to
# re-run. Run as root right after `installimage` + first login:
#
#   apt-get update && apt-get install -y git
#   git clone <backend repo> /opt/balla && cd /opt/balla/infra/live
#   cp .env.example .env && nano .env        # fill PUBLIC_IP, LIVE_DOMAIN, secrets
#   bash bootstrap.sh
#
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] || { echo ".env missing — copy .env.example and fill it in"; exit 1; }
# shellcheck disable=SC1091
source .env
for v in PUBLIC_IP LIVE_DOMAIN ACME_EMAIL DJANGO_HOOK_BASE SRS_HOOK_SECRET TURN_SECRET; do
  [ -n "${!v:-}" ] || { echo "$v is empty in .env"; exit 1; }
done

echo "== packages"
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg ufw fail2ban unattended-upgrades >/dev/null

echo "== docker"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin >/dev/null
fi

echo "== firewall"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp     comment 'ssh'
ufw allow 80/tcp     comment 'acme'
ufw allow 443/tcp    comment 'https + turn alt'
ufw allow 443/udp    comment 'turn alt'
ufw allow 8000/udp   comment 'srs webrtc'
ufw allow 8001/tcp   comment 'srs webrtc tcp'
ufw allow 1935/tcp   comment 'rtmp ingest (obs)'
ufw allow 3478/udp   comment 'turn'
ufw allow 3478/tcp   comment 'turn'
ufw allow 5349/tcp   comment 'turns'
ufw allow 49160:49200/udp comment 'turn relay'
ufw --force enable >/dev/null
ufw status numbered | sed 's/^/   /'

echo "== kernel: UDP buffers for WebRTC"
cat > /etc/sysctl.d/90-webrtc.conf <<'EOF'
net.core.rmem_max=26214400
net.core.rmem_default=1048576
net.core.wmem_max=26214400
net.core.wmem_default=1048576
net.ipv4.udp_mem=65536 131072 262144
EOF
sysctl -q --system

echo "== dns check"
resolved=$(getent hosts "$LIVE_DOMAIN" | awk '{print $1}' | head -1 || true)
if [ "$resolved" != "$PUBLIC_IP" ]; then
  echo "   WARNING: $LIVE_DOMAIN resolves to '${resolved:-nothing}', expected $PUBLIC_IP — Caddy cannot get a certificate until DNS is right."
fi

echo "== start stack"
docker compose pull -q
docker compose up -d
sleep 5
docker compose ps

echo
echo "== smoke"
curl -fsS "http://127.0.0.1:1985/api/v1/versions" && echo "   srs api ok" || echo "   srs api NOT answering"
echo "   TLS: curl -I https://$LIVE_DOMAIN/  (after DNS propagates)"
echo "   Publish test from a laptop with OBS: rtmp://$PUBLIC_IP/live/test?token=<publish token from Django>"
echo "done"
