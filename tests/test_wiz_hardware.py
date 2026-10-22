import asyncio
import contextlib
import statistics
import time
from dataclasses import replace

import pytest

from bulbsig.contracts import LightState
from bulbsig.wiz import WizBulb, matches

pytestmark = pytest.mark.hardware

SUBNETS = ["192.168.1.0/24"]
BULBS = [("desk", "9877d5a438a2", "192.168.1.9"), ("room", "9877d5e910a8", "192.168.1.14")]


def make(ip_override: str | None = None) -> list[WizBulb]:
    return [WizBulb(r, m, ip_override or ip, subnets=SUBNETS) for r, m, ip in BULBS]


async def raw_pilot(b: WizBulb) -> dict:
    r = await b.request("getPilot")
    assert r, f"{b.role} not responding"
    return {k: v for k, v in r["result"].items() if k not in ("mac", "rssi")}


@contextlib.asynccontextmanager
async def preserved():
    bulbs = make()
    before = {b.role: await raw_pilot(b) for b in bulbs}
    print(f"\nbefore: {before}")
    try:
        yield bulbs
    finally:
        oks = []
        for b in bulbs:
            orig = LightState.from_pilot(before[b.role])
            if not orig.on:
                # Off hides the colour; put it back first so the next "on" looks the same.
                await b.set(replace(orig, on=True))
            oks.append(await b.set(orig, attempts=5))
            after = await b.request("getPilot")
            print(f"restored {b.role}: ok={oks[-1]} after={after and after['result']}")
        assert all(oks)


STARTS = {
    "white": LightState(True, 60, temp=4000),
    "rgb": LightState(True, 70, rgb=(0, 80, 255)),
    "scene": LightState(True, 50, scene=5, speed=120),
    "off": LightState(False),
}


async def restore_cycle(b: WizBulb, start: LightState) -> list[str]:
    log = []
    assert await b.set(start), f"{b.role}: start {start}"
    snap = await b.get_state()
    changes = [LightState(True, 40, rgb=(0, 255, 0)) if start.rgb is None else LightState(True, 40, temp=2700)]
    if start.on:
        changes.append(LightState(False))
    for ch in changes:
        assert await b.set(ch), f"{b.role}: change {ch}"
        t = time.perf_counter()
        assert await b.set(snap), f"{b.role}: restore {snap} from {ch}"
        dt = (time.perf_counter() - t) * 1000
        after = await b.get_state()
        assert matches(snap, after)
        if snap.on:
            assert after == snap
        log.append(f"{b.role}: snap={snap} via {'off' if not ch.on else 'other'} -> {after} ({dt:.0f} ms)")
    return log


@pytest.mark.parametrize("start", list(STARTS))
async def test_m1_03_snapshot_restore(start):
    async with preserved() as bulbs:
        logs = await asyncio.gather(*(restore_cycle(b, STARTS[start]) for b in bulbs))
        print("\n".join(line for log in logs for line in log))


async def probe(b: WizBulb, want: LightState, alt: LightState, sent_at: float) -> tuple[str, float]:
    await asyncio.sleep(0.12)
    r = await b._n().request(b.ip, "getPilot", timeout=0.17, tries=1)
    ms = (time.perf_counter() - sent_at) * 1000
    if not r:
        return "no-reply", ms
    got = LightState.from_pilot(r["result"])
    return ("ok" if matches(want, got) else "next" if matches(alt, got) else f"mismatch {got}"), ms


async def test_m1_04_pattern_timing():
    colours = [(255, 0, 0), (0, 0, 255), (0, 255, 0), (255, 255, 0), (255, 0, 255)]
    steps = [LightState(True, 100 if i % 2 else 40, rgb=colours[i % 5]) for i in range(10)]
    late: dict[str, list[float]] = {"desk": [], "room": []}
    async with preserved() as bulbs:
        probes = []
        loop = asyncio.get_running_loop()
        next_t = loop.time() + 0.1
        for i, s in enumerate(steps):
            await asyncio.sleep(max(0.0, next_t - loop.time()))
            for b in bulbs:
                b.send(s)
                late[b.role].append((loop.time() - next_t) * 1000)
            if i in (2, 5, 8):
                alt = steps[min(i + 1, 9)]
                probes += [(i, b.role, asyncio.create_task(probe(b, s, alt, time.perf_counter()))) for b in bulbs]
            next_t += 0.3
        await asyncio.sleep(0.4)
        results = [(i, role, await t) for i, role, t in probes]
    for role, xs in late.items():
        print(f"{role}: send lateness max={max(xs):.2f} ms median={statistics.median(xs):.2f} ms "
              f"steps={[round(x, 2) for x in xs]}")
    for i, role, (res, ms) in results:
        print(f"step {i} {role}: readback {res} at +{ms:.0f} ms after send")
    assert max(late["desk"]) < 100
    assert all(res == "ok" for i, role, (res, ms) in results if role == "desk")


async def test_rediscovery_from_wrong_ip():
    async with preserved():
        bulbs = make("192.168.1.250")
        t = time.perf_counter()
        states = await asyncio.gather(*(b.get_state() for b in bulbs))
        dt = time.perf_counter() - t
        print(f"rediscovered {[(b.role, b.ip) for b in bulbs]} in {dt:.2f} s: {states}")
        assert [b.ip for b in bulbs] == [ip for _, _, ip in BULBS]
