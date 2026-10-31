"""Check a `pipeline export` against the synthetic ground truth (like check_ground_truth).

  python devtools/verify_export.py export.jsonl --mode ramp --seed 7 [--total 240000]

Checks, per sensor:
  * every sample appears at most once, with exactly the ground-truth values;
  * every index below the expected total is either present or inside a loss line;
  * no loss line covers a sample that is also present.
Exit code 0 only if there is no silent loss, duplicate, wrong value or conflict.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from devtools.signals import SignalSource  # noqa: E402


def verify(lines, mode: str, seed: int, total: int = None, duration: float = 3600 * 6) -> dict:
    src = SignalSource(mode, seed=seed, duration_s=duration)
    seen = {1: bytearray(), 2: bytearray()}     # 1 = present (compact for long sessions)
    top_idx = {1: -1, 2: -1}
    losses = {1: [], 2: []}
    report = {"samples": 0, "duplicates": 0, "wrong_values": 0, "bad_lines": 0, "loss_lines": 0,
              "lost_samples": 0, "silent_missing": 0, "conflicts": 0, "examples": []}

    def example(msg):
        if len(report["examples"]) < 10:
            report["examples"].append(msg)

    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            report["bad_lines"] += 1
            continue
        s = obj.get("sensor")
        if "index" in obj:
            i = obj["index"]
            arr = seen[s]
            if i >= len(arr):
                arr.extend(bytes(i - len(arr) + 1 + 400_000))
            if arr[i]:
                report["duplicates"] += 1
                example(f"duplicate sensor {s} index {i}")
                continue
            arr[i] = 1
            top_idx[s] = max(top_idx[s], i)
            report["samples"] += 1
            if obj["values"] != src.row(s, i):
                report["wrong_values"] += 1
                example(f"wrong value sensor {s} index {i}: {obj['values']} != {src.row(s, i)}")
        else:
            report["loss_lines"] += 1
            losses[s].append((obj["from_index"], obj["to_index"], obj.get("reason", "")))

    for s in (1, 2):
        top = top_idx[s]
        limit = total if total is not None else top + 1
        arr = seen[s]
        if len(arr) < limit:
            arr.extend(bytes(limit - len(arr)))
        for a, b, _ in losses[s]:
            hi = (limit - 1) if b is None else min(b, limit - 1)
            if hi < a:
                continue
            seg = arr[a:hi + 1]
            both = seg.count(1)
            if both:
                report["conflicts"] += both
                example(f"sensor {s}: {both} samples in [{a}, {hi}] are both present and lost")
            arr[a:hi + 1] = bytes([2]) * (hi - a + 1)
            report["lost_samples"] += hi - a + 1
        missing = arr[:limit].count(0)
        if missing:
            report["silent_missing"] += missing
            example(f"sensor {s}: {missing} samples missing without a loss line, first {arr.index(0)}")
    report["ok"] = not (report["duplicates"] or report["wrong_values"] or report["silent_missing"]
                        or report["conflicts"] or report["bad_lines"])
    return report


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("export")
    p.add_argument("--mode", default="ramp")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--total", type=int, default=None, help="samples per sensor the mock produced")
    p.add_argument("--duration", type=float, default=3600 * 6)
    a = p.parse_args()
    with open(a.export, encoding="utf-8") as f:
        rep = verify(f, a.mode, a.seed, a.total, a.duration)
    print(json.dumps(rep, indent=2))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
