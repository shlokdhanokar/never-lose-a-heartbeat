"""Resource figures for the submission (brief section 5, item 4), Linux only.

  sudo python3 devtools/measure_resources.py --minutes 60 --out evidence/resources.json

Samples /proc every 2 s for the pipeline (and the mock, if found):
  average CPU (percent of one core, and of the 4-core board), peak memory
  (VmHWM, the kernel's own high-water mark), megabytes written to disk
  (/proc/<pid>/io write_bytes: what reached the block layer), and megabytes
  sent over the network (interface tx bytes, plus the pipeline's own count).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import time


def main_pid(unit: str) -> int:
    try:
        out = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", unit],
                             capture_output=True, text=True).stdout.strip()
        return int(out or 0)
    except (OSError, ValueError):
        return 0


def find_pid(pattern: str) -> int:
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                with open(f"/proc/{d}/cmdline", "rb") as f:
                    if pattern.encode() in f.read() and int(d) != os.getpid():
                        return int(d)
            except OSError:
                pass
    return 0


def proc_sample(pid: int):
    try:
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().rsplit(")", 1)[1].split()
        ticks = int(parts[11]) + int(parts[12])
        hwm = rss = 0
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmHWM:"):
                    hwm = int(line.split()[1])
                elif line.startswith("VmRSS:"):
                    rss = int(line.split()[1])
        wb = 0
        with open(f"/proc/{pid}/io") as f:
            for line in f:
                if line.startswith("write_bytes:"):
                    wb = int(line.split()[1])
        return ticks, hwm, rss, wb
    except (OSError, IndexError, ValueError):
        return None


def default_iface() -> str:
    with open("/proc/net/route") as f:
        for line in f.readlines()[1:]:
            p = line.split()
            if p[1] == "00000000":
                return p[0]
    return "eth0"


def tx_bytes(iface: str) -> int:
    with open("/proc/net/dev") as f:
        for line in f:
            if line.strip().startswith(iface + ":"):
                return int(line.split(":", 1)[1].split()[8])
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--out", default="evidence/resources.json")
    ap.add_argument("--status", default="/var/lib/hbpipe/status.json")
    a = ap.parse_args()
    hz = os.sysconf("SC_CLK_TCK")
    iface = default_iface()
    targets = {"pipeline": lambda: main_pid("pipeline.service") or find_pid("pipeline"),
               "mock": lambda: main_pid("biosignal-mock.service") or find_pid("biosignal-mock")}
    acc = {k: {"cpu_s": 0.0, "peak_kb": 0, "written": 0, "last": None, "pid": 0} for k in targets}
    tx0 = tx_bytes(iface)
    app0 = None
    try:
        with open(a.status) as f:
            app0 = json.load(f).get("bytes_sent")
    except (OSError, ValueError):
        pass
    t0 = time.monotonic()
    end = t0 + a.minutes * 60
    while time.monotonic() < end:
        for name, getpid in targets.items():
            st = acc[name]
            pid = getpid()
            s = proc_sample(pid) if pid else None
            if s is None:
                continue
            ticks, hwm, rss, wb = s
            if st["last"] is not None and st["pid"] == pid:
                st["cpu_s"] += (ticks - st["last"][0]) / hz
                st["written"] += max(0, wb - st["last"][3])
            st["last"], st["pid"] = s, pid
            st["peak_kb"] = max(st["peak_kb"], hwm, rss)
        time.sleep(2)
    elapsed = time.monotonic() - t0
    app1 = None
    try:
        with open(a.status) as f:
            app1 = json.load(f).get("bytes_sent")
    except (OSError, ValueError):
        pass
    report = {
        "environment": {"machine": platform.machine(), "kernel": platform.release(),
                        "cpus_visible": os.cpu_count(), "model": open("/proc/device-tree/model").read().strip("\0")
                        if os.path.exists("/proc/device-tree/model") else platform.node()},
        "duration_s": round(elapsed),
        "network_interface": iface,
        "network_sent_mb_interface": round((tx_bytes(iface) - tx0) / 1e6, 2),
        "network_sent_mb_pipeline": round((app1 - app0) / 1e6, 2) if app0 is not None and app1 is not None else None,
    }
    for name, st in acc.items():
        report[name] = {"avg_cpu_percent_of_one_core": round(100 * st["cpu_s"] / elapsed, 2),
                        "avg_cpu_percent_of_4_cores": round(100 * st["cpu_s"] / elapsed / 4, 2),
                        "peak_memory_mb": round(st["peak_kb"] / 1024, 1),
                        "disk_written_mb": round(st["written"] / 1e6, 2)}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
