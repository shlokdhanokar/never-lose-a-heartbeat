"""Configuration: an INI file ([pipeline] section) plus HBPIPE_* environment overrides."""
from __future__ import annotations

import configparser
import os
import socket
from dataclasses import dataclass, fields

DEFAULT_PATHS = ("/etc/hbpipe/pipeline.conf", "pipeline.conf")


@dataclass
class Config:
    mock_host: str = "127.0.0.1"
    mock_port: int = 7100
    mock_timeout: float = 2.0
    poll_interval: float = 0.5
    data_dir: str = "/var/lib/hbpipe"
    cloud_url: str = ""
    cloud_token: str = ""
    cloud_timeout: float = 3.0
    probe_interval: float = 2.0
    live_seconds: float = 15.0
    upload_max_bytes: int = 1_500_000
    device_id: str = socket.gethostname()
    memory_buffer_seconds: int = 120
    ballast_mb: int = 64
    tls_pins: str = ""          # comma-separated SHA-256 certificate fingerprints
    ca_file: str = ""
    log_level: str = "INFO"

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "pipeline.db")

    @property
    def status_path(self) -> str:
        return os.path.join(self.data_dir, "status.json")

    @property
    def ballast_path(self) -> str:
        return os.path.join(self.data_dir, "reserve.bin")

    @property
    def lock_path(self) -> str:
        return os.path.join(self.data_dir, "pipeline.lock")

    @property
    def pins(self):
        return [p.strip().lower().replace(":", "") for p in self.tls_pins.split(",") if p.strip()]


def load_config(path: str = None) -> Config:
    cfg = Config()
    candidates = [path] if path else [os.environ.get("HBPIPE_CONFIG")] + list(DEFAULT_PATHS)
    parser = configparser.ConfigParser()
    for p in candidates:
        if p and os.path.exists(p):
            parser.read(p, encoding="utf-8")
            break
    else:
        if path:
            raise SystemExit(f"config file not found: {path}")
    section = parser["pipeline"] if parser.has_section("pipeline") else {}
    for f in fields(Config):
        raw = os.environ.get("HBPIPE_" + f.name.upper(), section.get(f.name) if section else None)
        if raw is None:
            continue
        setattr(cfg, f.name, type(getattr(cfg, f.name))(raw))
    return cfg
