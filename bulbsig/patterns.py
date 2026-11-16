"""Signal patterns: TOML loading, validation and the `bulbsig patterns` command."""
from __future__ import annotations

import argparse
import math
import sys
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from .contracts import PRIORITY, SCENES, LightState

ROLES = ("desk", "room")
DEFAULT_FILE = Path(__file__).parent / "patterns_default.toml"
REQUIRED_SIGNALS = (
    "dm", "mention", "vip", "backlog", "ringing",
    "meeting_t10", "meeting_t5", "meeting_t2", "meeting_t1", "meeting_t0", "meeting_overrun",
    "incident", "success", "heartbeat_lost", "test",
)
REQUIRED_MODES = ("on_air",)
MIN_STEP_MS = 300
DEFAULT_MAX_S = 30.0
QUIET_POLICIES = ("dim", "skip", "once")
STEP_KEYS = {"bulbs", "color", "temp", "off", "base", "bri", "ms", "ramp"}
PRESET_KEYS = {"description", "color", "temp", "scene", "speed", "bri", "swatch"}
PATTERN_KEYS = {"description", "priority", "swatch", "repeat", "max_s", "quiet", "allow_fast", "only_lit", "steps"}

Repeat = int | Literal["until_ack"]


class PatternError(ValueError):
    pass


@dataclass(frozen=True)
class Look:
    """What a step (or a mode) shows; unset fields keep the bulb's current value."""
    rgb: tuple[int, int, int] | None = None
    temp: int | None = None
    off: bool = False
    bri: int | None = None

    @property
    def empty(self) -> bool:
        return not self.off and self.rgb is None and self.temp is None and self.bri is None

    def apply(self, cur: LightState | None) -> LightState:
        if self.off:
            return LightState(on=False)
        cur = cur if cur is not None and cur.on else None
        dim = self.bri if self.bri is not None else (cur.dimming if cur and cur.dimming else 100)
        if self.rgb is not None:
            return LightState(on=True, dimming=dim, rgb=self.rgb)
        if self.temp is not None:
            return LightState(on=True, dimming=dim, temp=self.temp)
        if cur is None:
            return LightState(on=True, dimming=dim, temp=2700)
        return replace(cur, dimming=dim)


@dataclass(frozen=True)
class Step:
    bulbs: tuple[str, ...] | None  # None = every bulb
    look: Look = Look()
    base: bool = False
    ms: int = MIN_STEP_MS
    ramp: int = 0

    @property
    def wait(self) -> bool:
        return not self.base and self.look.empty

    def hits(self, role: str) -> bool:
        return self.bulbs is None or role in self.bulbs


@dataclass(frozen=True)
class Pattern:
    name: str
    description: str
    priority: int
    swatch: str
    repeat: Repeat
    steps: tuple[Step, ...]
    max_s: float | None = None
    quiet: str = "dim"
    allow_fast: bool = False
    only_lit: bool = False

    @property
    def repeating(self) -> bool:
        return self.repeat == "until_ack" or self.repeat > 1

    @property
    def max_duration_s(self) -> float | None:
        return self.max_s if self.max_s is not None else (DEFAULT_MAX_S if self.repeat == "until_ack" else None)

    def touches(self, role: str) -> bool:
        return any(s.hits(role) for s in self.steps if not s.wait)

    def step_ms(self, ms: float, floor: int = MIN_STEP_MS) -> float:
        if ms <= 0 or self.allow_fast:
            return max(0.0, ms)
        return max(ms, floor)

    def pass_ms(self, floor: int = MIN_STEP_MS) -> float:
        total = 0.0
        for s in self.steps:
            total += self.step_ms(s.ms / s.ramp, floor) * s.ramp if s.ramp > 1 else self.step_ms(s.ms, floor)
        return total


@dataclass(frozen=True)
class Mode:
    name: str
    description: str
    looks: dict[str, Look] = field(default_factory=dict)


