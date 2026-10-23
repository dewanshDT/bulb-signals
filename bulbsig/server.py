"""The daemon: HTTP API over the engine, rules and calendar."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import hmac
import ipaddress
import json
import logging
import signal as _signal
import time
from collections import deque
from typing import Any, Callable

from aiohttp import web

from . import __version__
from .contracts import EngineAPI, Notification

log = logging.getLogger("bulbsig.server")

RECENT_MAX = 100
HB_TIMEOUT_S = 180
HB_REPEAT_S = 300


def split_listen(listen: str) -> tuple[str, int]:
    host, _, port = listen.rpartition(":")
    if not host:
        raise ValueError(f"listen must be HOST:PORT, got {listen!r}")
    return host.strip("[]"), int(port)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Daemon:
    def __init__(self, cfg: Any, engine: EngineAPI, rules: Any, calendar: Any = None,
                 patterns: Any = None, *, clock: Callable[[], float] = time.time,
                 hb_timeout_s: float = HB_TIMEOUT_S, hb_repeat_s: float = HB_REPEAT_S,
                 keepalive_s: float = 15) -> None:
        self.cfg, self.engine, self.rules, self.calendar, self.patterns = cfg, engine, rules, calendar, patterns
        self.clock = clock
        self.hb_timeout_s, self.hb_repeat_s, self.keepalive_s = hb_timeout_s, hb_repeat_s, keepalive_s
        self.recent: deque[dict[str, Any]] = deque(maxlen=RECENT_MAX)
        self.focus = False
        self.focus_suppressed = 0
        self.sources: dict[str, float] = {}
        self.hb_fired: dict[str, float] = {}
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.streams: set[asyncio.Task[Any]] = set()


    def schedule_meeting(self, n: Notification) -> dict[str, Any] | None:
        """A calendar push becomes a meeting the calendar watcher counts down to."""
        from datetime import datetime
        from .calendar_ics import parse_push
        received = datetime.fromtimestamp(n.time).astimezone()
        cal = self.cfg.raw.get("calendar", {})
        start, end = parse_push(f"{n.body}\n{n.subtitle}\n{n.title}", received,
                                default_lead_min=float(cal.get("push_lead_min", 10)),
                                default_len_min=float(cal.get("push_len_min", 30)))
        m = self.calendar.add_adhoc(n.title, start, end)
        info = {"title": n.title, "start": start.isoformat(), "end": end.isoformat(), "new": m is not None}
        log.info("meeting from %s: %s", n.app, info)
        return info
    def publish(self, event: dict[str, Any]) -> None:
        for q in list(self.subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)

    def focus_allowed(self, signal: str) -> bool:
        fa = getattr(self.rules, "focus_allowed", None)
        return bool(fa(signal)) if fa else signal in getattr(self.rules, "focus_allow", ())

    def set_focus(self, active: bool) -> str | None:
        job = None
        was, self.focus = self.focus, active
        if was and not active and self.focus_suppressed > 0:
            n, self.focus_suppressed = self.focus_suppressed, 0
            try:
                job = self.engine.trigger("backlog", key="backlog", origin={"source": "focus", "count": n})
            except KeyError:
                log.error("backlog signal not defined")
        if not active:
            self.focus_suppressed = 0
        self.publish({"type": "mode", "mode": "focus", "active": active})
        return job

    def check_heartbeats(self) -> list[str]:
        now, fired = self.clock(), []
        for src, last in self.sources.items():
            if now - last <= self.hb_timeout_s:
                self.hb_fired.pop(src, None)
                continue
            prev = self.hb_fired.get(src)
            if prev is None or now - prev >= self.hb_repeat_s:
                self.hb_fired[src] = now
                fired.append(src)
                log.warning("heartbeat lost from %s (%.0fs silent)", src, now - last)
                try:
                    self.engine.trigger("heartbeat_lost", key=f"hb:{src}",
                                        origin={"source": "daemon", "lost": src, "last_seen": last})
                except KeyError:
                    log.error("heartbeat_lost signal not defined")
        return fired

    async def heartbeat_loop(self, every_s: float = 5) -> None:
        while True:
            await asyncio.sleep(every_s)
            try:
                self.check_heartbeats()
            except Exception:
                log.exception("heartbeat check failed")


DAEMON = web.AppKey("daemon", Daemon)


async def _body(req: web.Request) -> dict[str, Any]:
    try:
        d = await req.json() if req.can_read_body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid JSON"}), content_type="application/json")
    if not isinstance(d, dict):
        raise web.HTTPBadRequest(text=json.dumps({"error": "expected a JSON object"}), content_type="application/json")
    return d


def _err(status: int, msg: str) -> web.Response:
    return web.json_response({"error": msg}, status=status)


def _routes(d: Daemon) -> web.RouteTableDef:
    r = web.RouteTableDef()

    @r.get("/v1/health")
    async def health(req: web.Request) -> web.Response:
        return web.json_response({"ok": True, "version": __version__})

    @r.post("/v1/notify")
    async def notify(req: web.Request) -> web.Response:
        body = await _body(req)
        try:
            n = Notification.from_json(body)
        except TypeError as e:
            return _err(400, f"bad notification: {e}")
        if not n.time:
            n = dataclasses.replace(n, time=d.clock())
        m = d.rules.match(n)
        out: dict[str, Any] = {"rule": None, "signal": None, "job": None}
        if m is not None:
            out.update(rule=m.rule, signal=m.signal)
            if m.meeting and d.calendar is not None and hasattr(d.calendar, "add_adhoc"):
                out["meeting"] = d.schedule_meeting(n)
            elif d.focus and not d.focus_allowed(m.signal):
                d.focus_suppressed += 1
                out["suppressed"] = "focus"
            else:
                try:
                    out["job"] = d.engine.trigger(m.signal, key=m.key, priority=m.priority, origin=n.to_json())
                except KeyError:
                    log.error("rule %s names unknown signal %r", m.rule, m.signal)
                    out["error"] = f"unknown signal {m.signal!r}"
        rec = {"notification": n.to_json(), **out, "received": d.clock()}
        d.recent.append(rec)
        d.publish({"type": "notification", **{k: v for k, v in rec.items() if k != "received"}})
        return web.json_response(out)

    @r.post("/v1/signal")
    async def signal(req: web.Request) -> web.Response:
        body = await _body(req)
        name = body.get("signal")
        if not isinstance(name, str) or not name:
            return _err(400, "signal required")
        try:
            job = d.engine.trigger(name, key=body.get("key"), priority=body.get("priority"),
                                   origin={"source": "api"})
        except KeyError:
            return _err(404, f"unknown signal {name!r}")
        return web.json_response({"job": job})

    @r.post("/v1/ack")
    async def ack(req: web.Request) -> web.Response:
        body = await _body(req)
        return web.json_response({"acked": bool(d.engine.ack(body.get("job")))})

    @r.post("/v1/mode")
    async def mode(req: web.Request) -> web.Response:
        body = await _body(req)
        name, active = body.get("mode"), body.get("active", True)
        if not isinstance(name, str) or not name:
            return _err(400, "mode required")
        if name == "focus":
            job = d.set_focus(bool(active))
            return web.json_response({"backlog": job} if job else {})
        d.engine.set_mode(name, bool(active))
        if d.calendar is not None and hasattr(d.calendar, "mode_changed"):
            d.calendar.mode_changed(name, bool(active))
        return web.json_response({})

    @r.post("/v1/mute")
    async def mute(req: web.Request) -> web.Response:
        body = await _body(req)
        try:
            minutes = float(body.get("minutes", 0) or 0)
        except (TypeError, ValueError):
            return _err(400, "minutes must be a number")
        d.engine.mute(minutes * 60 if minutes > 0 else None)
        return web.json_response({})

    @r.post("/v1/heartbeat")
    async def heartbeat(req: web.Request) -> web.Response:
        body = await _body(req)
        src = body.get("source")
        if not isinstance(src, str) or not src:
            return _err(400, "source required")
        d.sources[src] = d.clock()
        d.hb_fired.pop(src, None)
        return web.json_response({})

    @r.get("/v1/state")
    async def state(req: web.Request) -> web.Response:
        snap = dict(d.engine.snapshot())
        snap["meetings"] = d.calendar.upcoming() if d.calendar is not None else []
        snap["notifications"] = list(d.recent)
        snap["focus"] = d.focus
        snap["focus_suppressed"] = d.focus_suppressed
        snap["sources"] = dict(d.sources)
        return web.json_response(snap)

    @r.get("/v1/patterns")
    async def patterns(req: web.Request) -> web.Response:
        out = []
        if d.patterns is not None:
            for name in d.patterns.names():
                p = d.patterns.get(name)
                sw = getattr(p, "swatch", None)
                out.append({"name": p.name, "description": getattr(p, "description", ""),
                            "priority": getattr(p, "priority", None),
                            "swatch": list(sw) if isinstance(sw, tuple) else sw,
                            "repeat": getattr(p, "repeat", None)})
        return web.json_response(out)

    @r.get("/v1/events")
    async def events(req: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                           "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        await resp.prepare(req)
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        eq = d.engine.subscribe()

        async def pump() -> None:
            while True:
                ev = await eq.get()
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(ev)

        d.subscribers.add(q)
        pumper = asyncio.create_task(pump())
        me = asyncio.current_task()
        if me:
            d.streams.add(me)
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), d.keepalive_s)
                except TimeoutError:
                    await resp.write(b": keepalive\n\n")
                    continue
                data = json.dumps(ev, default=str)
                await resp.write(f"event: {ev.get('type', 'message')}\ndata: {data}\n\n".encode())
        except ConnectionResetError:
            pass
        finally:
            pumper.cancel()
            d.subscribers.discard(q)
            d.engine.unsubscribe(eq)
            if me:
                d.streams.discard(me)
        return resp

    return r


@web.middleware
async def _auth(req: web.Request, handler: Callable[[web.Request], Any]) -> web.StreamResponse:
    token = req.app[DAEMON].cfg.token
    if token and req.path != "/v1/health":
        got = req.headers.get("Authorization", "")
        if not hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
            return _err(401, "unauthorized")
    return await handler(req)


def build_app(cfg: Any, engine: EngineAPI, rules: Any, calendar: Any = None, patterns: Any = None,
              *, clock: Callable[[], float] = time.time, hb_timeout_s: float = HB_TIMEOUT_S,
              hb_repeat_s: float = HB_REPEAT_S, hb_check_s: float = 5,
              keepalive_s: float = 15) -> web.Application:
    host, _ = split_listen(cfg.listen)
    if not cfg.token:
        if not is_loopback(host):
            raise ValueError(f"refusing to listen on non-loopback {cfg.listen} without a token")
        log.warning("no token configured; API is unauthenticated (loopback only)")
    d = Daemon(cfg, engine, rules, calendar, patterns, clock=clock, hb_timeout_s=hb_timeout_s,
               hb_repeat_s=hb_repeat_s, keepalive_s=keepalive_s)
    if calendar is not None and getattr(calendar, "publish", False) is None:
        calendar.publish = d.publish
    app = web.Application(middlewares=[_auth])
    app[DAEMON] = d
    app.add_routes(_routes(d))

    async def heartbeats(app: web.Application):  # type: ignore[no-untyped-def]
        t = asyncio.create_task(d.heartbeat_loop(hb_check_s))
        yield
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t

    async def close_streams(app: web.Application) -> None:
        # open SSE handlers would otherwise hold shutdown until its timeout
        for t in list(d.streams):
            t.cancel()

    app.cleanup_ctx.append(heartbeats)
    app.on_shutdown.append(close_streams)
    return app


async def serve(cfg: Any) -> None:
    from . import engine as engine_mod, patterns as patterns_mod, wiz
    from .calendar_ics import CalendarWatcher
    from .rules import Rules, config_handles, config_vip

    bulbs = await wiz.open_bulbs(cfg)
    pats = patterns_mod.load(cfg.patterns_file)
    engine = engine_mod.Engine(bulbs, pats, quiet=(cfg.quiet_start, cfg.quiet_end, cfg.quiet_max_bri))
    rules = Rules.load(cfg.rules_file, me=config_handles(cfg), vip=config_vip(cfg))
    # always on: meetings also arrive as notifications, even without an ICS feed
    until_ack = bool(cfg.raw.get("calendar", {}).get("on_air_until_ack", False))
    calendar = CalendarWatcher(
        cfg.ics_urls,
        trigger=lambda s, k: engine.trigger(s, key=k, origin={"source": "calendar"}),
        set_mode=engine.set_mode, poll_s=cfg.calendar_poll_s, on_air_until_ack=until_ack)
    app = build_app(cfg, engine, rules, calendar, pats)
    host, port = split_listen(cfg.listen)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("bulbsig daemon on http://%s:%d (%d bulbs, %d calendar feeds)", host, port, len(bulbs), len(cfg.ics_urls))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (_signal.SIGINT, _signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    eng_task = asyncio.create_task(engine.run(), name="engine")
    cal_task = asyncio.create_task(calendar.run(), name="calendar") if calendar else None
    stopper = asyncio.create_task(stop.wait())
    waiting = {stopper, eng_task} | ({cal_task} if cal_task else set())
    done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
    for t in done - {stopper}:
        if not t.cancelled() and t.exception():
            log.error("%s task died", t.get_name(), exc_info=t.exception())
    log.info("shutting down")
    stopper.cancel()
    if cal_task:
        cal_task.cancel()
        await asyncio.gather(cal_task, return_exceptions=True)
    await runner.cleanup()
    eng_task.cancel()  # engine restores the bulbs on cancel
    await asyncio.gather(eng_task, return_exceptions=True)
    for sig in (_signal.SIGINT, _signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.remove_signal_handler(sig)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="bulbsig daemon")
    ap.add_argument("--listen", help="HOST:PORT (default from config, 127.0.0.1:8766)")
    ap.add_argument("--config", help="config file (default ~/.config/bulbsig/config.toml)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from . import config
    cfg = config.load(a.config)
    if a.listen:
        cfg.listen = a.listen
    try:
        host, _ = split_listen(cfg.listen)
    except ValueError as e:
        log.error("%s", e)
        return 2
    if not cfg.token and not is_loopback(host):
        log.error("refusing to listen on %s without a token (set [daemon] token or BULBSIG_TOKEN)", cfg.listen)
        return 2
    try:
        asyncio.run(serve(cfg))
    except KeyboardInterrupt:
        pass
    return 0
