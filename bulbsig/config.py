"""Config loading. One TOML file, default ~/.config/bulbsig/config.toml,
overridable with $BULBSIG_CONFIG. Every section is optional."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path.home() / ".config" / "bulbsig" / "config.toml"
PKG_DIR = Path(__file__).parent


@dataclass
class BulbCfg:
    role: str
    mac: str
    ip: str | None = None


@dataclass
class Config:
    path: Path
    listen: str = "127.0.0.1:8766"
    url: str = "http://127.0.0.1:8766"
    token: str = ""
    bulbs: list[BulbCfg] = field(default_factory=list)
    subnets: list[str] = field(default_factory=list)
    quiet_start: str = "23:00"
    quiet_end: str = "08:00"
    quiet_max_bri: int = 10
    patterns_file: Path = PKG_DIR / "patterns_default.toml"
    rules_file: Path = PKG_DIR / "rules_default.toml"
    ics_urls: list[str] = field(default_factory=list)
    calendar_poll_s: int = 300
    raw: dict[str, Any] = field(default_factory=dict)


def _path(p: str | None, base: Path, default: Path) -> Path:
    if not p:
        return default
    q = Path(p).expanduser()
    return q if q.is_absolute() else base / q


def load(path: str | os.PathLike | None = None) -> Config:
    p = Path(path or os.environ.get("BULBSIG_CONFIG") or DEFAULT_PATH).expanduser()
    raw: dict[str, Any] = {}
    if p.exists():
        raw = tomllib.loads(p.read_text())
    d = raw.get("daemon", {})
    q = raw.get("quiet", {})
    cal = raw.get("calendar", {})
    bulbs = [
        BulbCfg(role=role, mac=str(b["mac"]).replace(":", "").lower(), ip=b.get("ip"))
        for role, b in raw.get("bulbs", {}).items()
    ]
    cfg = Config(
        path=p,
        listen=d.get("listen", "127.0.0.1:8766"),
        url=d.get("url") or "http://" + d.get("listen", "127.0.0.1:8766"),
        token=os.environ.get("BULBSIG_TOKEN") or d.get("token", ""),
        bulbs=bulbs,
        subnets=raw.get("network", {}).get("subnets", []),
        quiet_start=q.get("start", "23:00"),
        quiet_end=q.get("end", "08:00"),
        quiet_max_bri=int(q.get("max_bri", 10)),
        patterns_file=_path(raw.get("patterns_file"), p.parent, PKG_DIR / "patterns_default.toml"),
        rules_file=_path(raw.get("rules_file"), p.parent, PKG_DIR / "rules_default.toml"),
        ics_urls=list(cal.get("ics", [])),
        calendar_poll_s=int(cal.get("poll_s", 300)),
        raw=raw,
    )
    return cfg
