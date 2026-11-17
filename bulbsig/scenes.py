"""Room scenes: one named look for several bulbs at once, kept in scenes.toml.

  bulbsig scenes                     list
  bulbsig scenes apply evening
  bulbsig scenes save "late night"   save what the bulbs rest at now
  bulbsig scenes delete movie

All of these go through the daemon.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import tomllib
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .contracts import SCENES, LightState
from .patterns import PatternError, _hex, kelvin_rgb, scene_id

DEFAULT_FILE = Path(__file__).parent / "scenes_default.toml"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9 _-]{0,31}$")
LOOK_KEYS = {"on", "bri", "color", "temp", "scene", "speed"}


class SceneError(ValueError):
    pass


def norm_name(name: Any) -> str:
    n = " ".join(str(name).split()).lower()
    if not NAME_RE.match(n):
        raise SceneError(f"bad scene name {name!r}: 1-32 letters, digits, spaces, '-' or '_'")
    return n


def change_of(st: LightState) -> dict[str, Any]:
    """A bulb state as Engine.set_light keyword arguments."""
    d: dict[str, Any] = {"on": st.on}
    if st.dimming is not None:
        d["bri"] = st.dimming
    if st.scene:
        d["scene"] = st.scene
        if st.speed is not None:
            d["speed"] = st.speed
    elif st.rgb is not None:
        d["rgb"] = tuple(st.rgb)
    elif st.temp is not None:
        d["temp"] = st.temp
    return d


def parse_look(d: Any, where: str) -> dict[str, Any]:
    """{on?, bri?, color|temp|scene, speed?} from TOML or JSON → set_light keyword arguments."""
    if not isinstance(d, dict):
        raise SceneError(f"{where}: must be a table")
    if extra := set(d) - LOOK_KEYS:
        raise SceneError(f"{where}: unknown key {sorted(extra)[0]!r}")
    if sum(d.get(k) is not None for k in ("color", "temp", "scene")) > 1:
        raise SceneError(f"{where}: use one of color, temp or scene")
    out: dict[str, Any] = {"on": d.get("on", True)}
    if not isinstance(out["on"], bool):
        raise SceneError(f"{where}: on must be true or false")

    def num(k: str, lo: int, hi: int) -> int | None:
        v = d.get(k)
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise SceneError(f"{where}: {k} must be a number in {lo}..{hi}")
        return int(v)

    try:
        if d.get("color") is not None:
            out["rgb"] = _hex(d["color"], where)
        elif (t := num("temp", 2200, 6500)) is not None:
            out["temp"] = t
        elif d.get("scene") is not None:
            out["scene"] = scene_id(d["scene"], where)
            if (sp := num("speed", 10, 200)) is not None:
                out["speed"] = sp
    except PatternError as e:
        raise SceneError(str(e)) from None
    if (b := num("bri", 10, 100)) is not None:
        out["bri"] = b
    return out


def look_json(c: dict[str, Any]) -> dict[str, Any]:
    """set_light arguments → the API/TOML form, plus a display swatch."""
    d: dict[str, Any] = {"on": c.get("on", True)}
    if c.get("bri") is not None:
        d["bri"] = c["bri"]
    if c.get("scene"):
        d["scene"] = next((k for k, v in SCENES.items() if v == c["scene"]), c["scene"])
        if c.get("speed") is not None:
            d["speed"] = c["speed"]
    elif c.get("rgb") is not None:
        d["color"] = "%02x%02x%02x" % tuple(c["rgb"])
    elif c.get("temp") is not None:
        d["temp"] = c["temp"]
    if not d["on"]:
        sw = "#2b2f36"
    elif "color" in d:
        sw = "#" + d["color"]
    elif "scene" in d:
        sw = "#ff8a3d"
    else:
        sw = "#%02x%02x%02x" % kelvin_rgb(int(d.get("temp", 2700)))
    return d | {"swatch": sw}


@dataclass(frozen=True)
class Scene:
    name: str
    bulbs: dict[str, dict[str, Any]]  # role → set_light keyword arguments

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "bulbs": {r: look_json(c) for r, c in self.bulbs.items()}}


def parse_scene(name: Any, bulbs: Any) -> Scene:
    n = norm_name(name)
    if not isinstance(bulbs, dict) or not bulbs:
        raise SceneError(f"scene {n}: needs at least one bulb")
    return Scene(n, {str(r): parse_look(v, f"scene {n}.{r}") for r, v in bulbs.items()})


def loads(text: str) -> dict[str, Scene]:
    raw = tomllib.loads(text).get("scene", {})
    out: dict[str, Scene] = {}
    for name, bulbs in raw.items():
        s = parse_scene(name, bulbs)
        out[s.name] = s
    return out


def _toml_value(v: Any) -> str:
    return ("true" if v else "false") if isinstance(v, bool) else json.dumps(v)


def dumps(scenes: dict[str, Scene]) -> str:
    lines = ["# Room scenes for bulbsig. Edited by the TUI (l, then s / x); hand edits are fine.",
             "# [scene.\"name\"]  <role> = { on, bri, color = \"rrggbb\" | temp = K | scene = name, speed }", ""]
    for s in scenes.values():
        lines.append(f"[scene.{json.dumps(s.name)}]")
        for role, c in s.bulbs.items():
            d = {k: v for k, v in look_json(c).items() if k != "swatch"}
            if d.get("on") is True:
                d.pop("on")
            body = ", ".join(f"{k} = {_toml_value(v)}" for k, v in d.items())
            lines.append(f"{role} = {{ {body} }}")
        lines.append("")
    return "\n".join(lines)


class SceneStore:
    """The scene list, read at start and written back on every change."""

    def __init__(self, path: Path) -> None:
        self.path = path
        src = path if path.exists() else DEFAULT_FILE
        self.scenes = loads(src.read_text())

    def get(self, name: str) -> Scene | None:
        try:
            return self.scenes.get(norm_name(name))
        except SceneError:
            return None

    def put(self, scene: Scene) -> bool:
        replaced = scene.name in self.scenes
        new = dict(self.scenes)
        new[scene.name] = scene
        self._write(new)
        return replaced

    def delete(self, name: str) -> bool:
        s = self.get(name)
        if s is None:
            return False
        self._write({k: v for k, v in self.scenes.items() if k != s.name})
        return True

    def _write(self, scenes: dict[str, Scene]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".scenes-", suffix=".toml")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(dumps(scenes))
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self.scenes = scenes


def _describe(look: dict[str, Any]) -> str:
    if not look.get("on", True):
        return "off"
    what = look.get("color") and "#" + look["color"] or look.get("scene") or f"{look.get('temp', '?')}K"
    return f"{what}" + (f" {look['bri']}%" if look.get("bri") is not None else "")


def main(argv: list[str] | None = None) -> int:
    from . import config
    from .wiz import daemon_request

    ap = argparse.ArgumentParser(prog="bulbsig scenes", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="list", choices=["list", "apply", "save", "delete"])
    ap.add_argument("name", nargs="*")
    a = ap.parse_args(argv)
    cfg = config.load()
    name = " ".join(a.name)
    if a.cmd != "list" and not name:
        print(f"usage: bulbsig scenes {a.cmd} NAME", file=sys.stderr)
        return 2
    try:
        if a.cmd == "list":
            for s in daemon_request(cfg, "GET", "/v1/scenes"):
                looks = "  ".join(f"{r} {_describe(v)}" for r, v in s["bulbs"].items())
                print(f"{s['name']:16} {looks}")
        elif a.cmd == "apply":
            res = daemon_request(cfg, "POST", f"/v1/scenes/{quote(name)}/apply", {})
            print(f"applied {name}" + (f" (no bulb {', '.join(res['skipped'])})" if res.get("skipped") else ""))
        elif a.cmd == "save":
            res = daemon_request(cfg, "POST", "/v1/scenes", {"name": name})
            looks = "  ".join(f"{r} {_describe(v)}" for r, v in res["scene"]["bulbs"].items())
            print(f"{'replaced' if res.get('replaced') else 'saved'} {res['scene']['name']}: {looks}")
        else:
            daemon_request(cfg, "DELETE", f"/v1/scenes/{quote(name)}")
            print(f"deleted {name}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", e.reason)
        except ValueError:
            msg = e.reason
        print(f"daemon: {msg}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"daemon at {cfg.url}: {e} (is it running?)", file=sys.stderr)
        return 1
    return 0
