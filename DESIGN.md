# Design note: Never Lose a Heartbeat

## 1. Architecture

```
 Raspberry Pi 4 (ARM64, ≤4 cores, ≤2 GB)                          Free cloud VM
 ┌───────────────────────────────────────────────────────┐        ┌───────────────────────────────┐
 │ biosignal-mock ──TCP/NDJSON──▶ collector thread        │        │ Caddy (TLS) ─▶ cloud.server   │
 │   (unmodified)   CRC check, cursor per sensor          │        │   POST /v1/ingest (idempotent)│
 │                    │ one fsync'd transaction / 0.5 s   │ HTTPS  │   SQLite WAL, synchronous=FULL│
 │                    ▼                                   │ gzip   │        │                      │
 │              SQLite (WAL, FULL) ◀── uplink thread ─────┼───────▶│   worker: same processing.py │
 │   chunks · losses · outbox · results    live lane +    │        │   → results (mode=online)     │
 │                    ▲                    backlog lane   │        │   GET /v1/export, /v1/results │
 │   offline thread ──┘ processing.py (mode=offline)      │        └───────────────────────────────┘
 │   pipeline status | export | results   (CLI)           │
 └───────────────────────────────────────────────────────┘
```

* **One program, three threads, standard library only** (Python 3.9+). `systemd` restarts it within 1 s of a crash or kill.
* **Collector.** Every request carries `from_index` = the next index we need, so reconnects, duplicates and restarts can never skip or repeat data. Every reply is applied idempotently by `(session, sensor, index)`: data before the cursor is dropped, data at the cursor is saved, and a gap is either explained by `lost` or re-read. If it is still missing after 3 re-reads it is recorded as lost with a reason. Corrupt replies (CRC or JSON) are never used and the data is re-read. A new `session_id` closes the old session, and its unread tail is recorded as lost with `to_index: null`.
* **Storage.** SQLite in WAL mode with `synchronous=FULL`: each 0.5 s round is one transaction, fsync'd before the next read. Samples are kept as contiguous, non-overlapping chunks (≤400 samples, zlib-compressed, CRC of the stored text). **Invariant:** for every session and sensor, each index below the cursor is in exactly one chunk or one loss range. `export` re-checks this invariant and reports any hole as a loss, so a bug can't hide data.
* **Uplink.** Pending chunks sit in an `outbox` table and are removed when the cloud acknowledges them. Each upload carries the newest ~15 s first (the *live lane*), so online results keep arriving within seconds while a 2-hour backlog is still uploading, and then the oldest pending data (*backlog lane*, ~1.5 MB per request, gzip). The cloud keys chunks by `(session, sensor, first_index)` and losses by `(session, sensor, from_index)`. A resend is acknowledged without being stored again, and a chunk overlapping stored samples is rejected, so every sample is in the cloud **exactly once**.
* **Mode switching.** Decided by observed evidence, not guessed. Any failed upload (3 s socket timeout) switches to offline at once. While offline, `/health` is probed every 2 s, and the first success switches back to online. DNS lookups are capped at 2 s, because they can hang when the network is down.
* **Processing (section 3)** is one module, `hbpipe/processing.py`, imported by both the device and the cloud. A window is computed only when every input it depends on is *decided* (present, or known lost, or past the session end). A result is never computed from data that hasn't arrived yet, so no result ever needs to be revised. The 100-sample peak rule depends on history, so the code finds a *reset point* (100 samples where no peak is possible). The real previous peak must lie before it, so the peaks match a scan from sample 0 exactly, while touching only a few seconds of data. This is cross-checked against a plain line-by-line transcription of the brief (`tests/reference.py`).
* **Offline mode.** The device computes the windows the cloud has not confirmed (`mode: offline`). After reconnecting, the cloud also computes every window from the complete copy (`mode: online`). `pipeline results` shows one line per window and mode.

## 2. What is guaranteed, and what is not

| Guaranteed (given working storage) | How |
|---|---|
| Every sample received with a valid CRC is on disk exactly once, unchanged | cursor + idempotent apply + UNIQUE key; values stored as received |
| Every sample not on disk is listed with a reason | contiguity invariant; losses written in the same transaction that advances the cursor |
| Committed data survives crash, kill -9, reboot, power cut | WAL + fsync per commit; restart resumes from the stored cursor |
| No loss or duplicate from a mode switch | saving never depends on the network; cloud ingest is idempotent |
| Cloud copy complete and exactly-once after an outage | outbox + ack only after the cloud commits; backlog lane ≈ 60× real time on a normal link |
| Online and offline results use identical logic | one shared module; results only on decided data |
| Clock changes don't matter | only monotonic time is used for timing; data is ordered by index; TLS can fall back to a pinned certificate if the clock makes it look expired |

**Not guaranteed (always reported):**

* Samples the mock never delivers, because it buffers only 10 s: the mock stalling or the pipeline being down for more than about 10 s, a mock restart, or sensor dropouts. They are recorded as lost with the mock's reason or ours.
* **Storage that lies about fsync.** Some SD cards acknowledge writes before they are durable, which breaks the power-cut bound. Use a quality card or SSD with ext4.
* **Disk full.** A 64 MB reserve file (about 2 hours of data at ~30 MB/h) is released first. After that, up to 120 s of samples are held in memory, and anything older becomes a `disk full` loss. If power is also cut while the disk is full, the cursor on disk still marks where saving stopped, so after reboot the rest is reported as an open-ended loss, never silently.
* `computed_at` of offline results uses the device clock. The cloud's own timestamps use the cloud clock.

## 3. Maximum loss on a power cut, and why

**At most 1 second per sensor (≤400 samples), and typically about 0.5 s.** The collector reads both sensors and commits every 0.5 s. A commit returns only after fsync, so after power returns the database holds every committed round. A power cut can lose only the round being read or committed: the data received since the last commit plus anything still in the mock's memory (the mock dies too). That is one poll interval (0.5 s) plus read and fsync time, about 0.1 s on a Pi 4 SD card. After reboot the pipeline sees a new mock session and records the old session's unsaved tail as lost (`to_index: null`, reason: device rebooted or lost power), so the loss is reported, never silent. Committing more often would lower the bound but increase SD-card writes. The bound is set with `poll_interval` in the config.
