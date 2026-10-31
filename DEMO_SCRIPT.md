# Demo video plan (at most 5 minutes)

Record the board's terminal (SSH) and a second terminal. Keep `watch -n 0.5 pipeline status` visible on one side the whole time.

| Time | Show | Say |
|---|---|---|
| 0:00–0:30 | `uname -m` (aarch64), `nproc`, `free -h`, `systemctl status pipeline biosignal-mock` | "ARM64 board, 4 cores and 2 GB, mock and pipeline running under systemd." |
| 0:30–1:00 | `pipeline status` showing `online`, backlog near 0. Run `pipeline results --session $S \| tail -3`, run it again 2 s later | "Online: each second's result reaches the cloud in under a second." |
| 1:00–2:00 | `sudo bash devtools/fault_drill.sh netcut 45` | "Network cut. The status flips to `offline` within about 5 s. Samples keep being saved and the device computes results itself (`mode: offline`). When the network returns it flips back within about 4 s and the backlog drains." |
| 2:00–2:45 | `sudo bash devtools/fault_drill.sh kill` | "kill -9: systemd restarts the pipeline in about 1 s. It resumes from the stored cursor and re-reads the mock's 10 s buffer, so nothing is lost." |
| 2:45–4:00 | `sudo bash devtools/fault_drill.sh mark "pulling power"`, then **pull the power cable** and plug it back in. After boot: `sudo bash devtools/fault_drill.sh after-reboot` | "Power cut. Everything committed is still there. The unsaved tail of the old session (at most 1 s) is listed as lost with `to_index: null` and a reason, not hidden." |
| 4:00–4:45 | `sudo bash devtools/fault_drill.sh verify $S`, and the official `check_ground_truth` on both exports | "Device copy and cloud copy are identical, every sample exactly once, every gap explained." |
| 4:45–5:00 | `cat evidence/resources.json` | "Resource use over one hour online." |

Before recording: start a fresh mock session (`--mode signal --seed 7 --duration 3600`), let it run for 2 minutes, and set `S=$(pipeline status | python3 -c 'import json,sys;print(json.load(sys.stdin)["session"])')`.