@dataclass(frozen=True)
class Preset:
    """A resting colour the user can pick: exactly one of rgb / temp / scene."""
    name: str
    description: str
    swatch: str
    rgb: tuple[int, int, int] | None = None
    temp: int | None = None
    scene: int | None = None
    speed: int | None = None
    bri: int | None = None

    def change(self) -> dict[str, Any]:
        """Keyword arguments for Engine.set_light."""
        d: dict[str, Any] = {"rgb": self.rgb, "temp": self.temp, "scene": self.scene,
                             "speed": self.speed, "bri": self.bri}
        return {k: v for k, v in d.items() if v is not None}

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "swatch": self.swatch,
                "rgb": list(self.rgb) if self.rgb else None, "temp": self.temp, "scene": self.scene,
                "speed": self.speed, "bri": self.bri}


@dataclass
class PatternSet:
    patterns: dict[str, Pattern]
    modes: dict[str, Mode]
    source: str = "<string>"
    notes: list[str] = field(default_factory=list)  # load-time warnings (unknown keys)
    presets: dict[str, Preset] = field(default_factory=dict)

    def names(self) -> list[str]:
        return list(self.patterns)

    def get(self, name: str) -> Pattern:
        try:
            return self.patterns[name]
        except KeyError:
            raise KeyError(f"unknown signal {name!r}") from None

    def validate(self, min_step_ms: int = MIN_STEP_MS) -> list[str]:
        out = list(self.notes)
        for n in REQUIRED_SIGNALS:
            if n not in self.patterns:
                out.append(f"warning: required signal {n!r} is missing")
        for n in REQUIRED_MODES:
            if n not in self.modes:
                out.append(f"warning: required mode {n!r} is missing")
        for p in self.patterns.values():
            out += _validate_pattern(p, min_step_ms)
        for pr in self.presets.values():
            if pr.bri is not None and not 10 <= pr.bri <= 100:
                out.append(f"error: preset {pr.name}: bri {pr.bri} outside 10..100")
            if pr.temp is not None and not 2200 <= pr.temp <= 6500:
                out.append(f"warning: preset {pr.name}: temp {pr.temp} K outside the bulb range 2200..6500")
        for m in self.modes.values():
            for role, look in m.looks.items():
                if role not in ROLES:
                    out.append(f"warning: mode {m.name}: unknown bulb role {role!r}")
                if look.bri is not None and not 10 <= look.bri <= 100:
                    out.append(f"error: mode {m.name}.{role}: bri {look.bri} outside 10..100")
        return out


def _validate_pattern(p: Pattern, floor: int) -> list[str]:
    out: list[str] = []
    w = lambda level, msg: out.append(f"{level}: {p.name}: {msg}")  # noqa: E731
    if not p.steps:
        w("error", "no steps")
    if p.quiet not in QUIET_POLICIES:
        w("error", f"quiet must be one of {QUIET_POLICIES}, got {p.quiet!r}")
    if p.repeat != "until_ack" and p.repeat < 1:
        w("error", f"repeat must be >= 1 or 'until_ack', got {p.repeat!r}")
    if p.repeat == "until_ack" and p.max_s is None:
        w("warning", f"until_ack without max_s; stops after {DEFAULT_MAX_S:g} s")
    if p.steps and p.pass_ms(floor) <= 0:
        w("error", "a pass takes no time (all steps ms = 0)")
    for i, s in enumerate(p.steps):
        if s.ms < 0:
            w("error", f"step {i}: negative ms")
        if s.look.bri is not None and not 10 <= s.look.bri <= 100:
            w("error", f"step {i}: bri {s.look.bri} outside 10..100")
        if s.look.temp is not None and not 2200 <= s.look.temp <= 6500:
            w("warning", f"step {i}: temp {s.look.temp} K outside the bulb range 2200..6500")
        for r in s.bulbs or ():
            if r not in ROLES:
                w("warning", f"step {i}: unknown bulb role {r!r}")
        sub = s.ms / s.ramp if s.ramp > 1 else s.ms
        if 0 < sub < floor and not p.allow_fast:
            what = f"ramp sub-step {sub:.0f} ms" if s.ramp > 1 else f"{s.ms} ms"
            w("warning", f"step {i}: {what} is below the {floor} ms floor; stretched (set allow_fast = true to opt in)")
        if s.ramp > 1 and s.wait:
            w("warning", f"step {i}: ramp on a pause step does nothing")
    return out


