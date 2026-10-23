import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bulbsig.config import PKG_DIR, Config
from bulbsig.rules import Rules
from bulbsig.server import DAEMON, build_app, is_loopback, split_listen

TOKEN = "s3cret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SLACK = "com.tinyspeck.slackmacgap"
KNOWN = {"dm", "mention", "vip", "backlog", "ringing", "incident", "success", "heartbeat_lost",
         "test", "meeting_t5", "meeting_t0"}


class StubEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.queues: list[asyncio.Queue] = []
        self.n = 0

    def trigger(self, signal, *, key=None, priority=None, origin=None):
        if signal not in KNOWN:
            raise KeyError(signal)
        self.n += 1
        self.calls.append(("trigger", (signal, key, priority, origin)))
        return f"j{self.n}"

    def triggered(self) -> list[str]:
        return [a[0] for c, a in self.calls if c == "trigger"]

    def ack(self, job_id=None):
        self.calls.append(("ack", job_id))
        return job_id != "nope"

    def set_mode(self, mode, active):
        self.calls.append(("mode", (mode, active)))

    def mute(self, seconds):
        self.calls.append(("mute", seconds))

    def snapshot(self):
        return {"playing": None, "queue": [], "modes": [], "muted_until": None, "quiet": False, "bulbs": {}}

    def subscribe(self):
        q: asyncio.Queue = asyncio.Queue()
        self.queues.append(q)
        return q

    def unsubscribe(self, q):
        self.queues.remove(q)

    async def run(self):
        await asyncio.Event().wait()


@dataclass
class P:
    name: str
    description: str = ""
    priority: int = 30
    swatch: tuple = (255, 0, 0)
    repeat: bool = False


@dataclass
class Pats:
    items: dict = field(default_factory=lambda: {"dm": P("dm", "direct message", 50), "test": P("test")})

    def names(self):
        return list(self.items)

    def get(self, name):
        return self.items[name]


class StubCal:
    publish = None

    def __init__(self):
        self.modes = []

    def upcoming(self, hours=24):
        return [{"title": "Standup", "start": "x", "end": "y", "phase": "upcoming", "uid": "u"}]

    def mode_changed(self, mode, active):
        self.modes.append((mode, active))

    def add_adhoc(self, title, start, end):
        self.adhoc = getattr(self, "adhoc", []) + [(title, start, end)]
        return object() if len(self.adhoc) == 1 else None


class FakeClock:
    t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def rules() -> Rules:
    return Rules.load(PKG_DIR / "rules_default.toml")


def cfg(token: str = TOKEN, listen: str = "127.0.0.1:8766") -> Config:
    return Config(path=Path("/nonexistent"), token=token, listen=listen)


@pytest.fixture
async def env(rules):
    engine, cal, clock = StubEngine(), StubCal(), FakeClock()
    app = build_app(cfg(), engine, rules, cal, Pats(), clock=clock, keepalive_s=0.2, hb_check_s=0.05)
    client = TestClient(TestServer(app))
    await client.start_server()
    yield client, engine, cal, clock, app[DAEMON]
    await client.close()


async def post(client, path, body=None, headers=AUTH):
    r = await client.post(path, json=body or {}, headers=headers)
    return r.status, await r.json()


async def test_auth(env):
    client, *_ = env
    r = await client.get("/v1/health")
    assert r.status == 200 and (await r.json())["ok"] is True
    assert (await client.get("/v1/state")).status == 401
    assert (await client.get("/v1/state", headers={"Authorization": "Bearer wrong"})).status == 401
    assert (await client.post("/v1/notify", json={})).status == 401
    assert (await client.get("/v1/state", headers=AUTH)).status == 200


async def test_no_token_rules(rules):
    build_app(cfg(token=""), StubEngine(), rules)
    build_app(cfg(token="", listen="[::1]:9"), StubEngine(), rules)
    with pytest.raises(ValueError):
        build_app(cfg(token="", listen="0.0.0.0:8766"), StubEngine(), rules)
    build_app(cfg(listen="0.0.0.0:8766"), StubEngine(), rules)
    assert split_listen("[::1]:80") == ("::1", 80) and is_loopback("localhost") and not is_loopback("10.0.0.1")


async def test_notify_matches_and_records(env):
    client, engine, _, _, d = env
    st, out = await post(client, "/v1/notify", {"source": "mac", "app": SLACK, "title": "acme", "subtitle": "Frank", "body": "hi"})
    assert st == 200 and out == {"rule": "slack-dm", "signal": "dm", "job": "j1"}
    sig, key, prio, origin = engine.calls[0][1]
    assert (sig, key, prio, origin["subtitle"]) == ("dm", "dm:Frank", None, "Frank")
    st, out = await post(client, "/v1/notify", {"source": "mac", "app": SLACK, "title": "acme", "subtitle": "#random", "body": "lol"})
    assert out == {"rule": None, "signal": None, "job": None}
    assert len(d.recent) == 2 and d.recent[-1]["notification"]["subtitle"] == "#random"
    assert (await post(client, "/v1/notify", ["x"]))[0] == 400


