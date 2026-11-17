from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from textual.widgets import OptionList, RichLog, Static

from bulbsig.tui import BulbTUI, DaemonClient, LightScreen, SSEParser, look_matches, match_preset, match_scene

TOKEN = "s3cret"
SCREENSHOT = Path("/tmp/claude-960/-var-lib-storm-runtime-workspaces-scratch/"
                  "ae8cf2fd-7d92-4fae-bdca-b8ece2e4020f/scratchpad/tui.svg")


def _iso(minutes: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


class FakeDaemon:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.subs: list[asyncio.Queue[str | None]] = []
        now = time.time()
        self.state: dict[str, Any] = {
            "playing": {"job": "j1", "signal": "mention", "started": now - 4, "repeating": True},
            "queue": [{"job": "j2", "signal": "dm", "priority": 50, "key": "slack:dm"}],
            "modes": [], "muted_until": None, "quiet": False, "focus": False,
            "bulbs": {
                "desk": {"mac": "a8bb50000001", "ip": "192.168.1.40", "reachable": True,
                         "state": {"on": True, "dimming": 60, "temp": None, "rgb": [90, 60, 220],
                                   "scene": None, "speed": None}},
                "room": {"mac": "a8bb50000002", "ip": "192.168.1.41", "reachable": True,
                         "state": {"on": True, "dimming": 35, "temp": 2700, "rgb": None,
                                   "scene": None, "speed": None}},
            },
            "meetings": [
                {"title": "Design sync", "start": _iso(7), "end": _iso(37), "phase": "t10", "uid": "m1"},
                {"title": "1:1 with Priya", "start": _iso(95), "end": _iso(125), "phase": None, "uid": "m2"},
            ],
            "notifications": [
                {"type": "notification", "rule": "slack-dm", "signal": "dm", "job": "j0",
                 "notification": {"source": "macos", "app": "com.tinyspeck.slackmacgap",
                                  "title": "Asha", "body": "can you look at the deploy?",
                                  "time": now - 120, "id": "n1", "meta": {}}},
                {"type": "notification", "rule": None, "signal": None, "job": None,
                 "notification": {"source": "macos", "app": "app.zen-browser.zen",
                                  "title": "Download complete", "time": now - 60}},
            ],
            "sources": {"macos": now - 5},
        }
        self.patterns = [
            {"name": "mention", "description": "Slack @mention: two violet pulses", "priority": 60,
             "swatch": "8a5cf6", "repeat": 2},
            {"name": "dm", "description": "Direct message: soft blue breathe", "priority": 50,
             "swatch": "4f8fd8", "repeat": 1},
            {"name": "incident", "description": "Incident: red/amber alternating", "priority": 70,
             "swatch": "e0524a", "repeat": "until_ack"},
            {"name": "meeting_t0", "description": "Meeting starting now", "priority": 100,
             "swatch": "f2a33a", "repeat": "until_ack"},
        ]

    presets: list[dict[str, Any]] | None = [
        {"name": "warm", "description": "Warm white", "swatch": "#ffa757", "rgb": None, "temp": 2700,
         "scene": None, "speed": None, "bri": None},
        {"name": "violet", "description": "Violet", "swatch": "#5a3cdc", "rgb": [90, 60, 220], "temp": None,
         "scene": None, "speed": None, "bri": None},
    ]

    scenes: list[dict[str, Any]] = [
        {"name": "evening", "bulbs": {
            "desk": {"on": True, "bri": 60, "color": "5a3cdc", "swatch": "#5a3cdc"},
            "room": {"on": True, "bri": 35, "temp": 2700, "swatch": "#ffa757"}}},
        {"name": "all off", "bulbs": {"desk": {"on": False, "swatch": "#2b2f36"},
                                      "room": {"on": False, "swatch": "#2b2f36"}}},
    ]

    def make_app(self) -> web.Application:
        @web.middleware
        async def auth(req: web.Request, handler):
            if req.path != "/v1/health" and req.headers.get("Authorization") != f"Bearer {TOKEN}":
                return web.json_response({"error": "unauthorized"}, status=401)
            return await handler(req)

        async def state(_: web.Request) -> web.Response:
            return web.json_response(self.state)

        async def patterns(_: web.Request) -> web.Response:
            return web.json_response(self.patterns)

        async def post(req: web.Request) -> web.Response:
            body = await req.json()
            self.posts.append((req.path, body))
            if req.path == "/v1/ack":
                return web.json_response({"acked": True})
            if req.path == "/v1/signal":
                return web.json_response({"job": "j9"})
            return web.json_response({})

        async def events(req: web.Request) -> web.StreamResponse:
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await resp.prepare(req)
            q: asyncio.Queue[str | None] = asyncio.Queue()
            self.subs.append(q)
            await resp.write(b": keepalive\n\n")
            try:
                while (msg := await q.get()) is not None:
                    await resp.write(msg.encode())
            finally:
                self.subs.remove(q)
            return resp

        app = web.Application(middlewares=[auth])
        app.router.add_get("/v1/state", state)
        app.router.add_get("/v1/patterns", patterns)
        app.router.add_get("/v1/events", events)
        async def health(_: web.Request) -> web.Response:
            return web.json_response({"ok": True, "version": "t"})

        app.router.add_get("/v1/health", health)
        if self.presets is not None:
            async def presets(_: web.Request) -> web.Response:
                return web.json_response(self.presets)
            app.router.add_get("/v1/presets", presets)

            async def scenes(_: web.Request) -> web.Response:
                return web.json_response(self.scenes)

            async def delete(req: web.Request) -> web.Response:
                self.posts.append(("DELETE " + req.path, {}))
                return web.json_response({"deleted": req.match_info["name"]})

            async def bad(req: web.Request) -> web.Response:
                return web.json_response({"error": "bad scene name '!!'"}, status=400)

            app.router.add_get("/v1/scenes", scenes)
            app.router.add_post("/v1/scenes", post)
            app.router.add_post("/v1/scenes/{name}/apply", post)
            app.router.add_delete("/v1/scenes/{name}", delete)
            app.router.add_post("/v1/bad", bad)
        for p in ("notify", "signal", "ack", "mode", "mute", "heartbeat", "light"):
            app.router.add_post(f"/v1/{p}", post)
        return app

    def push(self, etype: str, data: dict[str, Any]) -> None:
        msg = f"event: {etype}\ndata: {json.dumps({'type': etype, **data})}\n\n"
        for q in list(self.subs):
            q.put_nowait(msg)

    def hangup(self) -> None:
        for q in list(self.subs):
            q.put_nowait(None)


async def until(cond, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def text_of(app: BulbTUI, sel: str) -> str:
    return str(app.query_one(sel, Static).render())


def feed_text(app: BulbTUI) -> str:
    return "\n".join(s.text for s in app.query_one("#feed", RichLog).lines)


@pytest.fixture
async def daemon():
    fd = FakeDaemon()
    srv = TestServer(fd.make_app())
    await srv.start_server()
    fd.server = srv
    yield fd
    fd.hangup()
    await fd.server.close()


def make_tui(fd: FakeDaemon, token: str = TOKEN) -> BulbTUI:
    return BulbTUI(DaemonClient(str(fd.server.make_url("")), token),
                   backoff_initial=0.05, backoff_max=0.2)


async def connected(app: BulbTUI, fd: FakeDaemon) -> None:
    await until(lambda: app.status == "connected" and len(fd.subs) == 1)


async def test_initial_state_renders(daemon: FakeDaemon) -> None:
    app = make_tui(daemon)
    async with app.run_test(size=(150, 42)) as pilot:
        await connected(app, daemon)
        await pilot.pause()
        bulbs = text_of(app, "#bulbs")
        assert "desk" in bulbs and "192.168.1.40" in bulbs and "60%" in bulbs and "warm" in bulbs
        meetings = text_of(app, "#meetings")
        assert "Design sync" in meetings and "1:1 with Priya" in meetings
        assert "mention" in text_of(app, "#now") and "dm" in text_of(app, "#now")
        ol = app.query_one("#patterns", OptionList)
        assert [ol.get_option_at_index(i).id for i in range(ol.option_count)] == \
            ["mention", "dm", "incident", "meeting_t0"]
        feed = feed_text(app)
        assert "Slack" in feed and "deploy" in feed and "no rule" in feed
        assert "connected" in text_of(app, "#topbar")

        daemon.push("notification", {
            "notification": {"source": "macos", "app": "com.tinyspeck.slackmacgap",
                             "title": "#incidents", "body": "API p99 latency spiking", "time": time.time()},
            "rule": "incident", "signal": "incident", "job": "j5"})
        daemon.push("mode", {"mode": "on_air", "active": True})
        daemon.push("merged", {"job": "j2", "signal": "dm", "key": "slack:dm", "count": 3})
        daemon.push("signal_start", {"job": "j5", "signal": "incident"})
        await until(lambda: "latency" in feed_text(app))
        await until(lambda: "ON AIR" in text_of(app, "#topbar"))
        await pilot.pause()
        assert "incident" in feed_text(app) and "merged" in feed_text(app)
        SCREENSHOT.parent.mkdir(parents=True, exist_ok=True)
        app.save_screenshot(SCREENSHOT.name, str(SCREENSHOT.parent))
        assert SCREENSHOT.exists()


async def test_filter_feed(daemon: FakeDaemon) -> None:
    app = make_tui(daemon)
    async with app.run_test(size=(150, 42)) as pilot:
        await connected(app, daemon)
        await pilot.press("slash", "z", "e", "n")
        await pilot.pause()
        assert "Download complete" in feed_text(app) and "deploy" not in feed_text(app)
        await pilot.press("escape")
        await pilot.pause()
        assert "deploy" in feed_text(app)


async def test_keys_post(daemon: FakeDaemon) -> None:
    app = make_tui(daemon)
    async with app.run_test(size=(150, 42)) as pilot:
        await connected(app, daemon)
        await pilot.press("space")
        await until(lambda: ("/v1/ack", {}) in daemon.posts)
        await pilot.press("down", "down", "p")
        await until(lambda: ("/v1/signal", {"signal": "incident"}) in daemon.posts)
        await pilot.press("m")
        await until(lambda: ("/v1/mute", {"minutes": 30}) in daemon.posts)
        await pilot.press("o")
        await until(lambda: ("/v1/mode", {"mode": "on_air", "active": True}) in daemon.posts)
        await pilot.press("f")
        await until(lambda: ("/v1/mode", {"mode": "focus", "active": True}) in daemon.posts)


async def test_mute_cycle(daemon: FakeDaemon) -> None:
    app = make_tui(daemon)
    async with app.run_test(size=(150, 42)) as pilot:
        await connected(app, daemon)
        await pilot.press("m")
        daemon.push("mute", {"until": time.time() + 1800})
        await until(lambda: "MUTED" in text_of(app, "#topbar"))
        await pilot.press("m")
        await pilot.press("m")
        await until(lambda: [b["minutes"] for p, b in daemon.posts if p == "/v1/mute"] == [30, 60, 0])


async def test_reconnect(daemon: FakeDaemon) -> None:
    app = make_tui(daemon)
    async with app.run_test(size=(150, 42)):
        await connected(app, daemon)
        port = daemon.server.port
        daemon.hangup()
        await daemon.server.close()
        await until(lambda: app.status == "reconnecting")
        assert "reconnecting" in text_of(app, "#topbar")
        daemon.state["bulbs"]["desk"]["ip"] = "192.168.1.99"
        daemon.server = TestServer(daemon.make_app(), port=port)
        await daemon.server.start_server()
        await connected(app, daemon)
        await until(lambda: "192.168.1.99" in text_of(app, "#bulbs"))
        daemon.push("notification", {"notification": {"app": "Zoom", "title": "back"},
                                     "rule": None, "signal": None, "job": None})
        await until(lambda: "back" in feed_text(app))


async def test_bad_token(daemon: FakeDaemon) -> None:
    app = make_tui(daemon, token="wrong")
    async with app.run_test(size=(150, 42)) as pilot:
        await until(lambda: app.status == "auth")
        await pilot.pause()
        assert "auth failed" in text_of(app, "#topbar")


async def test_garbage_fields_do_not_crash(daemon: FakeDaemon) -> None:
    daemon.state = {"bulbs": {"desk": {"state": {"on": True, "rgb": ["x", 1]}}, "room": None},
                    "meetings": [{"start": "nope"}, None], "queue": None, "playing": "bad",
                    "notifications": [None, {"notification": None}], "muted_until": "soon"}
    daemon.patterns = [{"swatch": "zz"}, "junk"]  # type: ignore[list-item]
    app = make_tui(daemon)
    async with app.run_test(size=(150, 42)) as pilot:
        await connected(app, daemon)
        daemon.push("signal_end", {})
        daemon.push("bulb_error", {"role": "ghost"})
        daemon.push("meeting", {"phase": "t0"})
        daemon.push("weird", {"x": 1})
        await until(lambda: "ghost" in feed_text(app))
        await pilot.pause()
        assert app.is_running


def test_sse_parser_split_chunks() -> None:
    stream = (": keepalive\n\nevent: notification\ndata: {\"a\": 1}\n\n"
              "event: mode\r\ndata: {\"mode\": \"on_air\",\r\ndata: \"active\": true}\r\n\r\n"
              "data: plain\n\n")
    want = [("notification", '{"a": 1}'), ("mode", '{"mode": "on_air",\n"active": true}'),
            ("message", "plain")]
    for size in (1, 2, 3, 5, 7, 11, len(stream)):
        p = SSEParser()
        got = []
        for i in range(0, len(stream), size):
            got += p.feed(stream[i:i + size].encode())
        assert got == want, size


def test_sse_parser_utf8_and_mid_line() -> None:
    p = SSEParser()
    assert p.feed("event: notif") == []
    assert p.feed("ication\ndata: {\"t\": \"caf") == []
    assert p.feed("é\"}\n") == []
    assert p.feed("\n") == [("notification", '{"t": "café"}')]


def test_sse_parser_split_multibyte() -> None:
    raw = "data: {\"t\": \"caf\u00e9 \u2603\"}\n\n".encode()
    p = SSEParser()
    got = []
    for b in raw:
        got += p.feed(bytes([b]))
    assert got == [("message", '{"t": "caf\u00e9 \u2603"}')]


def lights(fd: FakeDaemon) -> list[dict[str, Any]]:
    return [b for p, b in fd.posts if p == "/v1/light"]


async def test_bulbs_panel_names_preset(daemon: FakeDaemon) -> None:
    app = make_tui(daemon)
    async with app.run_test(size=(150, 42)) as pilot:
        await connected(app, daemon)
        await pilot.pause()
        bulbs = text_of(app, "#bulbs")
        assert "violet" in bulbs and "warm" in bulbs


async def test_light_panel(daemon: FakeDaemon) -> None:
    app = make_tui(daemon)
    async with app.run_test(size=(150, 42)) as pilot:
        await connected(app, daemon)
        await pilot.press("l")
        await until(lambda: isinstance(app.screen, LightScreen))
        scr = app.screen
        assert scr.bri == 60
        await pilot.press("down", "down", "down")
        await pilot.pause(0.3)
        assert lights(daemon) == []  # browsing does not apply
        await pilot.press("enter")
        await until(lambda: lights(daemon) == [{"bulbs": "all", "preset": "violet"}])
        await pilot.press("tab")
        assert scr.target == "desk"
        await pilot.press("right", "right", "right")
        await until(lambda: len(lights(daemon)) == 2)
        assert lights(daemon)[-1] == {"bulbs": "desk", "bri": 90}
        await pilot.press("t")
        await until(lambda: lights(daemon)[-1] == {"bulbs": "desk", "on": False})
        await pilot.press("c", "0", "0", "f", "f", "8", "0", "enter")
        await until(lambda: lights(daemon)[-1] == {"bulbs": "desk", "color": "00ff80"})
        await pilot.press("c", "z", "enter")
        await pilot.pause()
        assert lights(daemon)[-1] == {"bulbs": "desk", "color": "00ff80"}
        await pilot.press("escape", "p", "slash")  # main-screen keys are ignored while the panel is open
        await pilot.pause()
        assert isinstance(app.screen, LightScreen)
        await pilot.press("escape")
        await until(lambda: not isinstance(app.screen, LightScreen))


async def test_light_panel_old_daemon(daemon: FakeDaemon) -> None:
    daemon.presets = None
    await daemon.server.close()
    srv = TestServer(daemon.make_app())
    await srv.start_server()
    daemon.server = srv
    app = make_tui(daemon)
    async with app.run_test(size=(150, 42)) as pilot:
        await connected(app, daemon)
        assert app.presets == []
        await pilot.press("l", "t")
        await until(lambda: lights(daemon) == [{"bulbs": "all", "on": False}])


def test_match_preset() -> None:
    presets = FakeDaemon.presets
    assert match_preset({"on": True, "temp": 2720}, presets) == "warm"
    assert match_preset({"on": True, "temp": 3500}, presets) is None
    assert match_preset({"on": True, "rgb": [90, 60, 220]}, presets) == "violet"
    assert match_preset({"on": True, "scene": 5}, [{"name": "fire", "scene": 5, "bri": 60}]) is None


async def test_scenes_in_light_panel(daemon: FakeDaemon) -> None:
    app = make_tui(daemon)
    async with app.run_test(size=(150, 42)) as pilot:
        await connected(app, daemon)
        await pilot.pause()
        assert app.query_one("#bulbs", Static).border_subtitle == "scene: evening"
        await pilot.press("l")
        await until(lambda: isinstance(app.screen, LightScreen))
        scr = app.screen
        assert "scene evening" in str(scr.query_one("#light-head", Static).render())
        await pilot.press("down", "enter")
        await until(lambda: ("/v1/scenes/all off/apply", {}) in daemon.posts)
        await pilot.press("s")
        await pilot.pause()
        assert scr.query_one("#light-name").value == "all off"
        scr.query_one("#light-name").value = ""
        for k in "late night":
            await pilot.press("space" if k == " " else k)
        await pilot.press("enter")
        await until(lambda: ("/v1/scenes", {"name": "late night"}) in daemon.posts)
        await pilot.press("x")
        await pilot.pause()
        assert not [p for p, _ in daemon.posts if p.startswith("DELETE")]  # first x only arms it
        await pilot.press("x")
        await until(lambda: ("DELETE /v1/scenes/all off", {}) in daemon.posts)
        daemon.scenes = daemon.scenes[:1]
        daemon.push("scenes", {"change": "deleted", "name": "all off"})
        daemon.push("light", {"role": "desk", "state": {"on": True, "dimming": 60, "temp": 4000}})
        await until(lambda: len(app.scenes) == 1)
        await until(lambda: [o.id for o in scr.query_one("#light-presets", OptionList).options
                             if o.id and o.id.startswith("scene:")] == ["scene:evening"])
        await pilot.pause()
        assert app.query_one("#bulbs", Static).border_subtitle == ""
        await pilot.press("escape")
        await until(lambda: not isinstance(app.screen, LightScreen))


async def test_daemon_error_message_shown(daemon: FakeDaemon) -> None:
    app = make_tui(daemon)
    notes = []
    app.notify = lambda msg, **kw: notes.append(msg)  # type: ignore[method-assign]
    async with app.run_test(size=(150, 42)):
        await connected(app, daemon)
        app._send("/v1/bad", {}, "ok")
        await until(lambda: notes)
        assert notes == ["bad scene name '!!'"]


def test_scene_matching() -> None:
    assert look_matches({"on": False}, {"on": False, "temp": 2700})
    assert look_matches({"color": "5A3CDC", "bri": 60}, {"on": True, "dimming": 60, "rgb": [90, 60, 220]})
    assert not look_matches({"color": "5a3cdc", "bri": 50}, {"on": True, "dimming": 60, "rgb": [90, 60, 220]})
    assert look_matches({"scene": "fireplace"}, {"on": True, "scene": 5})
    assert look_matches({"temp": 2700}, {"on": True, "temp": 2740})
    state = FakeDaemon().state
    assert match_scene(state, FakeDaemon.scenes) == "evening"
    assert match_scene(state, FakeDaemon.scenes[1:]) is None