def _hex(v: Any, where: str) -> tuple[int, int, int]:
    s = str(v).removeprefix("#")
    if len(s) != 6:
        raise PatternError(f"{where}: colour must be 'rrggbb', got {v!r}")
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        raise PatternError(f"{where}: colour must be 'rrggbb', got {v!r}") from None


def resolve_priority(v: int | str) -> int:
    if isinstance(v, bool):
        raise PatternError(f"bad priority {v!r}")
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        if v in PRIORITY:
            return PRIORITY[v]
        if v.lstrip("-").isdigit():
            return int(v)
    raise PatternError(f"unknown priority {v!r}; use an int or one of {sorted(PRIORITY)}")


def _look(d: dict[str, Any], where: str) -> Look:
    if "color" in d and "temp" in d:
        raise PatternError(f"{where}: use color or temp, not both")
    return Look(
        rgb=_hex(d["color"], where) if "color" in d else None,
        temp=int(d["temp"]) if "temp" in d else None,
        off=bool(d.get("off", False)),
        bri=int(d["bri"]) if "bri" in d else None,
    )


def _bulbs(v: Any, where: str) -> tuple[str, ...] | None:
    if v is None or v == "all":
        return None
    if isinstance(v, str):
        return (v,)
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return None if "all" in v else tuple(v)
    raise PatternError(f"{where}: bulbs must be 'all', a role or a list of roles")


def _pattern(name: str, d: dict[str, Any], notes: list[str]) -> Pattern:
    for k in set(d) - PATTERN_KEYS:
        notes.append(f"warning: {name}: unknown key {k!r}")
    steps: list[Step] = []
    for i, sd in enumerate(d.get("steps", [])):
        where = f"pattern {name} step {i}"
        if not isinstance(sd, dict):
            raise PatternError(f"{where}: must be a table")
        for k in set(sd) - STEP_KEYS:
            notes.append(f"warning: {name}: step {i}: unknown key {k!r}")
        steps.append(Step(
            bulbs=_bulbs(sd.get("bulbs"), where),
            look=_look(sd, where),
            base=bool(sd.get("base", False)),
            ms=int(sd.get("ms", MIN_STEP_MS)),
            ramp=int(sd.get("ramp", 0)),
        ))
    rep = d.get("repeat", 1)
    if rep != "until_ack" and not isinstance(rep, int):
        raise PatternError(f"pattern {name}: repeat must be an int or 'until_ack'")
    swatch = d.get("swatch")
    if swatch is None:
        first = next((s.look.rgb for s in steps if s.look.rgb), None)
        swatch = "#%02x%02x%02x" % first if first else "#ffffff"
    else:
        swatch = "#%02x%02x%02x" % _hex(swatch, f"pattern {name} swatch")
    try:
        prio = resolve_priority(d.get("priority", PRIORITY.get(name, PRIORITY["default"])))
    except PatternError as e:
        raise PatternError(f"pattern {name}: {e}") from None
    return Pattern(
        name=name,
        description=str(d.get("description", "")),
        priority=prio,
        swatch=swatch,
        repeat=rep,
        steps=tuple(steps),
        max_s=float(d["max_s"]) if "max_s" in d else None,
        quiet=str(d.get("quiet", "dim")),
        allow_fast=bool(d.get("allow_fast", False)),
        only_lit=bool(d.get("only_lit", False)),
    )


def scene_id(v: Any, where: str) -> int:
    if isinstance(v, int) and not isinstance(v, bool) and v in SCENES.values():
        return v
    name = str(v).lower().replace(" ", "").replace("-", "")
    if name not in SCENES:
        raise PatternError(f"{where}: unknown scene {v!r}; try one of {', '.join(SCENES)}")
    return SCENES[name]


def _preset(name: str, d: dict[str, Any], notes: list[str]) -> Preset:
    where = f"preset {name}"
    for k in set(d) - PRESET_KEYS:
        notes.append(f"warning: {where}: unknown key {k!r}")
    if sum(k in d for k in ("color", "temp", "scene")) != 1:
        raise PatternError(f"{where}: needs exactly one of color, temp or scene")
    rgb = _hex(d["color"], where) if "color" in d else None
    temp = int(d["temp"]) if "temp" in d else None
    scene = scene_id(d["scene"], where) if "scene" in d else None
    if "swatch" in d:
        swatch = "#%02x%02x%02x" % _hex(d["swatch"], where)
    else:
        swatch = "#%02x%02x%02x" % (rgb or kelvin_rgb(temp or 2700))
    return Preset(name=name, description=str(d.get("description", "")), swatch=swatch, rgb=rgb, temp=temp,
                  scene=scene, speed=int(d["speed"]) if "speed" in d else None,
                  bri=int(d["bri"]) if "bri" in d else None)