async def test_notify_unknown_signal(env, rules):
    client, engine, *_ = env
    next(r for r in rules.rules if r.name == "call").signal = "nope"
    st, out = await post(client, "/v1/notify", {"source": "mac", "app": SLACK, "title": "acme", "subtitle": "A", "body": "huddle"})
    assert st == 200 and out["signal"] == "nope" and out["job"] is None and "unknown signal" in out["error"]


async def test_ring_buffer(env):
    client, _, _, _, d = env
    for i in range(105):
        await post(client, "/v1/notify", {"source": "s", "app": "x", "title": str(i)})
    st = await (await client.get("/v1/state", headers=AUTH)).json()
    assert len(st["notifications"]) == 100 and st["notifications"][-1]["notification"]["title"] == "104"


async def test_focus_suppression_and_backlog(env):
    client, engine, _, _, d = env
    assert await post(client, "/v1/mode", {"mode": "focus", "active": True}) == (200, {})
    _, out = await post(client, "/v1/notify", {"source": "m", "app": SLACK, "title": "acme", "subtitle": "Frank", "body": "hi"})
    assert out["suppressed"] == "focus" and out["job"] is None
    await post(client, "/v1/notify", {"source": "m", "app": SLACK, "title": "acme", "subtitle": "Gus", "body": "yo"})
    _, out = await post(client, "/v1/notify", {"source": "m", "app": SLACK, "title": "acme", "subtitle": "#incident", "body": "db"})
    assert out["job"]
    assert engine.triggered() == ["incident"]
    st = await (await client.get("/v1/state", headers=AUTH)).json()
    assert st["focus"] is True and st["focus_suppressed"] == 2
    st, out = await post(client, "/v1/mode", {"mode": "focus", "active": False})
    assert out == {"backlog": "j2"}
    assert engine.triggered() == ["incident", "backlog"]
    assert engine.calls[-1][1][3]["count"] == 2
    await post(client, "/v1/mode", {"mode": "focus", "active": True})
    await post(client, "/v1/mode", {"mode": "focus", "active": False})
    assert engine.triggered() == ["incident", "backlog"]
    assert not any(c == "mode" for c, _ in engine.calls)


async def test_signal_ack_mode_mute(env):
    client, engine, cal, *_ = env
    assert await post(client, "/v1/signal", {"signal": "test", "key": "k"}) == (200, {"job": "j1"})
    assert engine.calls[-1][1][:2] == ("test", "k")
    st, out = await post(client, "/v1/signal", {"signal": "zzz"})
    assert st == 404 and "error" in out
    assert (await post(client, "/v1/signal", {}))[0] == 400
    assert await post(client, "/v1/ack", {"job": "j1"}) == (200, {"acked": True})
    assert await post(client, "/v1/ack", {"job": "nope"}) == (200, {"acked": False})
    assert await post(client, "/v1/ack") == (200, {"acked": True})
    assert engine.calls[-1] == ("ack", None)
    assert await post(client, "/v1/mode", {"mode": "on_air", "active": True}) == (200, {})
    assert engine.calls[-1] == ("mode", ("on_air", True))
    await post(client, "/v1/mode", {"mode": "on_air", "active": False})
    assert cal.modes == [("on_air", True), ("on_air", False)]
    assert await post(client, "/v1/mute", {"minutes": 15}) == (200, {})
    assert engine.calls[-1] == ("mute", 900)
    await post(client, "/v1/mute", {"minutes": 0})
    assert engine.calls[-1] == ("mute", None)
    assert (await post(client, "/v1/mute", {"minutes": "x"}))[0] == 400


async def test_state_shape(env):
    client, *_ = env
    await post(client, "/v1/heartbeat", {"source": "sensor-mac"})
    st = await (await client.get("/v1/state", headers=AUTH)).json()
    assert {"playing", "queue", "modes", "muted_until", "quiet", "bulbs",
            "meetings", "notifications", "focus", "sources"} <= set(st)
    assert st["meetings"][0]["title"] == "Standup"
    assert st["sources"] == {"sensor-mac": 1000.0}


async def test_patterns(env):
    client, *_ = env
    r = await client.get("/v1/patterns", headers=AUTH)
    assert await r.json() == [
        {"name": "dm", "description": "direct message", "priority": 50, "swatch": [255, 0, 0], "repeat": False},
        {"name": "test", "description": "", "priority": 30, "swatch": [255, 0, 0], "repeat": False},
    ]


async def read_events(resp, n: int, timeout: float = 3) -> list[tuple[str, Any]]:
    out, buf = [], b""
    async with asyncio.timeout(timeout):
        while len(out) < n:
            buf += await resp.content.readany()
            while b"\n\n" in buf:
                chunk, buf = buf.split(b"\n\n", 1)
                text = chunk.decode()
                if text.startswith(":"):
                    out.append(("comment", text))
                    continue
                lines = dict(l.split(": ", 1) for l in text.splitlines())
                out.append((lines["event"], json.loads(lines["data"])))
    return out


