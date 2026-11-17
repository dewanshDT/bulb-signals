"""`bulbsig tui`: terminal dashboard, a pure HTTP client of the daemon."""
from __future__ import annotations

import argparse
import asyncio
import codecs
import json
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from math import log
from typing import Any
from urllib.parse import quote

import aiohttp
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from .contracts import SCENES

FEED_MAX = 500

BG = "#11141a"
PANEL = "#171b23"
EDGE = "#2a303c"
FG = "#c9d1dc"
DIM = "#6b7585"
ACCENT = "#7aa2c8"
OK = "#7fb48a"
WARN = "#d8b26a"
BAD = "#d07a7a"


class AuthError(Exception):
    pass


class SSEParser:
    """Incremental text/event-stream parser; chunks may split anywhere, even mid-line."""

    def __init__(self) -> None:
        self._buf = ""
        self._dec = codecs.getincrementaldecoder("utf-8")("replace")
        self._event = ""
        self._data: list[str] = []

    def feed(self, chunk: bytes | str) -> list[tuple[str, str]]:
        self._buf += self._dec.decode(chunk) if isinstance(chunk, bytes) else chunk
        out: list[tuple[str, str]] = []
        while True:
            idx = min((i for i in (self._buf.find("\n"), self._buf.find("\r")) if i >= 0), default=-1)
            if idx < 0:
                break
            # A lone trailing \r might be the first half of \r\n; wait for more.
            if self._buf[idx] == "\r" and idx == len(self._buf) - 1:
                break
            line = self._buf[:idx]
            skip = 2 if self._buf[idx:idx + 2] == "\r\n" else 1
            self._buf = self._buf[idx + skip:]
            if line == "":
                if self._data:
                    out.append((self._event or "message", "\n".join(self._data)))
                self._event, self._data = "", []
                continue
            if line.startswith(":"):
                continue
            name, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if name == "event":
                self._event = value
            elif name == "data":
                self._data.append(value)
        return out


