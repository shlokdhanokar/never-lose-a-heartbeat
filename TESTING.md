# Failure tests and results

There are two layers of evidence:

1. **Automated tests and drills that anyone can rerun on any machine** (results below, files in `evidence/`). They use the development mock (`devtools/mock_sim.py`), which follows section 6 of the brief and injects its faults. They were run on an x86-64 Windows laptop, and the drills speed up time (×4 or ×8) so that long sessions fit in minutes.
2. **Runs on the ARM64 board with the official mock** (`devtools/fault_drill.sh`, `devtools/measure_resources.py`). Fill in section 3 from those runs before submitting.

## 1. Unit tests

```bash
python -m unittest discover -s tests -t .
```

| File | What it proves |
|---|---|
| `tests/test_processing.py` | Section 3 results equal a plain transcription of the brief (`tests/reference.py`) on synthetic ECG and ramp data. This holds with random losses (1 to 1,500 samples), with windows computed one at a time, in batches, or in random pieces, and with older data still unknown. Heart rate follows the 60/90/72 bpm schedule. |
| `tests/test_protocol.py` | CRC-32 is checked over the data text exactly as sent. Corrupt, truncated or misshapen replies are rejected. A reason string can't trick the data extraction. |
| `tests/test_collector.py` | Duplicates and overlaps are saved once. Reported losses are recorded. An unexplained gap is re-read, then recorded. A mock restart closes the session with a `to_index: null` loss. Late replies from a closed session are ignored. A restart resumes from disk. Disk full turns into loss records, never silence. A socket run against a mock with 5% corrupt and 5% duplicate replies gives an exact export. |
| `tests/test_collector.py` (disk full) | A real SQLite "database or disk is full" error while the mock keeps producing: the reserve file is released, held samples become `disk full` loss records, and saving resumes when space returns. Nothing goes missing silently. |
| `tests/test_tls.py` | HTTPS with a verified certificate. An expired certificate is rejected, but accepted when its fingerprint is pinned (the wrong-clock case). A wrong pin is rejected. |
| `tests/test_clock.py` | The wall clock is used only for display timestamps. The collector runs correctly while `time.time()` jumps by hours and years. |
| `tests/test_cloud.py` | A resend is acknowledged but stored once. Overlapping or bad-CRC chunks are rejected. Online processing and export work. In-memory state rebuilds after a cloud restart. |

## 2. Drills on one machine (`devtools/e2e_local.py`)

Each drill starts the mock, the cloud, a black-hole "network" proxy and the pipeline, and injects faults on a timeline. Afterwards it:

* exports **both** copies of every session and checks them against the ground truth (every sample once, unchanged; every gap listed);
* checks **every** result line against the reference implementation;
* measures mode-switch time with `pipeline status` and online latency from "sample produced by the mock" to "result stored in the cloud".

| Drill (report) | Scenario | Samples checked (device + cloud) | Silent loss / duplicates / wrong values | Listed as lost (with reason) | Cloud copy = device copy | Results vs reference | Offline after cut | Online after restore | Catch-up | Online latency (median) |
|---|---|---|---|---|---|---|---|---|---|---|
| [dev-ramp-basic](evidence/dev-ramp-basic.md) | ramp, ×4, 8 min of data: network cut 25 s, kill -9, power cut | 619,108 ×2 | **0 / 0 / 0** | 0 | byte-identical | 774 windows, **0 mismatches** | 4.0 s | 3.1 s | 3.1 s | 0.41 s |
| [dev-signal-faults](evidence/dev-signal-faults.md) | ECG, ×4, mock faults (2% corrupt, 2% duplicate, stalls, dropouts): 2 network cuts, kill -9, mock restart, power cut | 783,680 ×2 | **0 / 0 / 0** | 67,267 (all caused by injected mock faults) | byte-identical | 1,065 windows, **0 mismatches** | ≤ 4.8 s | ≤ 3.5 s | n/a | 0.48 s |
| [dev-full-drill](evidence/dev-full-drill.md) | ECG, ×4, mock faults, **every fault type in one run**: 2 network cuts, kill -9, pipeline down longer than the mock buffer, **cloud service killed and restarted**, mock restart, power cut. All CLI commands checked. | 941,031 ×2 | **0 / 0 / 0** | 57,757 (mock faults, outage longer than the buffer, session ends) | byte-identical | 1,249 windows, **0 mismatches** | ≤ 4.0 s | ≤ 3.2 s | 5.6 s / 1.9 s | 0.45 s |
| [dev-2h-outage](evidence/dev-2h-outage.md) | ECG, ×8, 2.5 h of data: one **2-hour** network outage | 7,200,000 ×2 | **0 / 0 / 0** | 0 | byte-identical | first 900 windows checked, **0 mismatches**; all 9,000 windows have an online result | 4.3 s | 2.0 s | **56.9 s** for 2 h of data (limit 15 min) | 0.40 s |

All 36 unit tests pass (`python -m unittest discover -s tests -t .`, about 70 s). Storage on the device is about 30 MB per hour of data (76 MB for 2.5 h).

How the faults are made: **network cut**: the proxy silently drops all bytes, like a pulled cable (nothing is refused). **kill**: `kill -9` (TerminateProcess), restarted after 1 s as systemd does. **power cut**: mock and pipeline killed at the same instant, then both restarted, giving a new mock session. On the board, a real hard reset (`sysrq b`) or pulling the plug replaces this. **mock faults**: corrupt CRC, duplicate replies, stalls of up to 6 s, and sensor-2 dropouts.

**Power-cut loss measured in the drills:** the mock had produced 6–101 more samples per sensor (0.01–0.06 s) than were on disk at the instant of the cut. That is well inside the 1 s bound in DESIGN.md. Each of those tails is listed as lost (`to_index: null`).

**Online windows slower than 5 s** occurred only when the mock itself stalled or sensor 2 went silent, so the data was late at the source. The drill matches each one to a logged mock fault, and the count of unexplained ones is 0.

## 3. On the ARM64 board (fill in from your run)

Environment used: ______ (for example Raspberry Pi 4 Model B 4 GB, Raspberry Pi OS Bookworm 64-bit; or an ARM64 VM limited to 4 cores and 2 GB by `raspi-limits.slice`)
Cloud: ______ (provider, region, free tier)

| Test | Command | Result |
|---|---|---|
| Network cut 60 s | `sudo bash devtools/fault_drill.sh netcut 60` | offline after __ s, online after __ s, 0 lost |
| Process kill | `sudo bash devtools/fault_drill.sh kill` | restarted in __ s, 0 lost |
| Power cut (plug pulled) | `... mark`, pull power, `... after-reboot` | unsaved tail __ samples (≤ 400) |
| Clean reboot | `sudo bash devtools/fault_drill.sh reboot` | __ |
| Disk full 30 s | `sudo bash devtools/fault_drill.sh diskfill 30` | reserve released, __ lost (listed) |
| Clock jump +2 h | `sudo bash devtools/fault_drill.sh clock "+2 hours"` | no effect on data, mode stays online |
| 2 h outage catch-up | `netcut 7200` | cloud complete after __ min (limit 15) |
| Ground truth | `sudo bash devtools/fault_drill.sh verify $S` + official `check_ground_truth` | device: __, cloud: __ |

**Resource figures, 1 hour online** (`sudo python3 devtools/measure_resources.py --minutes 60`):

| Average CPU | Peak memory | Disk written | Network sent |
|---|---|---|---|
| __ % of one core | __ MB | __ MB | __ MB |
