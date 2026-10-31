#!/usr/bin/env bash
# Install the pipeline on a fresh Raspberry Pi OS / Debian / Ubuntu ARM64 board.
#   sudo ./deploy/install_device.sh
# Needs only python3 (3.9+), which Raspberry Pi OS already includes.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "run with sudo"; exit 1; fi
RUN_USER="${SUDO_USER:-pi}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"

command -v python3 >/dev/null || { apt-get update && apt-get install -y python3; }
python3 - <<'EOF'
import sys
assert sys.version_info >= (3, 9), "python 3.9+ required"
EOF

echo "== installing to /opt/hbpipe (service user: $RUN_USER)"
mkdir -p /opt/hbpipe
cp -r "$SRC/hbpipe" "$SRC/pipeline" "$SRC/devtools" /opt/hbpipe/
chmod +x /opt/hbpipe/pipeline
ln -sf /opt/hbpipe/pipeline /usr/local/bin/pipeline

mkdir -p /etc/hbpipe /var/lib/hbpipe
if [[ ! -f /etc/hbpipe/pipeline.conf ]]; then
  cp "$SRC/deploy/pipeline.conf.example" /etc/hbpipe/pipeline.conf
  echo "!! edit /etc/hbpipe/pipeline.conf: set cloud_url and cloud_token"
fi
chown -R "$RUN_USER:$RUN_USER" /var/lib/hbpipe
chown root:"$RUN_USER" /etc/hbpipe/pipeline.conf
chmod 640 /etc/hbpipe/pipeline.conf

echo "== systemd units"
install -m 644 "$SRC/deploy/raspi-limits.slice" /etc/systemd/system/raspi-limits.slice
sed "s/@USER@/$RUN_USER/g" "$SRC/deploy/pipeline.service" > /etc/systemd/system/pipeline.service
if [[ -x /opt/biosignal-mock/biosignal-mock ]]; then
  sed "s/@USER@/$RUN_USER/g" "$SRC/deploy/biosignal-mock.service" > /etc/systemd/system/biosignal-mock.service
  systemctl enable biosignal-mock.service
fi
systemctl daemon-reload
systemctl enable --now pipeline.service

# ext4 + SQLite fsync is what makes a power cut safe. Show the mount options.
echo "== storage: $(findmnt -no SOURCE,FSTYPE,OPTIONS --target /var/lib/hbpipe || true)"
sleep 2
systemctl --no-pager --lines=5 status pipeline.service || true
echo
echo "Done. Check with:  pipeline status"
