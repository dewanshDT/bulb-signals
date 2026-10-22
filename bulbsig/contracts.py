"""Shared types and interfaces. Every module builds against these; change them
only through the integrator so parallel work stays compatible."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Notification:
    """One notification from any source (macOS sensor, calendar, webhook)."""
    source: str
    app: str
    title: str = ""
    subtitle: str = ""
    body: str = ""
    time: float = 0.0
    id: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Notification":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


@dataclass(frozen=True)
class LightState:
    """A bulb's visible state. Exactly one of temp / rgb / scene applies when on."""
    on: bool
    dimming: int | None = None
    temp: int | None = None
    rgb: tuple[int, int, int] | None = None
    scene: int | None = None
    speed: int | None = None

    def to_params(self) -> dict[str, Any]:
        """setPilot params for this state."""
        if not self.on:
            return {"state": False}
        p: dict[str, Any] = {"state": True}
        if self.dimming is not None:
            p["dimming"] = max(10, min(100, int(self.dimming)))
        if self.scene:
            p["sceneId"] = self.scene
            if self.speed is not None:
                p["speed"] = self.speed
        elif self.rgb is not None:
            p["r"], p["g"], p["b"] = self.rgb
        elif self.temp is not None:
            p["temp"] = max(2200, min(6500, int(self.temp)))
        return p

    @classmethod
    def from_pilot(cls, r: dict[str, Any]) -> "LightState":
        """From a getPilot `result` object."""
        scene = r.get("sceneId") or None
        rgb = None
        if not scene and "r" in r:
            rgb = (int(r["r"]), int(r["g"]), int(r["b"]))
        return cls(
            on=bool(r.get("state")),
            dimming=r.get("dimming"),
            temp=None if (scene or rgb) else r.get("temp"),
            rgb=rgb,
            scene=scene,
            speed=r.get("speed") if scene else None,
        )


class Bulb(Protocol):
    """One physical bulb, addressed by role ("desk", "room")."""
    role: str
    mac: str

    async def get_state(self) -> LightState:
        """Read current state (getPilot). Raises BulbUnreachable."""
        ...

    def send(self, state: LightState) -> None:
        """Fire-and-forget setPilot. Never blocks, never raises on network loss."""
        ...

    async def set(self, state: LightState, *, attempts: int = 3) -> bool:
        """setPilot, then read back until it matches or attempts run out."""
        ...


class BulbUnreachable(Exception):
    pass


class EngineAPI(Protocol):
    """What the daemon, calendar and rules may call on the engine."""

    async def run(self) -> None: ...

    def trigger(self, signal: str, *, key: str | None = None,
                priority: int | None = None,
                origin: dict[str, Any] | None = None) -> str:
        """Queue a named signal. `key` merges repeats; returns a job id."""
        ...

    def ack(self, job_id: str | None = None) -> bool:
        """Stop the playing repeating signal (or a specific job)."""
        ...

    def set_mode(self, mode: str, active: bool) -> None:
        """Persistent baseline overlay, e.g. "on_air"."""
        ...

    def mute(self, seconds: float | None) -> None:
        """Mute all signals for N seconds; None or 0 unmutes."""
        ...

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe state for GET /v1/state (see ENGINE_SNAPSHOT_KEYS)."""
        ...

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Queue of engine events (see EVENT_TYPES)."""
        ...

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None: ...


ENGINE_SNAPSHOT_KEYS = (
    "playing",    # {"job", "signal", "started", "repeating"} | None
    "queue",      # [{"job", "signal", "priority", "key"}]
    "modes",      # ["on_air", ...]
    "muted_until",  # unix seconds | None
    "quiet",      # bool, quiet hours active now
    "bulbs",      # {role: {"mac", "ip", "reachable", "state": LightState-as-dict}}
)

EVENT_TYPES = (
    "notification",   # {"type", "notification": Notification-json, "rule", "signal", "job"}
    "queued",         # {"type", "job", "signal", "priority", "key"}
    "merged",         # {"type", "job", "signal", "key", "count"}
    "signal_start",   # {"type", "job", "signal"}
    "signal_end",     # {"type", "job", "signal", "reason": "done"|"ack"|"preempted"|"muted"|"timeout"}
    "mode",           # {"type", "mode", "active"}
    "mute",           # {"type", "until"}
    "bulb_error",     # {"type", "role", "error"}
    "meeting",        # {"type", "phase": "t10"|"t5"|"t2"|"t1"|"t0"|"end"|"overrun", "title", "start", "end"}
)

PRIORITY = {
    "meeting_t0": 100,
    "ringing": 90,
    "vip": 80,
    "incident": 70,
    "mention": 60,
    "dm": 50,
    "default": 30,
    "ambient": 10,
}
