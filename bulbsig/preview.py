"""`bulbsig preview`: play signals on the real bulbs, directly or through the daemon."""
from __future__ import annotations

import argparse
import asyncio
import inspect
import sys
from typing import Any

from . import config
from . import patterns as patterns_mod
from .engine import Engine

ACK_AFTER_S = 6.0
MODE_HOLD_S = 4.0


async def _wait_end(q: asyncio.Queue[dict[str, Any]], job: str) -> str:
    while True:
        ev = await q.get()
        if ev.get("type") == "signal_end" and ev.get("job") == job:
            return ev["reason"]
        if ev.get("type") == "bulb_error":
            print(f"  ! {ev['role']}: {ev['error']}")


async def _close(bulbs: dict[str, Any]) -> None:
    for b in bulbs.values():
        close = getattr(b, "close", None)
        if close is not None:
            r = close()
            if inspect.isawaitable(r):
                await r


async def _direct(cfg: config.Config, ps: patterns_mod.PatternSet, names: list[str], gap: float) -> int:
    from . import wiz  # written separately; imported lazily so --list works without it
    bulbs = await wiz.open_bulbs(cfg)
    if not bulbs:
        print("no bulbs found", file=sys.stderr)
        return 1
    engine = Engine(bulbs, ps)
    q = engine.subscribe()
    runner = asyncio.create_task(engine.run())
    try:
        for i, name in enumerate(names):
            if i:
                await asyncio.sleep(gap)
            if name in ps.modes:
                print(f"mode {name}: on for {MODE_HOLD_S:g} s")
                engine.set_mode(name, True)
                await asyncio.sleep(MODE_HOLD_S)
                engine.set_mode(name, False)
                await engine.wait_idle()
                continue
            p = ps.get(name)
            print(f"{name:16} {p.swatch}  {p.description}")
            job = engine.trigger(name)
            end = asyncio.ensure_future(_wait_end(q, job))
            if p.repeat == "until_ack":
                done, _ = await asyncio.wait({end}, timeout=ACK_AFTER_S)
                if not done:
                    engine.ack(job)
            reason = await end
            await engine.wait_idle()
            print(f"  -> {reason}")
    finally:
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass
        await _close(bulbs)
    return 0


async def _via_daemon(cfg: config.Config, names: list[str], gap: float) -> int:
    import aiohttp
    headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else {}
    rc = 0
    async with aiohttp.ClientSession(headers=headers) as s:
        for i, name in enumerate(names):
            if i:
                await asyncio.sleep(gap)
            try:
                async with s.post(f"{cfg.url.rstrip('/')}/v1/signal", json={"signal": name}) as r:
                    body = await r.text()
                    print(f"{name:16} HTTP {r.status} {body.strip()}")
                    rc |= r.status >= 400
            except aiohttp.ClientError as e:
                print(f"{name:16} failed: {e}", file=sys.stderr)
                return 1
    return int(rc)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bulbsig preview", description="play signals on the bulbs")
    ap.add_argument("signals", nargs="*", help="signal or mode names, or 'all'")
    ap.add_argument("--gap", type=float, default=2.0, help="seconds between signals (default 2)")
    ap.add_argument("--list", action="store_true", help="list the signals and modes")
    ap.add_argument("--via-daemon", action="store_true", help="ask the running daemon to play them")
    a = ap.parse_args(argv)
    cfg = config.load()
    ps = patterns_mod.load(cfg.patterns_file)
    if a.list or not a.signals:
        for p in ps.patterns.values():
            print(f"{p.name:16} {p.swatch}  {p.description}")
        for m in ps.modes.values():
            print(f"{m.name:16} mode     {m.description}")
        return 0 if a.list else 2
    names = ps.names() if a.signals == ["all"] else a.signals
    unknown = [n for n in names if n not in ps.patterns and n not in ps.modes]
    if unknown:
        print(f"unknown signal(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    try:
        if a.via_daemon:
            return asyncio.run(_via_daemon(cfg, names, a.gap))
        return asyncio.run(_direct(cfg, ps, names, a.gap))
    except KeyboardInterrupt:
        return 130