def loads(text: str, source: str = "<string>") -> PatternSet:
    raw = tomllib.loads(text)
    notes: list[str] = []
    pats = {n: _pattern(n, d, notes) for n, d in raw.get("pattern", {}).items()}
    modes: dict[str, Mode] = {}
    for n, d in raw.get("mode", {}).items():
        looks = {r: _look(v, f"mode {n}.{r}") for r, v in d.items() if isinstance(v, dict)}
        modes[n] = Mode(name=n, description=str(d.get("description", "")), looks=looks)
    presets = {n: _preset(n, d, notes) for n, d in raw.get("preset", {}).items()}
    return PatternSet(pats, modes, source, notes, presets)


def load(path: str | Path) -> PatternSet:
    p = Path(path).expanduser()
    return loads(p.read_text(), str(p))


def kelvin_rgb(k: int) -> tuple[int, int, int]:
    t = k / 100
    r = 255.0 if t <= 66 else 329.698727446 * (t - 60) ** -0.1332047592
    g = 99.4708025861 * math.log(t) - 161.1195681661 if t <= 66 else 288.1221695283 * (t - 60) ** -0.0755148492
    b = 255.0 if t >= 66 else (0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307)
    c = lambda x: max(0, min(255, round(x)))  # noqa: E731
    return (c(r), c(g), c(b))


def lerp_state(a: LightState | None, b: LightState, f: float) -> LightState:
    """Intermediate state f (0..1) of the way from a to b; f >= 1 is exactly b."""
    if f >= 1 or a is None or a.scene or b.scene:
        return b
    if not a.on and not b.on:
        return b
    if not a.on:
        a = replace(b, dimming=10)
    if not b.on:
        b = replace(a, dimming=10)
    mix = lambda x, y: x + (y - x) * f  # noqa: E731
    dim = round(mix(a.dimming or 100, b.dimming or 100))
    if a.rgb is None and b.rgb is None:
        return LightState(on=True, dimming=dim, temp=round(mix(a.temp or 2700, b.temp or 2700)))
    ra = a.rgb or kelvin_rgb(a.temp or 2700)
    rb = b.rgb or kelvin_rgb(b.temp or 2700)
    return LightState(on=True, dimming=dim, rgb=tuple(round(mix(x, y)) for x, y in zip(ra, rb)))  # type: ignore[arg-type]


def _repeat_label(p: Pattern) -> str:
    return "until_ack" if p.repeat == "until_ack" else f"x{p.repeat}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bulbsig patterns")
    ap.add_argument("cmd", choices=["list", "validate"])
    ap.add_argument("file", nargs="?")
    a = ap.parse_args(argv)
    path = a.file
    if path is None:
        from . import config
        path = config.load().patterns_file
    try:
        ps = load(path)
    except (OSError, ValueError) as e:
        print(f"error: {path}: {e}", file=sys.stderr)
        return 1
    if a.cmd == "list":
        for p in ps.patterns.values():
            secs = p.pass_ms() / 1000
            print(f"{p.name:16} {p.priority:4} {_repeat_label(p):10} {secs:6.1f}s  {p.swatch}  {p.description}")
        for m in ps.modes.values():
            roles = ", ".join(m.looks)
            print(f"{'mode ' + m.name:16} {'':4} {'':10} {'':7}  {'':7}  {m.description} [{roles}]")
        for pr in ps.presets.values():
            print(f"{'preset ' + pr.name:16} {'':4} {'':10} {'':7}  {pr.swatch}  {pr.description}")
        return 0
    problems = ps.validate()
    for msg in problems:
        print(msg)
    errors = sum(m.startswith("error") for m in problems)
    print(f"{ps.source}: {len(ps.patterns)} patterns, {len(ps.modes)} modes, {len(ps.presets)} presets, "
          f"{errors} errors, {len(problems) - errors} warnings")
    return 1 if errors else 0