async def test_sse(env):
    client, engine, cal, _, d = env
    resp = await client.get("/v1/events", headers=AUTH)
    assert resp.status == 200 and resp.headers["Content-Type"].startswith("text/event-stream")
    for _ in range(50):
        if engine.queues:
            break
        await asyncio.sleep(0.01)
    engine.queues[0].put_nowait({"type": "signal_start", "job": "j9", "signal": "dm"})
    await post(client, "/v1/notify", {"source": "m", "app": SLACK, "title": "acme", "subtitle": "Frank", "body": "hi"})
    cal.publish({"type": "meeting", "phase": "t5", "title": "Standup"})
    evs = await read_events(resp, 4)
    kinds = [k for k, _ in evs if k != "comment"]
    assert kinds[:3] == ["signal_start", "notification", "meeting"]
    note = next(v for k, v in evs if k == "notification")
    assert note["rule"] == "slack-dm" and note["job"] == "j1" and note["notification"]["subtitle"] == "Frank"
    assert ("comment", ": keepalive") in await read_events(resp, 1)
    resp.close()
    for _ in range(100):
        if not engine.queues and not d.subscribers:
            break
        await asyncio.sleep(0.02)
    assert engine.queues == [] and d.subscribers == set()


async def test_heartbeat_lost(env):
    client, engine, _, clock, d = env
    await post(client, "/v1/heartbeat", {"source": "sensor"})
    clock.t += 179
    assert d.check_heartbeats() == []
    clock.t += 2
    assert d.check_heartbeats() == ["sensor"]
    assert engine.calls[-1][1][:2] == ("heartbeat_lost", "hb:sensor")
    clock.t += 200
    assert d.check_heartbeats() == []
    clock.t += 101
    assert d.check_heartbeats() == ["sensor"]
    await post(client, "/v1/heartbeat", {"source": "sensor"})
    clock.t += 10
    assert d.check_heartbeats() == []
    assert (await post(client, "/v1/heartbeat", {}))[0] == 400


async def test_heartbeat_background_task(env):
    client, engine, _, clock, _ = env
    await post(client, "/v1/heartbeat", {"source": "sensor"})
    clock.t += 500
    for _ in range(100):
        if "heartbeat_lost" in engine.triggered():
            break
        await asyncio.sleep(0.02)
    assert engine.triggered() == ["heartbeat_lost"]


async def test_serve_lifecycle(monkeypatch, tmp_path):
    import os
    import signal
    import socket
    import sys
    import types

    import aiohttp

    import bulbsig
    from bulbsig import server

    engine = StubEngine()
    restored = asyncio.Event()

    async def run():
        try:
            await asyncio.Event().wait()
        finally:
            restored.set()

    engine.run = run

    async def open_bulbs(cfg):
        return {}

    fakes = {"wiz": types.SimpleNamespace(open_bulbs=open_bulbs),
             "patterns": types.SimpleNamespace(load=lambda p: Pats()),
             "engine": types.SimpleNamespace(Engine=lambda bulbs, pats, quiet: engine)}
    for name, mod in fakes.items():
        monkeypatch.setitem(sys.modules, f"bulbsig.{name}", mod)
        monkeypatch.setattr(bulbsig, name, mod, raising=False)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    ics = tmp_path / "c.ics"
    ics.write_text("BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:x\nEND:VCALENDAR\n")
    c = cfg(listen=f"127.0.0.1:{port}")
    c.ics_urls = [str(ics)]
    task = asyncio.create_task(server.serve(c))
    async with aiohttp.ClientSession() as http:
        for _ in range(100):
            try:
                async with http.get(f"http://127.0.0.1:{port}/v1/health") as r:
                    assert r.status == 200
                    break
            except aiohttp.ClientConnectionError:
                await asyncio.sleep(0.02)
        sse = await http.get(f"http://127.0.0.1:{port}/v1/events", headers=AUTH)
        async with http.get(f"http://127.0.0.1:{port}/v1/state", headers=AUTH) as r:
            assert (await r.json())["meetings"] == []
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, 5)
        sse.close()
    assert restored.is_set()


async def test_calendar_push_schedules_meeting(env):
    client, engine, cal, _, _ = env
    note = {"source": "mac", "app": "app.zen-browser.zen", "title": "Daily Sync",
            "subtitle": "via calendar.google.com", "body": "10:30 – 10:45am", "time": 1792650000}
    st, out = await post(client, "/v1/notify", note)
    assert st == 200 and out["rule"] == "calendar-web" and out["job"] is None
    assert out["meeting"]["new"] is True
    title, start, end = cal.adhoc[0]
    assert title == "Daily Sync" and (start.hour, start.minute, end.hour, end.minute) == (10, 30, 10, 45)
    _, again = await post(client, "/v1/notify", note)
    assert again["meeting"]["new"] is False
    assert not [c for c in engine.calls if c[0] == "trigger"]
