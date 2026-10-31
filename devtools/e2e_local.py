"""End-to-end failure drill on one machine (any OS).

Starts the development mock, the cloud service, a "network" proxy between the
device and the cloud, and the pipeline. Then injects faults on a timeline:

  network cut       the proxy black-holes traffic (packets vanish, nothing is refused)
  network restore
  process kill      kill -9 of the pipeline, restarted after 1 s (as systemd would)
  power cut         mock and pipeline killed together and restarted (new mock
                    session). On Linux use devtools/fault_drill.sh for a real
                    hard reset (sysrq) of the board.
  mock restart      the mock is restarted alone (new session)

At the end it exports both copies, checks them against the ground truth, checks
every result against tests/reference.py, measures mode-switch times and online
result latency, and writes evidence/<name>.json and .md.

  python devtools/e2e_local.py --name drill --mode signal --speed 4 --duration 900 \
      --faults corrupt=0.01,dup=0.01,stall=0.001,dropout=0.001
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from devtools.verify_export import verify  # noqa: E402
from tests.reference import reference_results  # noqa: E402

PY = sys.executable
FIELDS = ("missing_1", "missing_2", "complete", "mean_1", "min_1", "max_1", "mean_2", "min_2", "max_2",
          "magnitude", "heart_rate")


def file_hash(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class BlackholeProxy:
    """TCP forwarder. cut(): bytes are silently dropped and no replies flow,
    like a pulled cable. restore(): forwarding resumes for new connections."""

    def __init__(self, target_port: int) -> None:
        self.target = ("127.0.0.1", target_port)
        self.port = free_port()
        self.is_cut = False
        self.conns = []
        self.lock = threading.Lock()
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", self.port))
        self.srv.listen(16)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            c, _ = self.srv.accept()
            if self.is_cut:
                with self.lock:
                    self.conns.append((c, None))   # held open, never answered
                continue
            try:
                u = socket.create_connection(self.target, timeout=5)
            except OSError:
                c.close()
                continue
            with self.lock:
                self.conns.append((c, u))
            for a, b in ((c, u), (u, c)):
                threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    def _pump(self, a, b) -> None:
        try:
            while True:
                data = a.recv(65536)
                if not data:
                    break
                if self.is_cut:
                    continue          # dropped on the floor
                b.sendall(data)
        except OSError:
            pass
        finally:
            for s in (a, b):
                try:
                    s.close()
                except OSError:
                    pass

    def cut(self) -> None:
        self.is_cut = True

    def restore(self) -> None:
        self.is_cut = False
        with self.lock:
            conns, self.conns = self.conns, []
        for c, u in conns:            # stalled connections are dead after an outage
            for s in (c, u):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass


class Drill:
    def __init__(self, args) -> None:
        self.args = args
        self.dir = tempfile.mkdtemp(prefix="hb-drill-")
        self.mock_port, self.cloud_port = free_port(), free_port()
        self.token = "drill-token"
        self.procs = {}
        self.timeline = []
        self.status_log = []
        self.mock_infos = []
        self.t_start = time.time()
        self.start_cloud()
        time.sleep(1.0)
        self.proxy = BlackholeProxy(self.cloud_port)
        self.conf = os.path.join(self.dir, "pipeline.conf")
        with open(self.conf, "w") as f:
            f.write("[pipeline]\n"
                    f"mock_port = {self.mock_port}\n"
                    f"data_dir = {os.path.join(self.dir, 'device')}\n"
                    f"cloud_url = http://127.0.0.1:{self.proxy.port}\n"
                    f"cloud_token = {self.token}\n"
                    "device_id = drill-device\n"
                    "ballast_mb = 8\n")

    # ------------------------------------------------------------ processes

    def start_cloud(self) -> None:
        self.cloud = subprocess.Popen([PY, "-m", "cloud.server", "--db", os.path.join(self.dir, "cloud", "cloud.db"),
                                       "--port", str(self.cloud_port)], cwd=ROOT,
                                      env=dict(os.environ, HBCLOUD_TOKEN=self.token),
                                      stdout=open(os.path.join(self.dir, "cloud.log"), "a"), stderr=subprocess.STDOUT)

    def start_mock(self) -> None:
        info = os.path.join(self.dir, f"mock{len(self.mock_infos)}.json")
        a = self.args
        self.procs["mock"] = subprocess.Popen(
            [PY, "devtools/mock_sim.py", "--mode", a.mode, "--seed", str(a.seed), "--duration", str(a.duration),
             "--port", str(self.mock_port), "--speed", str(a.speed), "--faults", a.faults, "--info", info,
             "--fault-log", os.path.join(self.dir, "faults.jsonl")],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            if os.path.exists(info):
                break
            time.sleep(0.05)
        with open(info) as f:
            self.mock_infos.append(json.load(f))

    def start_pipeline(self) -> None:
        log = open(os.path.join(self.dir, "pipeline.log"), "a")
        self.procs["pipeline"] = subprocess.Popen([PY, "pipeline", "--config", self.conf, "run"], cwd=ROOT,
                                                  stdout=log, stderr=subprocess.STDOUT)

    def kill(self, name: str) -> None:
        self.procs[name].kill()
        self.procs[name].wait()

    def cli(self, *args) -> str:
        return subprocess.run([PY, "pipeline", "--config", self.conf, *args], cwd=ROOT,
                              capture_output=True, text=True, timeout=600).stdout

    def status(self):
        try:
            return json.loads(self.cli("status").strip().splitlines()[-1])
        except (ValueError, IndexError):
            return None

    def mark(self, what: str) -> None:
        self.timeline.append((time.time(), what))
        print(f"[{time.time() - self.t_start:7.1f}s] {what}", flush=True)

    # ------------------------------------------------------------ scenario

    def watch_status(self, stop: threading.Event) -> None:
        while not stop.is_set():
            st = self.status()
            if st:
                self.status_log.append((time.time(), st.get("mode"), st.get("backlog")))
                with open(os.path.join(self.dir, "status_log.jsonl"), "a") as f:
                    f.write(json.dumps([time.time(), st.get("mode"), st.get("backlog")]) + "\n")
            time.sleep(0.5)

    def run(self) -> dict:
        a = self.args
        self.start_mock()
        self.mark("mock started")
        self.start_pipeline()
        self.mark("pipeline started")
        stop = threading.Event()
        threading.Thread(target=self.watch_status, args=(stop,), daemon=True).start()
        events = []
        for spec in a.events.split(","):
            if spec:
                t, what = spec.split(":")
                events.append((float(t), what))
        for t, what in sorted(events):
            time.sleep(max(0.0, self.t_start + t - time.time()))
            if what == "cut":
                self.proxy.cut()
                self.mark("network cut")
            elif what == "restore":
                self.proxy.restore()
                self.mark("network restored")
            elif what == "kill":
                self.kill("pipeline")
                self.mark("pipeline killed (kill -9)")
                time.sleep(1.0)
                self.start_pipeline()
                self.mark("pipeline restarted")
            elif what == "power":
                self.kill("pipeline")
                self.kill("mock")
                self.mark("power cut (mock + pipeline killed)")
                time.sleep(2.0)
                self.start_mock()
                self.start_pipeline()
                self.mark("power restored (new mock session, pipeline restarted)")
            elif what == "cloudkill":
                self.cloud.kill()
                self.cloud.wait()
                self.mark("cloud service killed (kill -9)")
                time.sleep(8.0)
                self.start_cloud()
                self.mark("cloud service restarted")
            elif what == "longkill":
                self.kill("pipeline")
                self.mark("pipeline killed and kept down longer than the mock buffer")
                time.sleep(12.0 / self.args.speed + 3.0)
                self.start_pipeline()
                self.mark("pipeline restarted")
            elif what == "mockrestart":
                self.kill("mock")
                self.mark("mock killed")
                time.sleep(0.5)
                self.start_mock()
                self.mark("mock restarted (new session)")
        # wait for the last session to end and the backlog to drain
        last = self.mock_infos[-1]
        end_at = last["start_time"] + last["duration"] / last["speed"] + 5
        time.sleep(max(0.0, end_at - time.time()))
        deadline = time.time() + 900
        while time.time() < deadline:
            st = self.status()
            if st and st["backlog"] == 0 and st["mode"] == "online":
                break
            time.sleep(1)
        self.mark("backlog drained")
        time.sleep(4.0)       # let the cloud finish its last windows
        stop.set()
        return self.check()

    # ------------------------------------------------------------ checks

    def check(self) -> dict:
        a = self.args
        report = {"name": a.name, "params": vars(a), "platform": sys.platform,
                  "timeline": [(round(t - self.t_start, 2), w) for t, w in self.timeline], "sessions": []}
        sessions = [json.loads(l) for l in self.cli("sessions").splitlines() if l.strip()]
        info_by_session = {i["session_id"]: i for i in self.mock_infos}
        for s in sessions:
            sid = s["session"]
            info = info_by_session.get(sid, {})
            completed = bool(s["ended_1"] and s["ended_2"])
            total = int(info["duration"] * 400) if completed and info else None
            dev_path = os.path.join(self.dir, f"export-{sid}-device.jsonl")
            cloud_path = os.path.join(self.dir, f"export-{sid}-cloud.jsonl")
            with open(dev_path, "w") as f:
                f.write(self.cli("export", "--session", sid, "--from", "device"))
            with open(cloud_path, "w") as f:
                f.write(self.cli("export", "--session", sid, "--from", "cloud"))
            with open(dev_path) as f:
                dev = verify(f, a.mode, a.seed, total)
            with open(cloud_path) as f:
                cloud = verify(f, a.mode, a.seed, total)
            identical = file_hash(dev_path) == file_hash(cloud_path)
            res = self.check_results(sid, dev_path, info)
            report["sessions"].append({"session": sid, "completed_by_mock": completed, "device_export": dev,
                                       "cloud_export": cloud, "cloud_copy_identical_to_device": identical,
                                       "results": res})
        report["cli"] = self.cli_checks(sessions)
        report["mode_switches"] = self.switch_times()
        report["catch_up"] = self.catchup_times()
        report["pass"] = all(x["device_export"]["ok"] and x["cloud_export"]["ok"] and x["cloud_copy_identical_to_device"]
                             and x["results"]["mismatches"] == 0 and x["results"]["missing_online"] == 0
                             and x["results"]["online_over_5s"]["unexplained"] == 0
                             for x in report["sessions"]) and all(
            sw["seconds"] is not None and sw["seconds"] <= 10 for sw in report["mode_switches"]) and report["cli"]["all_ok"]
        return report

    def check_results(self, sid: str, export_path: str, info: dict) -> dict:
        s1, s2 = {}, {}
        top = -1
        cap = 400 * self.args.check_windows + 1 if self.args.check_windows else None
        with open(export_path) as f:
            for line in f:
                if '"index"' not in line:
                    continue
                o = json.loads(line)
                top = max(top, o["index"])
                if cap is None or o["index"] < cap:
                    (s1 if o["sensor"] == 1 else s2)[o["index"]] = o["values"]
        n = top + 1 if cap is None else min(top + 1, cap)
        ref = reference_results([s1.get(i) for i in range(n)], [s2.get(i) for i in range(n)], sid)
        if cap is not None:
            ref = [r for r in ref if r["window"] < self.args.check_windows]
        del s1, s2
        ref_by_w = {r["window"]: r for r in ref}
        total_windows = (top // 400) + 1 if top >= 0 else 0
        lines = [json.loads(l) for l in self.cli("results", "--session", sid).splitlines() if l.strip()]
        faults = []
        fpath = os.path.join(self.dir, "faults.jsonl")
        if os.path.exists(fpath):
            with open(fpath) as f:
                faults = [json.loads(l) for l in f if l.strip()]
        explained = unexplained = 0
        mism, examples = 0, []
        modes = {"online": 0, "offline": 0}
        latencies = []
        outage = self.outage_spans()
        for r in lines:
            modes[r["mode"]] = modes.get(r["mode"], 0) + 1
            exp = ref_by_w.get(r["window"])
            if exp is None:
                continue
            diff = [k for k in FIELDS if r[k] != exp[k]]
            if diff:
                mism += 1
                if len(examples) < 5:
                    examples.append({"window": r["window"], "mode": r["mode"], "fields": diff,
                                     "got": {k: r[k] for k in diff}, "expected": {k: exp[k] for k in diff}})
            if r["mode"] == "online" and info:
                ready = info["start_time"] + (400 * (r["window"] + 1) + 1) / (400 * info["speed"])
                done = datetime.strptime(r["computed_at"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                    tzinfo=timezone.utc).timestamp()
                if not any(lo - 3 <= ready <= hi + 20 for lo, hi in outage):
                    latencies.append(done - ready)
                    if done - ready > 5:
                        if self.source_delay(faults, info, r["window"], ready, done):
                            explained += 1
                        else:
                            unexplained += 1
        online_windows = {r["window"] for r in lines if r["mode"] == "online"}
        missing_online = [w for w in range(total_windows) if w not in online_windows]
        out = {"windows_expected": total_windows, "windows_checked_against_reference": len(ref),
               "lines": len(lines), "by_mode": modes, "mismatches": mism,
               "mismatch_examples": examples, "missing_online": len(missing_online),
               "online_over_5s": {"explained_by_injected_source_fault": explained, "unexplained": unexplained}}
        if latencies:
            latencies.sort()
            out["online_latency_s"] = {"windows": len(latencies), "median": round(statistics.median(latencies), 3),
                                       "p99": round(latencies[int(0.99 * (len(latencies) - 1))], 3),
                                       "max": round(latencies[-1], 3)}
        return out

    @staticmethod
    def source_delay(faults, info, w, ready, done) -> bool:
        """Was the data for window w late at the source (a mock stall or a sensor
        dropout the mock had not yet reported)?"""
        for ev in faults:
            if ev["kind"] == "stall" and ready - 1 <= ev["t"] + ev["seconds"] and ev["t"] <= done:
                return True
            if ev["kind"] == "dropout":
                lo, hi = 400 * max(0, w - 6), 400 * (w + 1)
                if ev["from_index"] <= hi and ev["to_index"] >= lo and abs(ev["t"] - ready) < 30:
                    return True
        return False

    def outage_spans(self):
        spans, start = [], None
        for t, w in self.timeline:
            if (w.startswith("network cut") or w.startswith("power cut") or w.startswith("pipeline killed")
                    or w.startswith("cloud service killed")):
                start = t
            elif start is not None and (w.startswith("network restored") or w.startswith("power restored")
                                        or w.startswith("pipeline restarted") or w.startswith("cloud service restarted")):
                spans.append((start, t))
                start = None
        return spans

    def switch_times(self):
        out = []
        for t, w in self.timeline:
            want = "offline" if w == "network cut" else ("online" if w == "network restored" else None)
            if want is None:
                continue
            hit = next((ts for ts, mode, _ in self.status_log if ts >= t and mode == want), None)
            out.append({"event": w, "to": want, "seconds": None if hit is None else round(hit - t, 2)})
        return out

    def cli_checks(self, sessions) -> dict:
        """Every documented command runs and prints well-formed JSON lines."""
        out = {}
        sid = sessions[-1]["session"] if sessions else ""
        for name, args in (("status", ["status"]), ("sessions", ["sessions"]), ("events", ["events", "--limit", "200"]),
                           ("results_online", ["results", "--session", sid, "--mode", "online"]),
                           ("results_offline", ["results", "--session", sid, "--mode", "offline"])):
            lines = [l for l in self.cli(*args).splitlines() if l.strip()]
            ok = all(isinstance(json.loads(l), dict) for l in lines) and (bool(lines) or name == "results_offline")
            out[name] = {"lines": len(lines), "json_ok": ok}
        st = json.loads(self.cli("status").splitlines()[-1])
        out["status_fields_ok"] = all(k in st for k in ("mode", "samples_saved", "samples_in_cloud", "backlog"))
        kinds = {}
        for l in self.cli("events", "--limit", "500").splitlines():
            k = json.loads(l)["kind"]
            kinds[k] = kinds.get(k, 0) + 1
        out["event_kinds"] = kinds
        self.kill("pipeline")
        st = json.loads(self.cli("status").splitlines()[-1])
        out["status_after_stop"] = {"daemon": st["daemon"], "mode": st["mode"]}
        out["all_ok"] = (all(v["json_ok"] for v in out.values() if isinstance(v, dict) and "json_ok" in v)
                         and out["status_fields_ok"] and st["daemon"] == "stopped")
        return out

    def catchup_times(self):
        """Seconds from each network restore until the device backlog is empty."""
        out = []
        cut_at = None
        for t, w in self.timeline:
            if w == "network cut":
                cut_at = t
            if w != "network restored":
                continue
            hit = next((ts for ts, mode, backlog in self.status_log if ts >= t and backlog == 0), None)
            data_s = None
            if cut_at is not None:
                data_s = round((t - cut_at) * self.args.speed)
            out.append({"outage_real_s": round(t - cut_at, 1) if cut_at else None,
                        "outage_data_s": data_s,
                        "seconds_to_empty_backlog": None if hit is None else round(hit - t, 1)})
        return out

    def shutdown(self) -> None:
        for p in list(self.procs.values()) + [self.cloud]:
            try:
                p.kill()
            except OSError:
                pass


def to_markdown(rep: dict) -> str:
    L = [f"# Drill `{rep['name']}` - {'PASS' if rep['pass'] else 'FAIL'}", "",
         f"Platform: `{rep['platform']}`; mode `{rep['params']['mode']}`, speed x{rep['params']['speed']}, "
         f"duration {rep['params']['duration']} s, faults `{rep['params']['faults'] or 'none'}`.", "",
         "## Timeline (seconds from start)", ""]
    L += [f"- {t:7.1f}  {w}" for t, w in rep["timeline"]]
    cli = rep.get("cli", {})
    L += ["", "## Commands", ""]
    L += [f"- `{k}`: {v['lines']} JSON lines, well-formed: {v['json_ok']}" for k, v in cli.items()
          if isinstance(v, dict) and "json_ok" in v]
    if cli:
        L += [f"- events recorded: {cli.get('event_kinds')}",
              f"- `status` after the pipeline was stopped: {cli.get('status_after_stop')}"]
    L += ["", "## Catch-up after network restore", ""]
    L += [f"- outage of {c['outage_data_s']} s of data ({c['outage_real_s']} s real): device backlog empty "
          f"**{c['seconds_to_empty_backlog']} s** after the network returned" for c in rep.get("catch_up", [])] or ["- none"]
    L += ["", "## Mode switches", ""]
    L += [f"- {s['event']} -> `{s['to']}` after **{s['seconds']} s**" for s in rep["mode_switches"]] or ["- none"]
    for s in rep["sessions"]:
        d, c, r = s["device_export"], s["cloud_export"], s["results"]
        L += ["", f"## Session `{s['session']}` (ended normally: {s['completed_by_mock']})", "",
              "| check | device | cloud |", "|---|---|---|",
              f"| samples exported | {d['samples']} | {c['samples']} |",
              f"| samples listed as lost | {d['lost_samples']} | {c['lost_samples']} |",
              f"| duplicates | {d['duplicates']} | {c['duplicates']} |",
              f"| wrong values | {d['wrong_values']} | {c['wrong_values']} |",
              f"| silent (unlisted) missing | {d['silent_missing']} | {c['silent_missing']} |",
              f"| present-and-lost conflicts | {d['conflicts']} | {c['conflicts']} |", "",
              f"Cloud copy byte-identical to device copy: **{s['cloud_copy_identical_to_device']}**", "",
              f"Results: {r['lines']} lines {r['by_mode']}, windows {r['windows_expected']}, "
              f"checked against the reference: {r['windows_checked_against_reference']}, "
              f"mismatches: **{r['mismatches']}**, windows without an online result: {r['missing_online']}"]
        if "online_latency_s" in r:
            lat = r["online_latency_s"]
            L.append(f"Online latency (sample produced by the mock -> result stored in the cloud), "
                     f"{lat['windows']} windows outside outages: median {lat['median']} s, p99 {lat['p99']} s, "
                     f"max {lat['max']} s")
            o = r["online_over_5s"]
            L.append(f"Windows over 5 s: {o['explained_by_injected_source_fault']} while the mock itself was "
                     f"stalled or a sensor was silent (data late at the source); unexplained: **{o['unexplained']}**")
        if d["examples"] or c["examples"]:
            L += ["", "Examples:"] + [f"- {e}" for e in d["examples"] + c["examples"]]
    return "\n".join(L) + "\n"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--name", default="drill")
    p.add_argument("--mode", choices=("signal", "ramp"), default="ramp")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--speed", type=float, default=4.0)
    p.add_argument("--duration", type=float, default=600)
    p.add_argument("--faults", default="")
    p.add_argument("--events", default="20:cut,45:restore,60:kill,80:power",
                   help="comma list of <seconds>:<cut|restore|kill|power|mockrestart>")
    p.add_argument("--out", default=os.path.join(ROOT, "evidence"))
    p.add_argument("--check-windows", type=int, default=0,
                   help="compare only the first N windows with the (slow) reference; 0 = all")
    args = p.parse_args()
    drill = Drill(args)
    try:
        rep = drill.run()
    finally:
        drill.shutdown()
    os.makedirs(args.out, exist_ok=True)
    rep["work_dir"] = drill.dir
    with open(os.path.join(args.out, f"{args.name}.json"), "w") as f:
        json.dump(rep, f, indent=2)
    with open(os.path.join(args.out, f"{args.name}.md"), "w") as f:
        f.write(to_markdown(rep))
    print(to_markdown(rep))
    return 0 if rep["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
