#!/usr/bin/env bash
# Failure tests on the real ARM64 board (Linux). Each command logs to
# evidence/drill-log.txt with UTC timestamps, so the results can be submitted.
#
#   sudo bash devtools/fault_drill.sh netcut 60        cut the network to the cloud for 60 s
#   sudo bash devtools/fault_drill.sh kill             kill -9 the pipeline (systemd restarts it)
#   sudo bash devtools/fault_drill.sh powercut         hard reset with no sync (like pulling power)
#   sudo bash devtools/fault_drill.sh after-reboot     record the state after a power cut / reboot
#   sudo bash devtools/fault_drill.sh reboot           clean reboot
#   sudo bash devtools/fault_drill.sh diskfill 30      fill the data disk for 30 s, then free it
#   sudo bash devtools/fault_drill.sh clock "+2 hours" jump the clock (NTP off), then restore
#   sudo bash devtools/fault_drill.sh verify SESSION   export both copies and check them
#
# On a real Raspberry Pi, a real power cut means pulling the plug: run
# `fault_drill.sh mark "pulling power"` first, then `after-reboot` once it is back.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
EVID="$HERE/evidence"
LOG="$EVID/drill-log.txt"
CONF="${HBPIPE_CONFIG:-/etc/hbpipe/pipeline.conf}"
DATA_DIR="$(awk -F= '/^data_dir/{gsub(/ /,"",$2); print $2}' "$CONF")"
CLOUD_URL="$(awk -F= '/^cloud_url/{gsub(/ /,"",$2); print $2}' "$CONF")"
mkdir -p "$EVID"

stamp() { date -u +%Y-%m-%dT%H:%M:%S.%3NZ; }
log() { echo "$(stamp) $*" | tee -a "$LOG"; }
status() { pipeline --config "$CONF" status 2>/dev/null || echo '{"error": "status failed"}'; }
watch_mode() {   # print the mode every 0.5 s for $1 seconds
  local end=$(( $(date +%s) + $1 ))
  while (( $(date +%s) < end )); do
    echo "$(stamp) $(status | python3 -c 'import json,sys; s=json.load(sys.stdin); print(s.get("mode"), "saved", s.get("samples_saved"), "cloud", s.get("samples_in_cloud"), "backlog", s.get("backlog"))')" >> "$LOG"
    sleep 0.5
  done
}
cloud_ip() {
  local host; host="$(python3 -c "import urllib.parse,sys; print(urllib.parse.urlsplit(sys.argv[1]).hostname)" "$CLOUD_URL")"
  getent ahostsv4 "$host" | awk '{print $1; exit}'
}

cmd="${1:-help}"; shift || true
case "$cmd" in
  mark)
    log "MARK $*"; log "status $(status)"; sync "$LOG" ;;
  netcut)
    secs="${1:-60}"; ip="$(cloud_ip)"
    log "NETCUT start: dropping all traffic to/from $ip for $secs s"; log "status $(status)"
    iptables -I OUTPUT -d "$ip" -j DROP; iptables -I INPUT -s "$ip" -j DROP
    watch_mode "$secs"
    iptables -D OUTPUT -d "$ip" -j DROP; iptables -D INPUT -s "$ip" -j DROP
    log "NETCUT end: traffic restored"
    watch_mode 30
    log "status $(status)" ;;
  kill)
    pid="$(systemctl show -p MainPID --value pipeline.service)"
    log "KILL -9 pipeline pid $pid"; log "status $(status)"
    kill -9 "$pid"
    watch_mode 15
    log "pipeline pid now $(systemctl show -p MainPID --value pipeline.service)"
    log "status $(status)" ;;
  powercut)
    log "POWERCUT: immediate reset without syncing (sysrq b)"; log "status $(status)"
    sync "$LOG"
    echo 1 > /proc/sys/kernel/sysrq
    echo b > /proc/sysrq-trigger ;;
  reboot)
    log "REBOOT (clean)"; log "status $(status)"; sync "$LOG"; systemctl reboot ;;
  after-reboot)
    log "AFTER REBOOT: uptime $(cut -d' ' -f1 /proc/uptime) s"
    watch_mode 10
    log "status $(status)"
    pipeline --config "$CONF" sessions | tee -a "$LOG"
    log "open-ended losses (samples the mock produced but the pipeline had not saved):"
    for s in $(pipeline --config "$CONF" sessions | python3 -c 'import json,sys; [print(json.loads(l)["session"]) for l in sys.stdin]'); do
      pipeline --config "$CONF" export --session "$s" --from device | grep '"to_index": null' | tee -a "$LOG" || true
    done ;;
  diskfill)
    secs="${1:-30}"
    free=$(df --output=avail -B1 "$DATA_DIR" | tail -1)
    log "DISKFILL: filling $DATA_DIR ($free bytes free) for $secs s"; log "status $(status)"
    fallocate -l $(( free - 4096 )) "$DATA_DIR/../hb-diskfill.bin" 2>/dev/null || \
      dd if=/dev/zero of="$DATA_DIR/../hb-diskfill.bin" bs=1M status=none || true
    watch_mode "$secs"
    rm -f "$DATA_DIR/../hb-diskfill.bin"
    log "DISKFILL end: space freed"
    watch_mode 10
    log "status $(status)" ;;
  clock)
    off="${1:-+2 hours}"
    log "CLOCK: NTP off, jumping clock by $off"; log "status $(status)"
    timedatectl set-ntp false || true
    date -s "$off" >/dev/null
    watch_mode 30
    timedatectl set-ntp true || true
    log "CLOCK: NTP back on"; log "status $(status)" ;;
  verify)
    s="${1:?session id}"
    pipeline --config "$CONF" export --session "$s" --from device > "$EVID/export-$s-device.jsonl"
    pipeline --config "$CONF" export --session "$s" --from cloud > "$EVID/export-$s-cloud.jsonl"
    log "VERIFY $s: $(wc -l < "$EVID/export-$s-device.jsonl") device lines, $(wc -l < "$EVID/export-$s-cloud.jsonl") cloud lines"
    if cmp -s "$EVID/export-$s-device.jsonl" "$EVID/export-$s-cloud.jsonl"; then log "cloud copy identical to device copy"; else log "cloud copy differs from device copy"; fi
    if command -v check_ground_truth >/dev/null; then
      check_ground_truth "$EVID/export-$s-device.jsonl" | tee -a "$LOG" || true
      check_ground_truth "$EVID/export-$s-cloud.jsonl" | tee -a "$LOG" || true
    else
      log "check_ground_truth not on PATH; run the official tool on the two export files"
    fi
    pipeline --config "$CONF" results --session "$s" > "$EVID/results-$s.jsonl"
    log "results: $(wc -l < "$EVID/results-$s.jsonl") lines" ;;
  *)
    sed -n '2,16p' "$0" ;;
esac
