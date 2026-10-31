# Never Lose a Heartbeat

An offline-first data pipeline for a two-sensor bedside device on a Raspberry Pi-class board, with a free cloud service. It saves every sample safely on the device and streams to the cloud when the network is up. When the network is down, the device processes the data itself and catches the cloud up afterwards. Every sample ends up in both copies exactly once, and every sample that couldn't be saved is listed with a reason.

* **Design note (2 pages):** [DESIGN.md](DESIGN.md)
* **Failure tests and evidence:** [TESTING.md](TESTING.md) and [evidence/](evidence/)
* **Demo video plan:** [DEMO_SCRIPT.md](DEMO_SCRIPT.md)

## Layout

```
pipeline                 the one device program (CLI entry point)
hbpipe/                  device side, standard library only
  collector.py           mock -> disk: CRC check, exactly-once, loss accounting
  store.py               SQLite WAL + fsync per commit; chunks, losses, outbox, results
  uplink.py              disk -> cloud; online/offline switching; live + backlog lanes
  offline.py             offline processing on the device
  processing.py          section 3 logic (shared with the cloud)
  export.py, cli.py      required commands
cloud/                   cloud service (stdlib HTTP + SQLite), same processing.py
deploy/                  systemd units, Pi-class resource slice, install scripts, Caddy TLS
devtools/                development mock, export checker, drills, resource meter
tests/                   unit tests and a plain reference implementation of section 3
```

## Install on a fresh board (Raspberry Pi OS / Debian / Ubuntu, ARM64)

Requirements: `python3` 3.9 or newer (preinstalled on Raspberry Pi OS). No pip packages.

```bash
git clone <this repo> && cd never-lose-a-heartbeat
sudo bash deploy/install_device.sh              # installs /opt/hbpipe, systemd unit, /usr/local/bin/pipeline
sudo nano /etc/hbpipe/pipeline.conf             # set cloud_url and cloud_token (from the cloud install)
sudo systemctl restart pipeline
pipeline status
```

The official mock runs unmodified on the same board (port 7100), for example:

```bash
biosignal-mock --mode signal --seed 7 --duration 3600 --port 7100
```

To start it at boot, copy it to `/opt/biosignal-mock/` before running the installer, which then also installs `deploy/biosignal-mock.service`.

**Not a Raspberry Pi?** Any ARM64 Linux VM works, for example a free-tier ARM64 cloud instance or a QEMU Raspberry Pi OS image. Both services run in `raspi-limits.slice`, which caps them at 4 CPU cores and 2 GB of memory (`AllowedCPUs=0-3`, `MemoryMax=2G`).

## Install the cloud side (free tier)

Use any free Linux VM, for example Oracle Cloud *Always Free* or an Azure *free services* B1s VM. Open ports 80/443 in the cloud firewall, then:

```bash
sudo bash deploy/install_cloud.sh <dns-name>    # e.g. 203-0-113-7.sslip.io, or <label>.<region>.cloudapp.azure.com
```

The script prints the HTTPS URL and a random API token for the device config. Caddy provides a free Let's Encrypt certificate. A container image is also available (`deploy/Dockerfile.cloud`) for free hosts with a persistent volume.

Without Caddy, the service can also serve HTTPS itself: `python3 -m cloud.server --tls-cert fullchain.pem --tls-key privkey.pem --host 0.0.0.0 --port 443`.

Optional: `pipeline pin-cert` prints the certificate fingerprint. Add it as `tls_pins` in the device config so uploads keep working if the board's clock is changed.

## Commands (case brief section 4)

```bash
pipeline status                                         # one JSON line: mode, samples saved, in cloud, backlog, ...
pipeline export --session ID --from device              # JSON lines: samples, then losses
pipeline export --session ID --from cloud
pipeline results --session ID                           # one JSON line per window and mode
pipeline sessions                                       # session ids and counters
pipeline events                                         # mode switches, session changes, incidents
```

Example status line:

```json
{"mode": "online", "samples_saved": 384000, "samples_in_cloud": 384000, "backlog": 0, "backlog_seconds": 0.0, "samples_lost": 0, "open_ended_losses": 0, "session": "342f4822", "sessions": 2, "daemon": "running", ...}
```

Export lines use the brief's format:

```json
{"sensor": 1, "index": 120000, "values": [720, 1200, 1560]}
{"sensor": 2, "from_index": 4000, "to_index": 4399, "reason": "buffer_overflow"}
```

Result lines contain `missing_1`, `missing_2`, `complete`, then per channel `mean_1`, `min_1`, `max_1` (sensor 1, 3 values) and `mean_2`, `min_2`, `max_2` (sensor 2, 2 values), then `magnitude`, `heart_rate` and `mode`.

## Try it on any computer (no board needed)

```bash
python -m unittest discover -s tests -t .                                   # unit tests
python devtools/e2e_local.py --name demo --mode signal --speed 4 --duration 600 \
    --faults corrupt=0.02,dup=0.02,stall=0.003,dropout=0.002 \
    --events 20:cut,50:restore,65:kill,80:mockrestart,125:power
```

The drill starts a development mock, the cloud and the pipeline, injects the faults, then checks both exports against the ground truth and every result against the reference implementation. It writes `evidence/demo.md`.

`devtools/mock_sim.py` is a **development stand-in** built from the protocol in section 6, because the official mock's download link was blank in the brief. Judging uses the official `biosignal-mock` and `check_ground_truth`. Nothing in `hbpipe/` depends on the stand-in.

## Interpretation notes

Where the brief left a choice open, these are the choices made (each is easy to change):

* `heart_rate` is null if any **sensor 1** sample (the heart-rate source) is missing in the 5 seconds.
* Per-channel outputs are lists in channel order: `mean_1 = [ch1, ch2, ch3]`. They are `null` when the window has no samples.
* Rounding uses Python's `round(x, 1)`, and `-0.0` is written as `0.0`.
* A session ends at its last sample. No windows are produced after it. The last window may be partial, and its missing samples are counted.
* Windows during an outage get an `offline` result from the device and, after catch-up, an `online` result from the cloud.

## Disclosure of AI tools

This solution was developed with an AI coding assistant (Anthropic's Claude) for code drafting, tests and documentation, as the brief allows. Every figure in this repository comes from running the code; nothing is estimated except where stated.
