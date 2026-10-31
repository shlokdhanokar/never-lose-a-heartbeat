"""Development stand-in for biosignal-mock (case brief section 6).

The official mock was not attached to the brief, so this one follows the
documented protocol to let the pipeline be built and tested. Judging uses the
official mock; nothing in hbpipe depends on this file.

  python devtools/mock_sim.py --mode ramp --seed 7 --duration 600 --port 7100 \
      [--speed 4] [--faults corrupt=0.02,dup=0.02,stall=0.002,dropout=0.002] [--info info.json]

Protocol: newline-delimited JSON over TCP, one client at a time.
  {"cmd": "read", "sensor": 1}                      next data (at most 1 s)
  {"cmd": "read", "sensor": 1, "from_index": N}     re-read from N
Keeps only the last 10 s per sensor; older unread data is reported in "lost".
"""
from __future__ import annotations

import argparse
import json
import os
import random
import socket
import sys
import threading
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from devtools.signals import RATE, SignalSource  # noqa: E402

BUFFER = 10 * RATE


class Mock:
    def __init__(self, args) -> None:
        self.src = SignalSource(args.mode, seed=args.seed, duration_s=args.duration)
        self.speed = args.speed
        self.total = self.src.total
        self.session_id = os.urandom(4).hex()
        self.t0 = time.time()
        self.m0 = time.monotonic()
        self.cursor = {1: 0, 2: 0}
        self.faults = {k: float(v) for k, v in
                       (f.split("=") for f in args.faults.split(",") if f)} if args.faults else {}
        self.rng = random.Random(args.seed * 31 + 1)
        self.dropouts = []          # (from, to) ranges of sensor 2 that were never captured
        self.fault_log = open(args.fault_log, "a") if args.fault_log else None

    def log_fault(self, **event) -> None:
        if self.fault_log:
            event["t"] = time.time()
            self.fault_log.write(json.dumps(event) + "\n")
            self.fault_log.flush()

    def produced(self) -> int:
        n = int((time.monotonic() - self.m0) * RATE * self.speed)
        return min(n, self.total)

    def _maybe_dropout(self, produced: int) -> None:
        p = self.faults.get("dropout", 0)
        if p and self.rng.random() < p:
            length = self.rng.choice((40, 400, 1200))
            a = produced
            self.dropouts.append((a, min(self.total - 1, a + length - 1)))
            self.log_fault(kind="dropout", sensor=2, from_index=a, to_index=a + length - 1)

    def reply(self, sensor: int, from_index) -> str:
        produced = self.produced()
        self._maybe_dropout(produced)
        start = self.cursor[sensor] if from_index is None else max(0, int(from_index))
        lost = []
        buf_lo = max(0, produced - BUFFER)
        if start < buf_lo:
            lost.append({"from_index": start, "to_index": buf_lo - 1, "reason": "buffer_overflow"})
            start = buf_lo
        waiting = False
        if sensor == 2:
            for a, b in sorted(self.dropouts):
                if a <= start <= b:
                    if b < produced:
                        lost.append({"from_index": start, "to_index": b, "reason": "sensor_dropout"})
                        start = b + 1
                    else:
                        waiting = True      # sensor 2 is silent right now
        end = start if waiting else min(produced, start + RATE)
        if sensor == 2:
            for a, b in self.dropouts:
                if start < a < end:
                    end = a
        rows = [self.src.row(sensor, i) for i in range(start, end)]
        self.cursor[sensor] = end
        text = json.dumps(rows)
        crc = format(zlib.crc32(text.encode()) & 0xFFFFFFFF, "08x")
        if self.faults.get("corrupt") and self.rng.random() < self.faults["corrupt"]:
            crc = format((int(crc, 16) ^ 0x5A5A) & 0xFFFFFFFF, "08x")
        session_end = produced >= self.total and end >= self.total
        return ('{"session_id": "%s", "sensor": %d, "first_index": %d, "count": %d, "lost": %s, '
                '"session_end": %s, "crc32": "%s", "data": %s}'
                % (self.session_id, sensor, start, len(rows), json.dumps(lost),
                   "true" if session_end else "false", crc, text))

    def serve(self, port: int) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        while True:
            conn, _ = srv.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.handle(conn)

    def handle(self, conn: socket.socket) -> None:
        buf = b""
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    return
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        req = json.loads(line)
                    except ValueError:
                        continue
                    if req.get("cmd") != "read" or req.get("sensor") not in (1, 2):
                        continue
                    p = self.faults.get("stall", 0)
                    if p and self.rng.random() < p:
                        pause = self.rng.choice((1.5, 4.0, 12.0)) / max(1.0, self.speed ** 0.5)
                        self.log_fault(kind="stall", seconds=pause)
                        time.sleep(pause)
                    out = (self.reply(req["sensor"], req.get("from_index")) + "\n").encode()
                    conn.sendall(out)
                    if self.faults.get("dup") and self.rng.random() < self.faults["dup"]:
                        conn.sendall(out)
        except OSError:
            return
        finally:
            conn.close()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=("signal", "ramp"), default="signal")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--duration", type=float, default=3600)
    p.add_argument("--port", type=int, default=7100)
    p.add_argument("--speed", type=float, default=1.0, help="time acceleration for tests")
    p.add_argument("--faults", default="")
    p.add_argument("--info", default="", help="write session id and start time here")
    p.add_argument("--fault-log", default="", help="append injected fault events (JSON lines) here")
    args = p.parse_args()
    mock = Mock(args)
    if args.info:
        with open(args.info, "w") as f:
            json.dump({"session_id": mock.session_id, "start_time": mock.t0, "speed": args.speed,
                       "mode": args.mode, "seed": args.seed, "duration": args.duration}, f)
    print(f"mock session {mock.session_id} on port {args.port} ({args.mode}, x{args.speed})", flush=True)
    mock.serve(args.port)


if __name__ == "__main__":
    main()
