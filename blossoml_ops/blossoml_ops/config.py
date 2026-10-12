"""Configuration (``/etc/blossoml-ops/config.yaml``).

Values that Claude must never choose (DM recipient, relays) are constants in
``blossoml_ops.notify`` and are not configurable here.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

DEFAULT_CONFIG_PATH = "/etc/blossoml-ops/config.yaml"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]+\.service$")
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    blossoml_dir: Path
    blossoml_unit: str
    blossoml_url: str
    tls_verify: bool
    test_blob_sha256: str | None
    state_dir: Path
    trace_dir: Path
    work_dir: Path
    anthropic_api_key_file: Path
    nostr_nsec_file: Path
    proxy: str | None
    model: str | None
    max_turns: int
    max_budget_usd: float
    run_timeout_s: int
    mask_pubkeys: bool
    phase2_enabled: bool
    trace_retention_days: int
    trace_max_total_mb: int
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def data_dir(self) -> Path:
        return self.blossoml_dir / "data"

    @property
    def db_path(self) -> Path:
        return self.blossoml_dir / "db" / "blossom.db"

    @property
    def undelivered_path(self) -> Path:
        return self.state_dir / "undelivered.jsonl"


_KNOWN_TOP = {
    "blossoml", "test_blob_sha256", "paths", "secrets", "proxy", "model", "max_turns",
    "max_budget_usd", "run_timeout_s", "mask_pubkeys", "phase2", "trace",
}


def _get(m: Mapping, key: str, typ, default=None, required=False):
    if key not in m or m[key] is None:
        if required:
            raise ConfigError(f"missing required setting {key!r}")
        return default
    v = m[key]
    if typ is float and isinstance(v, int) and not isinstance(v, bool):
        v = float(v)
    if not isinstance(v, typ) or (typ is int and isinstance(v, bool)):
        raise ConfigError(f"setting {key!r} must be {typ.__name__}")
    return v


def parse_config(raw: Any) -> Config:
    if not isinstance(raw, Mapping):
        raise ConfigError("config must be a mapping")
    unknown = set(raw) - _KNOWN_TOP
    if unknown:
        raise ConfigError(f"unknown settings: {sorted(unknown)}")
    b = _get(raw, "blossoml", dict, {})
    paths = _get(raw, "paths", dict, {})
    secrets = _get(raw, "secrets", dict, {})
    phase2 = _get(raw, "phase2", dict, {})
    trace = _get(raw, "trace", dict, {})

    url = _get(b, "url", str, "http://localhost:8082")
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or u.hostname not in LOCAL_HOSTS or u.path not in ("", "/") or u.query or u.username:
        raise ConfigError("blossoml.url must be http(s)://localhost:<port> (the probe never leaves the host)")
    unit = _get(b, "unit", str, "blossoml.service")
    if not UNIT_RE.match(unit):
        raise ConfigError("blossoml.unit must look like name.service")

    sha = _get(raw, "test_blob_sha256", str)
    if sha is not None and not SHA256_RE.match(sha):
        raise ConfigError("test_blob_sha256 must be 64 lowercase hex characters")

    proxy = _get(raw, "proxy", str)
    if proxy is not None:
        pu = urllib.parse.urlsplit(proxy)
        if pu.scheme != "http" or not pu.hostname:
            raise ConfigError("proxy must be an http:// URL")

    max_turns = _get(raw, "max_turns", int, 30)
    if not 1 <= max_turns <= 200:
        raise ConfigError("max_turns must be in 1..200")
    budget = _get(raw, "max_budget_usd", float, 1.0)
    if not 0 < budget <= 50:
        raise ConfigError("max_budget_usd must be in (0, 50]")

    state_dir = Path(_get(paths, "state_dir", str, "/var/lib/blossoml-ops"))
    return Config(
        blossoml_dir=Path(_get(b, "dir", str, "/home/ubuntu/blossoML")),
        blossoml_unit=unit,
        blossoml_url=url.rstrip("/"),
        tls_verify=_get(b, "tls_verify", bool, True),
        test_blob_sha256=sha,
        state_dir=state_dir,
        trace_dir=Path(_get(paths, "trace_dir", str, str(state_dir / "traces"))),
        work_dir=Path(_get(paths, "work_dir", str, str(state_dir / "work"))),
        anthropic_api_key_file=Path(_get(secrets, "anthropic_api_key_file", str, "/etc/blossoml-ops/anthropic_api_key")),
        nostr_nsec_file=Path(_get(secrets, "nostr_bot_nsec_file", str, "/etc/blossoml-ops/nostr_bot_nsec")),
        proxy=proxy,
        model=_get(raw, "model", str),
        max_turns=max_turns,
        max_budget_usd=budget,
        run_timeout_s=_get(raw, "run_timeout_s", int, 900),
        mask_pubkeys=_get(raw, "mask_pubkeys", bool, False),
        phase2_enabled=_get(phase2, "enabled", bool, False),
        trace_retention_days=_get(trace, "retention_days", int, 30),
        trace_max_total_mb=_get(trace, "max_total_mb", int, 50),
    )


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    return parse_config(raw or {})


def read_secret(path: Path) -> str:
    """Read a secret file. Secrets never enter prompts, tool output, or traces."""
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise ConfigError(f"cannot read secret {path}: {e.strerror}") from None
    if not value:
        raise ConfigError(f"secret {path} is empty")
    return value