class DaemonClient:
    def __init__(self, url: str, token: str, *, read_timeout: float = 60.0) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.read_timeout = read_timeout
        self._session: aiohttp.ClientSession | None = None

    def _s(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.token}"} if self.token else {})
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _req(self, method: str, path: str, body: Any = None) -> Any:
        async with self._s().request(method, self.url + path, json=body,
                                     timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 401:
                raise AuthError()
            text = await r.text()
            if r.status >= 400:
                try:
                    msg = json.loads(text).get("error")
                except (ValueError, AttributeError):
                    msg = None
                raise aiohttp.ClientResponseError(r.request_info, r.history, status=r.status,
                                                  message=str(msg or r.reason or ""))
            return json.loads(text) if text.strip() else {}

    async def get_state(self) -> dict[str, Any]:
        d = await self._req("GET", "/v1/state")
        return d if isinstance(d, dict) else {}

    async def get_patterns(self) -> list[dict[str, Any]]:
        d = await self._req("GET", "/v1/patterns")
        return [p for p in d if isinstance(p, dict)] if isinstance(d, list) else []

    async def _list(self, path: str) -> list[dict[str, Any]]:
        try:
            d = await self._req("GET", path)
        except aiohttp.ClientResponseError as e:
            if e.status in (404, 503):  # older daemon, or scenes.toml broken
                return []
            raise
        return [p for p in d if isinstance(p, dict)] if isinstance(d, list) else []

    async def get_presets(self) -> list[dict[str, Any]]:
        return await self._list("/v1/presets")

    async def get_scenes(self) -> list[dict[str, Any]]:
        return await self._list("/v1/scenes")

    async def post(self, path: str, body: dict[str, Any], method: str = "POST") -> Any:
        return await self._req(method, path, body if method == "POST" else None)

    async def events(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=self.read_timeout)
        async with self._s().get(self.url + "/v1/events", timeout=timeout,
                                 headers={"Accept": "text/event-stream"}) as r:
            if r.status == 401:
                raise AuthError()
            r.raise_for_status()
            parser = SSEParser()
            async for chunk in r.content.iter_any():
                for ev, raw in parser.feed(chunk):
                    try:
                        data = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(data, dict):
                        yield data.get("type") or ev, data
        raise ConnectionError("event stream closed")


def _d(x: Any) -> dict[str, Any]:
    return x if isinstance(x, dict) else {}


def _l(x: Any) -> list[Any]:
    return x if isinstance(x, list) else []


def _hex(s: Any, default: str = "#556070") -> str:
    s = str(s or "").lstrip("#")
    if len(s) == 6 and all(c in "0123456789abcdefABCDEF" for c in s):
        return "#" + s.lower()
    return default


def kelvin_rgb(k: float) -> tuple[int, int, int]:
    t = max(1000.0, min(40000.0, float(k))) / 100
    if t <= 66:
        r, g = 255.0, 99.4708025861 * log(t) - 161.1195681661
        b = 0.0 if t <= 19 else 138.5177312231 * log(t - 10) - 305.0447927307
    else:
        r = 329.698727446 * (t - 60) ** -0.1332047592
        g = 288.1221695283 * (t - 60) ** -0.0755148492
        b = 255.0
    c = lambda v: int(max(0, min(255, v)))  # noqa: E731
    return c(r), c(g), c(b)


def short_app(app: Any) -> str:
    a = str(app or "?")
    low = a.lower()
    for key, name in (("slack", "Slack"), ("zen", "Zen"), ("zoom", "Zoom"), ("teams", "Teams"),
                      ("calendar", "Calendar"), ("mail", "Mail"), ("messages", "Messages")):
        if key in low:
            return name
    return a.rsplit(".", 1)[-1][:10] or "?"


def _parse_dt(s: Any) -> datetime | None:
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(s, timezone.utc)
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.astimezone()


def _clock(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.now().strftime("%H:%M")


def _dur(secs: float) -> str:
    secs = int(max(0, secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def match_preset(s: dict[str, Any], presets: list[dict[str, Any]]) -> str | None:
    """The name of the preset a bulb state shows, if any."""
    for p in presets:
        if p.get("bri") is not None and s.get("dimming") != p.get("bri"):
            continue
        if p.get("scene"):
            hit = s.get("scene") == p.get("scene")
        elif p.get("rgb"):
            hit = list(s.get("rgb") or []) == list(p["rgb"])
        elif p.get("temp"):
            hit = not s.get("rgb") and not s.get("scene") and s.get("temp") is not None \
                and abs(float(s["temp"]) - float(p["temp"])) <= 50
        else:
            hit = False
        if hit:
            return str(p.get("name"))
    return None


def look_matches(look: dict[str, Any], s: dict[str, Any]) -> bool:
    """Whether a bulb state shows a scene's look for that bulb."""
    if not look.get("on", True):
        return not s.get("on")
    if not s.get("on"):
        return False
    if look.get("bri") is not None and s.get("dimming") != look["bri"]:
        return False
    if look.get("color"):
        return _describe(s) == "#" + str(look["color"]).lower()
    if look.get("scene"):
        return s.get("scene") == SCENES.get(str(look["scene"]), look["scene"])
    if look.get("temp"):
        return not s.get("rgb") and not s.get("scene") and s.get("temp") is not None \
            and abs(float(s["temp"]) - float(look["temp"])) <= 50
    return True


def match_scene(state: dict[str, Any], scenes: list[dict[str, Any]]) -> str | None:
    """The room scene every bulb currently shows, if any."""
    bulbs = _d(state.get("bulbs"))
    for sc in scenes:
        looks = _d(sc.get("bulbs"))
        if looks and all(r in bulbs and look_matches(_d(lk), _d(_d(bulbs[r]).get("state")))
                         for r, lk in looks.items()):
            return str(sc.get("name"))
    return None


TARGETS = ("desk", "room", "all")
DELETE_CONFIRM_S = 3.0


class LightScreen(ModalScreen[None]):
    """Room scenes and resting colours. Enter applies; nothing changes while browsing."""

    CSS = f"""
    LightScreen {{ align: center middle; background: #000000 50%; }}
    #light {{ width: 64; height: auto; max-height: 95%; background: {PANEL}; border: round {ACCENT};
        border-title-color: {ACCENT}; border-title-style: bold; padding: 0 1; }}
    #light-head {{ height: auto; margin: 0 0 1 0; }}
    #light-presets {{ height: auto; max-height: 24; background: {PANEL}; border: none; }}
    #light-presets > .option-list--option-highlighted {{ background: #253042; }}
    .light-input {{ display: none; background: {PANEL}; border: round {EDGE}; }}
    .light-input.shown {{ display: block; }}
    #light-help {{ color: {DIM}; height: auto; margin: 1 0 0 0; }}
    """

    BINDINGS = [
        Binding("tab", "target", "Bulb", priority=True),
        Binding("left", "bri(-10)", "Dimmer"),
        Binding("right", "bri(10)", "Brighter"),
        Binding("t", "toggle", "On/off"),
        Binding("c", "custom", "Custom"),
        Binding("s", "save_scene", "Save scene"),
        Binding("x", "delete_scene", "Delete scene"),
        Binding("escape", "close", "Close"),
        Binding("l", "close", "Close", show=False),
    ]

    def __init__(self, presets: list[dict[str, Any]], state: dict[str, Any], target: str = "all",
                 debounce_s: float = 0.2, scenes: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.presets = presets
        self.scenes = scenes or []
        self.state = state
        self.target = target
        self.debounce_s = debounce_s
        self.bri = self._target_bri()
        self._bri_timer: Any = None
        self._delete_armed: tuple[str, float] | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="light"):
            yield Static(id="light-head")
            yield OptionList(id="light-presets")
            yield Input(placeholder="rrggbb — enter to apply, esc to cancel", id="light-hex",
                        classes="light-input")
            yield Input(placeholder="scene name — enter to save what the bulbs show now, esc to cancel",
                        id="light-name", classes="light-input")
            yield Static("enter apply · tab bulb · ←/→ bri · t on/off · c hex\n"
                         "s save as scene · x delete scene · esc close", id="light-help")

    def on_mount(self) -> None:
        self.query_one("#light").border_title = "Light"
        self.render_options()
        self.query_one("#light-presets", OptionList).focus()
        self.render_head()

    def render_options(self) -> None:
        ol = self.query_one("#light-presets", OptionList)
        keep = ol.get_option_at_index(ol.highlighted).id if ol.highlighted is not None and ol.option_count else None
        opts = [Option(Text("Scenes · both bulbs", f"bold {ACCENT}"), disabled=True)]
        for sc in self.scenes:
            prompt = Text()
            for role in sorted(_d(sc.get("bulbs"))):
                prompt.append("█▌", _hex(_d(_d(sc["bulbs"]).get(role)).get("swatch")))
            prompt.append(f" {sc.get('name') or '?'}", f"bold {FG}")
            opts.append(Option(prompt, id=f"scene:{sc.get('name')}"))
        if not self.scenes:
            opts.append(Option(Text("  none yet — set the lights, then s", DIM), disabled=True))
        opts.append(Option(Text("Colours · target bulb", f"bold {ACCENT}"), disabled=True))
        for p in self.presets:
            prompt = Text.assemble(("███ ", _hex(p.get("swatch"))), (str(p.get("name") or "?"), f"bold {FG}"))
            if p.get("description"):
                prompt.append(f"  {p.get('description')}", DIM)
            opts.append(Option(prompt, id=f"preset:{p.get('name')}"))
        ol.set_options(opts)
        ids = [o.id for o in opts]
        first = next((i for i, o in enumerate(opts) if not o.disabled), None)
        idx = ids.index(keep) if keep is not None and keep in ids else first
        ol.highlighted = idx

    def _roles(self) -> list[str]:
        bulbs = sorted(_d(self.state.get("bulbs")))
        return bulbs if self.target == "all" else [self.target]

    def _states(self) -> list[dict[str, Any]]:
        bulbs = _d(self.state.get("bulbs"))
        return [_d(_d(bulbs.get(r)).get("state")) for r in self._roles()]

    def _target_bri(self) -> int:
        for s in self._states():
            if s.get("on") and isinstance(s.get("dimming"), (int, float)):
                return int(s["dimming"])
        return 100

    def update_state(self, state: dict[str, Any]) -> None:
        self.state = state
        self.render_head()

    def update_scenes(self, scenes: list[dict[str, Any]]) -> None:
        self.scenes = scenes
        self.render_options()
        self.render_head()

    def render_head(self) -> None:
        t = Text()
        for name in TARGETS:
            t.append(f" {name} ", f"bold {BG} on {ACCENT}" if name == self.target else DIM)
            t.append(" ")
        t.append(f"   bri {self.bri}%", FG)
        if sc := match_scene(self.state, self.scenes):
            t.append(f"   scene {sc}", ACCENT)
        for r, s in zip(self._roles(), self._states()):
            t.append(f"\n{r:<5} ", f"bold {FG}")
            if not s:
                t.append("unknown", DIM)
            elif not s.get("on"):
                t.append("off", DIM)
            else:
                t.append(match_preset(s, self.presets) or _describe(s), FG)
                if s.get("dimming") is not None:
                    t.append(f"  {s.get('dimming')}%", DIM)
        self.query_one("#light-head", Static).update(t)

    def _apply(self, body: dict[str, Any], ok: str) -> None:
        self.app._send("/v1/light", {"bulbs": self.target, **body}, f"{self.target}: {ok}")  # type: ignore[attr-defined]

    def _highlighted_scene(self) -> str | None:
        ol = self.query_one("#light-presets", OptionList)
        if ol.highlighted is None or not ol.option_count:
            return None
        oid = ol.get_option_at_index(ol.highlighted).id or ""
        return oid.removeprefix("scene:") if oid.startswith("scene:") else None

    @on(OptionList.OptionSelected, "#light-presets")
    def _picked(self, ev: OptionList.OptionSelected) -> None:
        ev.stop()
        kind, _, name = (ev.option.id or "").partition(":")
        if kind == "scene":
            self.app._send(f"/v1/scenes/{quote(name)}/apply", {}, f"scene {name}")  # type: ignore[attr-defined]
        elif kind == "preset":
            self._apply({"preset": name}, name)

    def action_target(self) -> None:
        self.target = TARGETS[(TARGETS.index(self.target) + 1) % len(TARGETS)]
        self.bri = self._target_bri()
        self.render_head()

    def action_bri(self, step: int) -> None:
        self.bri = max(10, min(100, self.bri + step))
        self.render_head()
        if self._bri_timer is not None:
            self._bri_timer.stop()
        self._bri_timer = self.set_timer(self.debounce_s, self._send_bri)

    def _send_bri(self) -> None:
        self._bri_timer = None
        self._apply({"bri": self.bri}, f"{self.bri}%")

    def action_toggle(self) -> None:
        on = not any(s.get("on") for s in self._states())
        self._apply({"on": on}, "on" if on else "off")

    def _show_input(self, wid: str) -> None:
        for inp in self.query(".light-input").results(Input):
            inp.remove_class("shown")
        inp = self.query_one(wid, Input)
        inp.add_class("shown")
        inp.focus()

    def action_custom(self) -> None:
        self._show_input("#light-hex")

    def action_save_scene(self) -> None:
        name = self._highlighted_scene()
        self._show_input("#light-name")
        inp = self.query_one("#light-name", Input)
        if name and not inp.value:
            inp.value = name  # enter overwrites the highlighted scene; type a new name for a new one

    def action_delete_scene(self) -> None:
        name = self._highlighted_scene()
        if name is None:
            self.app.notify("highlight a scene to delete it", timeout=2)
            return
        now = time.monotonic()
        if self._delete_armed and self._delete_armed[0] == name and now - self._delete_armed[1] < DELETE_CONFIRM_S:
            self._delete_armed = None
            self.app._send(f"/v1/scenes/{quote(name)}", {}, f"deleted {name}", method="DELETE")  # type: ignore[attr-defined]
        else:
            self._delete_armed = (name, now)
            self.app.notify(f"press x again to delete {name}", timeout=DELETE_CONFIRM_S)

    @on(Input.Submitted, "#light-hex")
    def _hex_submitted(self, ev: Input.Submitted) -> None:
        ev.stop()
        h = ev.value.strip().lstrip("#")
        if _hex(h, "") == "":
            self.app.notify(f"not a colour: {ev.value!r}; want rrggbb", severity="error", timeout=3)
            return
        self._apply({"color": h.lower()}, f"#{h.lower()}")
        self._hide_inputs()

    @on(Input.Submitted, "#light-name")
    def _name_submitted(self, ev: Input.Submitted) -> None:
        ev.stop()
        name = " ".join(ev.value.split())
        if not name:
            return
        self.app._send("/v1/scenes", {"name": name}, f"saved scene {name.lower()}")  # type: ignore[attr-defined]
        self._hide_inputs()

    def _hide_inputs(self) -> bool:
        shown = False
        for inp in self.query(".light-input").results(Input):
            shown = shown or inp.has_class("shown")
            inp.value = ""
            inp.remove_class("shown")
        self.query_one("#light-presets", OptionList).focus()
        return shown

    def action_close(self) -> None:
        if self._hide_inputs():
            return
        if self._bri_timer is not None:
            self._bri_timer.stop()
            self._send_bri()
        self.dismiss()

def _describe(s: dict[str, Any]) -> str:
    if s.get("scene"):
        return f"scene {s.get('scene')}"
    rgb = s.get("rgb")
    if isinstance(rgb, list) and len(rgb) == 3:
        try:
            return "#%02x%02x%02x" % tuple(max(0, min(255, int(c))) for c in rgb)
        except (TypeError, ValueError):
            return "rgb"
    if s.get("temp"):
        return f"{s.get('temp')}K"
    return "on"


@dataclass
class FeedEntry:
    app: str
    text: str
    line: Text


class BulbTUI(App[int]):
    TITLE = "Bulb Signals"
    CSS = f"""
    Screen {{ background: {BG}; color: {FG}; }}
    #topbar {{ height: 1; background: {PANEL}; padding: 0 1; }}
    #body {{ height: 1fr; }}
    #left {{ width: 1fr; }}
    #right {{ width: 50; }}
    .panel {{
        background: {PANEL}; border: round {EDGE}; border-title-color: {ACCENT};
        border-title-style: bold; padding: 0 1; margin: 0 0 0 1; height: auto;
    }}
    #feed {{ background: {PANEL}; border: round {EDGE}; border-title-color: {ACCENT};
        border-title-style: bold; height: 1fr; padding: 0 1; scrollbar-size-vertical: 1; }}
    #filter {{ display: none; height: 3; background: {PANEL}; border: round {ACCENT}; }}
    #filter.shown {{ display: block; }}
    #patterns {{ background: {PANEL}; border: round {EDGE}; border-title-color: {ACCENT};
        border-title-style: bold; height: 1fr; margin: 0 0 0 1; padding: 0 0; scrollbar-size-vertical: 1; }}
    #patterns:focus {{ border: round {ACCENT}; }}
    #patterns > .option-list--option-highlighted {{ background: #253042; }}
    Footer {{ background: {PANEL}; }}
    """

    BINDINGS = [
        Binding("space", "ack", "Ack"),
        Binding("p", "play", "Play"),
        Binding("enter", "play", "Play", show=False),
        Binding("m", "mute", "Mute"),
        Binding("o", "on_air", "On air"),
        Binding("f", "focus_mode", "Focus"),
        Binding("l", "light", "Light"),
        Binding("r", "refresh", "Refresh"),
        Binding("slash", "filter", "Filter"),
        Binding("escape", "clear_filter", "Clear filter", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, client: DaemonClient, *, backoff_initial: float = 1.0,
                 backoff_max: float = 5.0, poll_s: float = 15.0) -> None:
        super().__init__()
        self.client = client
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.poll_s = poll_s
        self.state: dict[str, Any] = {}
        self.patterns: list[dict[str, Any]] = []
        self.presets: list[dict[str, Any]] = []
        self.scenes: list[dict[str, Any]] = []
        self.swatches: dict[str, str] = {}
        self.status = "connecting"
        self.status_detail = ""
        self.feed: deque[FeedEntry] = deque(maxlen=FEED_MAX)
        self.filter_text = ""
        self._mute_step = 0
        self._connected = False

    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Input(placeholder="filter by app or text — enter to keep, esc to clear", id="filter")
                yield RichLog(id="feed", max_lines=FEED_MAX, wrap=False, markup=False, auto_scroll=True)
            with Vertical(id="right"):
                yield Static(id="now", classes="panel")
                yield Static(id="bulbs", classes="panel")
                yield Static(id="meetings", classes="panel")
                yield OptionList(id="patterns")
        yield Footer()

    def on_mount(self) -> None:
        for wid, title in (("feed", "Feed"), ("now", "Now"), ("bulbs", "Bulbs"),
                           ("meetings", "Upcoming"), ("patterns", "Signals")):
            self.query_one(f"#{wid}").border_title = title
        self.query_one("#patterns", OptionList).focus()
        self.render_all()
        self.set_interval(1.0, self._tick)
        self.set_interval(self.poll_s, self._poll)
        self.run_worker(self._stream_loop(), group="stream", exclusive=True)

    async def on_unmount(self) -> None:
        await self.client.close()

    # ---- networking ----

    async def _load(self) -> None:
        state, patterns, presets, scenes = await asyncio.gather(
            self.client.get_state(), self.client.get_patterns(), self.client.get_presets(),
            self.client.get_scenes())
        self.state = state
        self.patterns = patterns
        self.presets = presets
        self.scenes = scenes
        self.swatches = {str(p.get("name")): _hex(p.get("swatch")) for p in patterns}
        self._mute_step = 1 if self._muted() else 0
        self.feed.clear()
        for n in _l(state.get("notifications")):
            self._add_notification(_d(n), redraw=False)
        self.render_patterns()
        self.render_all()
        self.redraw_feed()

    async def _stream_loop(self) -> None:
        delay = self.backoff_initial
        attempt = 0
        while True:
            try:
                await self._load()
                self._connected = True
                self.set_status("connected")
                delay, attempt = self.backoff_initial, 0
                async for etype, data in self.client.events():
                    self.apply_event(etype, data)
            except AuthError:
                self._connected = False
                self.set_status("auth", "auth failed — check token")
                return
            except NoMatches:  # widgets torn down: the app is exiting
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # any network/parse failure means: reconnect
                self._connected = False
                attempt += 1
                self.set_status("reconnecting", f"reconnecting in {delay:.0f}s (#{attempt}) · {type(e).__name__}")
                await asyncio.sleep(delay)
                delay = min(self.backoff_max, delay * 2)

    async def _reload_scenes(self) -> None:
        try:
            self.scenes = await self.client.get_scenes()
        except Exception:
            return
        if isinstance(self.screen, LightScreen):
            self.screen.update_scenes(self.scenes)
        self.render_bulbs()

    async def _poll(self) -> None:
        if not self._connected:
            return
        try:
            st = await self.client.get_state()
        except Exception:
            return
        notes = self.state.get("notifications")
        self.state = st | {"notifications": notes}
        try:
            self.render_all()
        except NoMatches:
            pass

    def _send(self, path: str, body: dict[str, Any], ok: str, method: str = "POST") -> None:
        async def go() -> None:
            try:
                res = await self.client.post(path, body, method)
            except AuthError:
                self.set_status("auth", "auth failed — check token")
                return
            except aiohttp.ClientResponseError as e:
                self.notify(e.message or f"{path} failed ({e.status})", severity="error")
                return
            except Exception as e:
                self.notify(f"{path} failed: {e}", severity="error")
                return
            if path == "/v1/ack" and isinstance(res, dict) and res.get("acked") is False:
                self.notify("nothing to ack", timeout=2)
            else:
                self.notify(ok, timeout=2)
        self.run_worker(go(), group="post")

    # ---- events ----

    def apply_event(self, etype: str, d: dict[str, Any]) -> None:
        st = self.state
        sig = str(d.get("signal") or "")
        if etype == "notification":
            notes = _l(st.get("notifications"))
            notes.append(d)
            st["notifications"] = notes[-FEED_MAX:]
            self._add_notification(d)
        elif etype == "queued":
            q = _l(st.get("queue"))
            q.append({k: d.get(k) for k in ("job", "signal", "priority", "key")})
            st["queue"] = q
        elif etype == "merged":
            self._add_line("", f"merged {sig}", self._sig_line("⧉", sig, f"merged ×{d.get('count', '?')}"))
        elif etype == "signal_start":
            st["queue"] = [j for j in _l(st.get("queue")) if _d(j).get("job") != d.get("job")]
            st["playing"] = {"job": d.get("job"), "signal": sig, "started": time.time(),
                             "repeating": d.get("repeating")}
            self._add_line("", f"start {sig}", self._sig_line("▶", sig, "start"))
        elif etype == "signal_end":
            self.set_timer(1.5, self._poll)  # bulb colours aren't evented; pick up the restore
            if _d(st.get("playing")).get("job") == d.get("job"):
                st["playing"] = None
            st["queue"] = [j for j in _l(st.get("queue")) if _d(j).get("job") != d.get("job")]
            self._add_line("", f"end {sig}", self._sig_line("■", sig, f"end · {d.get('reason') or '?'}"))
        elif etype == "mode":
            mode, active = str(d.get("mode") or ""), bool(d.get("active"))
            self.set_timer(1.5, self._poll)
            if mode == "focus":
                st["focus"] = active
            else:
                modes = [m for m in _l(st.get("modes")) if m != mode]
                st["modes"] = modes + [mode] if active else modes
        elif etype == "mute":
            st["muted_until"] = d.get("until")
            if not d.get("until"):
                self._mute_step = 0
        elif etype == "bulb_error":
            role = str(d.get("role") or "?")
            b = _d(_d(st.get("bulbs")).get(role))
            if b:
                b["reachable"] = False
            line = Text.assemble((f"{_clock(time.time())}  ", DIM), ("⚠ ", BAD),
                                 (f"{role} bulb: {d.get('error') or 'error'}", BAD))
            self._add_line(role, f"bulb {role} {d.get('error')}", line)
        elif etype == "light":
            b = _d(_d(st.get("bulbs")).get(str(d.get("role") or "")))
            if b and isinstance(d.get("state"), dict):
                b["state"] = d["state"]
            self.set_timer(1.5, self._poll)
        elif etype == "scenes":
            self.run_worker(self._reload_scenes(), group="scenes", exclusive=True)
        elif etype == "meeting":
            title = str(d.get("title") or "meeting")
            for m in _l(st.get("meetings")):
                if _d(m).get("title") == d.get("title") and _d(m).get("start") == d.get("start"):
                    m["phase"] = d.get("phase")
            line = Text.assemble((f"{_clock(time.time())}  ", DIM), ("◷ ", ACCENT),
                                 (f"{title} · {d.get('phase') or ''}", FG))
            self._add_line("meeting", f"meeting {title}", line)
        self.render_all()

    def _sig_line(self, glyph: str, sig: str, what: str) -> Text:
        sw = self.swatches.get(sig, DIM)
        return Text.assemble((f"{_clock(time.time())}  ", DIM), (f"{glyph} ", sw),
                             (f"{sig or '?':<16}", f"bold {sw}"), (what, DIM))

    def _add_notification(self, d: dict[str, Any], redraw: bool = True) -> None:
        n = _d(d.get("notification"))
        app = short_app(n.get("app"))
        title = str(n.get("title") or "").strip()
        body = " ".join(str(n.get(k) or "") for k in ("subtitle", "body")).strip()
        excerpt = (f"{title} — {body}" if title and body else title or body or "(empty)").replace("\n", " ")
        if len(excerpt) > 72:
            excerpt = excerpt[:71] + "…"
        sig = d.get("signal")
        line = Text.assemble((f"{_clock(n.get('time'))}  ", DIM), (f"{app:<9} ", f"bold {ACCENT}"),
                             (f"{excerpt}  ", FG))
        if sig:
            sw = self.swatches.get(str(sig), DIM)
            line.append("██ ", sw)
            line.append(str(sig), sw)
        else:
            line.append("no rule", f"italic {DIM}")
        self._add_line(f"{app} {n.get('app') or ''}", f"{excerpt} {sig or ''}", line, redraw)

    def _add_line(self, app: str, text: str, line: Text, redraw: bool = True) -> None:
        e = FeedEntry(app.lower(), text.lower(), line)
        self.feed.append(e)
        if redraw and self._matches(e):
            self.query_one("#feed", RichLog).write(line)

    def _matches(self, e: FeedEntry) -> bool:
        f = self.filter_text.lower().strip()
        return not f or f in e.app or f in e.text

    def redraw_feed(self) -> None:
        log_ = self.query_one("#feed", RichLog)
        log_.clear()
        for e in self.feed:
            if self._matches(e):
                log_.write(e.line)
        log_.border_subtitle = f"filter: {self.filter_text}" if self.filter_text else ""

    # ---- rendering ----

    def _muted(self) -> float | None:
        try:
            u = float(self.state.get("muted_until") or 0)
        except (TypeError, ValueError):
            return None
        return u if u > time.time() else None

    def set_status(self, status: str, detail: str = "") -> None:
        self.status, self.status_detail = status, detail
        self.render_topbar()

    def render_topbar(self) -> None:
        t = Text.assemble(("● ", ACCENT), ("Bulb Signals", f"bold {FG}"), ("   ", ""))
        color = {"connected": OK, "reconnecting": WARN, "auth": BAD}.get(self.status, DIM)
        t.append("◉ ", color)
        t.append(self.status_detail or self.status, color)
        badges: list[tuple[str, str]] = []
        if "on_air" in _l(self.state.get("modes")):
            badges.append((" ON AIR ", f"bold #ffffff on {BAD}"))
        if self.state.get("focus"):
            badges.append((" FOCUS ", f"bold {BG} on {ACCENT}"))
        if (u := self._muted()):
            badges.append((f" MUTED {datetime.fromtimestamp(u).strftime('%H:%M')} · {_dur(u - time.time())} ",
                           f"bold {BG} on {WARN}"))
        if self.state.get("quiet"):
            badges.append((" QUIET HOURS ", f"{FG} on #34304a"))
        for b, style in badges:
            t.append("  ")
            t.append(b, style)
        self.query_one("#topbar", Static).update(t)

    def render_now(self) -> None:
        t = Text()
        p = _d(self.state.get("playing"))
        if p:
            sig = str(p.get("signal") or "?")
            sw = self.swatches.get(sig, DIM)
            t.append("▶ ", sw)
            t.append(f"{sig}", f"bold {sw}")
            try:
                t.append(f"  {_dur(time.time() - float(p.get('started')))}", DIM)
            except (TypeError, ValueError):
                pass
            if p.get("repeating"):
                t.append("  ↻ until ack", WARN)
        else:
            t.append("idle", DIM)
        for j in _l(self.state.get("queue"))[:5]:
            j = _d(j)
            sig = str(j.get("signal") or "?")
            t.append("\n  · ", DIM)
            t.append(sig, self.swatches.get(sig, FG))
            if j.get("priority") is not None:
                t.append(f"  p{j.get('priority')}", DIM)
        if len(q := _l(self.state.get("queue"))) > 5:
            t.append(f"\n  … {len(q) - 5} more", DIM)
        self.query_one("#now", Static).update(t)

    def render_bulbs(self) -> None:
        t = Text()
        bulbs = _d(self.state.get("bulbs"))
        if not bulbs:
            t.append("no bulbs", DIM)
        for i, (role, b) in enumerate(sorted(bulbs.items())):
            b = _d(b)
            s = _d(b.get("state"))
            if i:
                t.append("\n")
            reach = b.get("reachable")
            t.append("● " if reach else "○ ", OK if reach else BAD)
            t.append(f"{role:<5} ", f"bold {FG}")
            rgb = s.get("rgb")
            pname = match_preset(s, self.presets) if s.get("on") else None
            if not s.get("on"):
                t.append("██", "#2b2f36")
                t.append(" off      ", DIM)
            elif isinstance(rgb, list) and len(rgb) == 3:
                try:
                    r, g, bl = (max(0, min(255, int(c))) for c in rgb)
                    t.append("██", f"#{r:02x}{g:02x}{bl:02x}")
                    t.append(f" {(pname or 'rgb')[:9]:<9}", DIM)
                except (TypeError, ValueError):
                    t.append("██ ?        ", DIM)
            elif s.get("scene"):
                t.append("▚▚", ACCENT)
                t.append(f" {(pname or 'scene ' + str(s.get('scene')))[:9]:<9}", DIM)
            elif s.get("temp"):
                try:
                    r, g, bl = kelvin_rgb(float(s["temp"]))
                    t.append("██", f"#{r:02x}{g:02x}{bl:02x}")
                    t.append(f" {(pname or str(int(s['temp'])) + 'K')[:9]:<9}", DIM)
                except (TypeError, ValueError):
                    t.append("██ ?        ", DIM)
            else:
                t.append("██ on       ", DIM)
            dim = s.get("dimming")
            t.append(f"{dim}%" if isinstance(dim, (int, float)) and s.get("on") else "  ", FG)
            t.append(f"  {b.get('ip') or '—'}", DIM)
        panel = self.query_one("#bulbs", Static)
        panel.update(t)
        sc = match_scene(self.state, self.scenes)
        panel.border_subtitle = f"scene: {sc}" if sc else ""

    def render_meetings(self) -> None:
        t = Text()
        now = datetime.now(timezone.utc)
        rows = []
        for m in _l(self.state.get("meetings")):
            m = _d(m)
            start, end = _parse_dt(m.get("start")), _parse_dt(m.get("end"))
            if start is None or (end and end < now):
                continue
            rows.append((start, end, str(m.get("title") or "(untitled)")))
        rows.sort(key=lambda r: r[0])
        if not rows:
            t.append("nothing scheduled", DIM)
        for i, (start, end, title) in enumerate(rows[:4]):
            if i:
                t.append("\n")
            secs = (start - now).total_seconds()
            if secs <= 0:
                left = f"ends {_dur((end - now).total_seconds())}" if end else "now"
                t.append(f"{'NOW':>8} ", f"bold {BAD}")
                t.append(f"{title[:24]:<24} ", f"bold {FG}")
                t.append(left, DIM)
                continue
            style = f"bold {WARN}" if secs < 600 else ACCENT
            t.append(f"{_dur(secs):>8} ", style)
            t.append(f"{title[:24]:<24} ", f"bold {FG}" if secs < 600 else FG)
            t.append(start.astimezone().strftime("%H:%M"), DIM)
        self.query_one("#meetings", Static).update(t)

    def render_patterns(self) -> None:
        ol = self.query_one("#patterns", OptionList)
        keep = ol.highlighted
        opts = []
        for p in self.patterns:
            name = str(p.get("name") or "?")
            rep = p.get("repeat")
            prompt = Text.assemble(("███ ", self.swatches.get(name, DIM)), (name, f"bold {FG}"))
            if rep == "until_ack":
                prompt.append("  ↻", WARN)
            if p.get("priority") is not None:
                prompt.append(f"  p{p.get('priority')}", DIM)
            if p.get("description"):
                prompt.append(f"\n    {p.get('description')}", DIM)
            opts.append(Option(prompt, id=name))
        ol.set_options(opts)
        if opts:
            ol.highlighted = keep if keep is not None and keep < len(opts) else 0

    def render_all(self) -> None:
        self.render_topbar()
        self.render_now()
        self.render_bulbs()
        self.render_meetings()
        if isinstance(self.screen, LightScreen):
            self.screen.update_state(self.state)

    def _tick(self) -> None:
        self.render_topbar()
        self.render_meetings()
        if self.state.get("playing"):
            self.render_now()

    # ---- actions ----

    def selected_pattern(self) -> str | None:
        ol = self.query_one("#patterns", OptionList)
        if ol.highlighted is None or not ol.option_count:
            return None
        return ol.get_option_at_index(ol.highlighted).id

    def action_ack(self) -> None:
        self._send("/v1/ack", {}, "acked")

    def action_play(self) -> None:
        if name := self.selected_pattern():
            self._send("/v1/signal", {"signal": name}, f"playing {name}")

    @on(OptionList.OptionSelected, "#patterns")
    def _selected(self, ev: OptionList.OptionSelected) -> None:
        ev.stop()
        if ev.option.id:
            self._send("/v1/signal", {"signal": ev.option.id}, f"playing {ev.option.id}")

    def action_mute(self) -> None:
        if not self._muted():
            self._mute_step = 0
        minutes = (30, 60, 0)[self._mute_step]
        self._mute_step = (self._mute_step + 1) % 3
        self._send("/v1/mute", {"minutes": minutes}, f"muted {minutes} min" if minutes else "unmuted")

    def action_on_air(self) -> None:
        active = "on_air" not in _l(self.state.get("modes"))
        self._send("/v1/mode", {"mode": "on_air", "active": active}, f"ON AIR {'on' if active else 'off'}")

    def action_focus_mode(self) -> None:
        active = not self.state.get("focus")
        self._send("/v1/mode", {"mode": "focus", "active": active}, f"focus {'on' if active else 'off'}")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # these act on main-screen widgets, which a modal hides
        if action in ("play", "filter", "clear_filter", "light") and isinstance(self.screen, ModalScreen):
            return False
        return True

    def action_light(self) -> None:
        if not self.presets and not _d(self.state.get("bulbs")):
            self.notify("no bulbs yet", timeout=2)
            return
        self.push_screen(LightScreen(self.presets, self.state, scenes=self.scenes))

    def action_refresh(self) -> None:
        if self.status == "auth":
            return
        self.run_worker(self._stream_loop(), group="stream", exclusive=True)

    def action_filter(self) -> None:
        inp = self.query_one("#filter", Input)
        inp.add_class("shown")
        inp.focus()

    def action_clear_filter(self) -> None:
        inp = self.query_one("#filter", Input)
        inp.value = ""
        inp.remove_class("shown")
        self.filter_text = ""
        self.redraw_feed()
        self.query_one("#patterns", OptionList).focus()

    @on(Input.Changed, "#filter")
    def _filter_changed(self, ev: Input.Changed) -> None:
        self.filter_text = ev.value
        self.redraw_feed()

    @on(Input.Submitted, "#filter")
    def _filter_submitted(self, ev: Input.Submitted) -> None:
        if not ev.value:
            self.query_one("#filter", Input).remove_class("shown")
        self.query_one("#patterns", OptionList).focus()


def main(argv: list[str] | None = None) -> int:
    from bulbsig import config

    ap = argparse.ArgumentParser(prog="bulbsig tui", description="terminal dashboard for the bulb daemon")
    ap.add_argument("--url")
    ap.add_argument("--token")
    ap.add_argument("--config")
    a = ap.parse_args(argv)
    cfg = config.load(a.config)
    client = DaemonClient(a.url or cfg.url, a.token if a.token is not None else cfg.token)
    BulbTUI(client).run()
    return 0
