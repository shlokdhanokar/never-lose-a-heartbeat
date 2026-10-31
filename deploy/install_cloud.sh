#!/usr/bin/env bash
# Install the cloud service on a free-tier Ubuntu/Debian VM
# (for example Oracle Cloud Always Free, or an Azure free-services B1s VM).
#   sudo ./deploy/install_cloud.sh your-host-name
# your-host-name: a DNS name pointing at the VM, e.g. 203-0-113-7.sslip.io
# (free, works for any public IP) or an Azure DNS label <name>.<region>.cloudapp.azure.com.
# Caddy then gets a free Let's Encrypt certificate automatically.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "run with sudo"; exit 1; fi
DOMAIN="${1:?usage: install_cloud.sh <dns-name>}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"

apt-get update
apt-get install -y python3 caddy || {
  echo "caddy not in apt; installing from the official Caddy repository"
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update && apt-get install -y caddy
}

id hbcloud >/dev/null 2>&1 || useradd --system --home /var/lib/hbcloud --shell /usr/sbin/nologin hbcloud
mkdir -p /opt/hbcloud /var/lib/hbcloud /etc/hbcloud
cp -r "$SRC/hbpipe" "$SRC/cloud" /opt/hbcloud/
chown -R hbcloud:hbcloud /var/lib/hbcloud
if [[ ! -f /etc/hbcloud/token ]]; then
  python3 -c "import secrets; print(secrets.token_urlsafe(32))" > /etc/hbcloud/token
fi
chown root:hbcloud /etc/hbcloud/token
chmod 640 /etc/hbcloud/token

install -m 644 "$SRC/deploy/hbcloud.service" /etc/systemd/system/hbcloud.service
sed "s/@DOMAIN@/$DOMAIN/g" "$SRC/deploy/Caddyfile" > /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable --now hbcloud.service
systemctl restart caddy

# open the firewall for HTTPS (cloud consoles also need an ingress rule for 80/443)
if command -v ufw >/dev/null && ufw status | grep -q active; then ufw allow 80,443/tcp; fi
if command -v iptables >/dev/null; then
  iptables -C INPUT -p tcp --dport 443 -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport 443 -j ACCEPT
  iptables -C INPUT -p tcp --dport 80 -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport 80 -j ACCEPT
fi

sleep 3
echo
echo "Cloud URL:   https://$DOMAIN"
echo "Token:       $(cat /etc/hbcloud/token)"
echo "Health:      curl https://$DOMAIN/health"
echo "Put both into /etc/hbpipe/pipeline.conf on the device (cloud_url, cloud_token)."
