import asyncio
import json
from datetime import datetime

import pytest

from bulbsig import patterns
from bulbsig.config import PKG_DIR
from bulbsig.contracts import ENGINE_SNAPSHOT_KEYS, LightState
from bulbsig.engine import Engine
from bulbsig.testing import FakeBulb

BASE_DESK = LightState(on=True, dimming=80, temp=2700)
BASE_ROOM = LightState(on=True, dimming=50, temp=3000)
NOON = datetime(2026, 10, 22, 12, 0).timestamp()
NIGHT = datetime(2026, 10, 22, 23, 30).timestamp()

TOML = """
[pattern.low]
priority = 10
steps = [{ bulbs = "desk", color = "0000ff", bri = 50, ms = 300 }, { ms = 3000 }]
[pattern.mid]
priority = 50
steps = [{ bulbs = "all", color = "00ff00", bri = 100, ms = 300 }]
[pattern.hi]
priority = 90
steps = [{ bulbs = "room", color = "ff0000", bri = 100, ms = 300 }]
[pattern.rep]
priority = 50
repeat = "until_ack"
max_s = 60
steps = [{ bulbs = "all", color = "ff00ff", bri = 100, ms = 300 }, { bulbs = "all", base = true, ms = 300 }]
[pattern.short]
priority = 50
repeat = "until_ack"
max_s = 2
steps = [{ bulbs = "desk", color = "ffffff", bri = 100, ms = 300 }]
[pattern.skipq]
quiet = "skip"
steps = [{ bulbs = "desk", color = "ffffff", ms = 300 }]
[pattern.onceq]
quiet = "once"
repeat = 3
steps = [{ bulbs = "desk", color = "ffffff", bri = 100, ms = 300 }]
[mode.on_air]
room = { color = "ff0000", bri = 10 }
"""


class Clock:
    def __init__(self, t=NOON):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def bulbs():
    return {"desk": FakeBulb("desk", "aa", BASE_DESK, ip="10.0.0.2"),
            "room": FakeBulb("room", "bb", BASE_ROOM, ip="10.0.0.3")}


def make(bulbs, clock, ps=None, **kw):
    kw.setdefault("time_scale", 0.001)
    return Engine(bulbs, ps or patterns.loads(TOML), clock=clock, **kw)


@pytest.fixture
async def eng(bulbs, clock):
    e = make(bulbs, clock)
    q = e.subscribe()
    task = asyncio.create_task(e.run())
    yield e, q
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def until(q, pred, timeout=3.0):
    seen = []

    async def loop():
        while True:
            ev = await q.get()
            seen.append(ev)
            if pred(ev):
                return ev
    try:
        return await asyncio.wait_for(loop(), timeout)
    except TimeoutError:
        raise AssertionError(f"event not seen; got {seen}") from None


def ended(job=None, signal=None):
    return lambda ev: ev["type"] == "signal_end" and (job is None or ev["job"] == job) \
        and (signal is None or ev["signal"] == signal)


async def test_plays_and_restores_only_touched(eng, bulbs):
    e, q = eng
    job = e.trigger("low")
    ev = await until(q, ended(job))
    assert ev["reason"] == "done"
    await e.wait_idle()
    assert bulbs["desk"].sends[0][1].rgb == (0, 0, 255)
    assert bulbs["desk"].sets[-1][1] == BASE_DESK
    assert bulbs["room"].sends == [] and bulbs["room"].sets == []


async def test_unknown_signal_keyerror(eng):
    e, _ = eng
    with pytest.raises(KeyError):
        e.trigger("nope")
    with pytest.raises(KeyError):
        e.set_mode("nope", True)


async def test_preempt_drops_one_shot_and_restores_first(eng, bulbs):
    e, q = eng
    low = e.trigger("low")
    await until(q, lambda ev: ev["type"] == "signal_start" and ev["job"] == low)
    while not bulbs["desk"].sends:
        await asyncio.sleep(0)
    hi = e.trigger("hi")
    ev = await until(q, ended(low))
    assert ev["reason"] == "preempted"
    await until(q, ended(hi))
    await e.wait_idle()
    kinds = [(k, s) for k, _, s in bulbs["desk"].calls if k in ("send", "set")]
    assert kinds[0][0] == "send" and kinds[1] == ("set", BASE_DESK)
    assert all(j["signal"] != "low" for j in e.snapshot()["queue"])
    assert bulbs["room"].sets[-1][1] == BASE_ROOM


async def test_lower_priority_waits(eng):
    e, q = eng
    mid = e.trigger("mid")
    low = e.trigger("low")
    order = []
    while len(order) < 2:
        ev = await until(q, lambda ev: ev["type"] in ("signal_start", "signal_end"))
        if ev["type"] == "signal_end":
            assert ev["reason"] == "done"
            order.append(ev["job"])
    assert order == [mid, low]


async def test_until_ack_requeued_after_preempt_then_ack(eng):
    e, q = eng
    rep = e.trigger("rep")
    await until(q, lambda ev: ev["type"] == "signal_start" and ev["job"] == rep)
    e.trigger("hi")
    assert (await until(q, ended(rep)))["reason"] == "preempted"
    await until(q, lambda ev: ev["type"] == "queued" and ev["job"] == rep)
    await until(q, ended(signal="hi"))
    await until(q, lambda ev: ev["type"] == "signal_start" and ev["job"] == rep)
    assert e.snapshot()["playing"]["job"] == rep
    assert e.ack() is True
    assert (await until(q, ended(rep)))["reason"] == "ack"
    assert e.ack() is False


async def test_ack_specific_queued_job(eng):
    e, q = eng
    e.trigger("low")
    mid = e.trigger("mid", priority=5)
    assert e.ack(mid) is True
    assert (await until(q, ended(mid)))["reason"] == "ack"


async def test_until_ack_timeout(eng):
    e, q = eng
    job = e.trigger("short")
    assert (await until(q, ended(job)))["reason"] == "timeout"


async def test_priority_override_preempts(eng):
    e, q = eng
    low = e.trigger("low")
    await until(q, lambda ev: ev["type"] == "signal_start")
    e.trigger("low", priority="vip")
    assert (await until(q, ended(low)))["reason"] == "preempted"


async def test_merging(eng, clock):
    e, q = eng
    a = e.trigger("low", key="slack:C1")
    b = e.trigger("low", key="slack:C1")
    assert a == b
    ev = await until(q, lambda ev: ev["type"] == "merged")
    assert ev["count"] == 2 and ev["key"] == "slack:C1"
    await until(q, ended(a))
    await e.wait_idle()
    clock.t += 10
    assert e.trigger("low", key="slack:C1") == a
    assert (await until(q, lambda ev: ev["type"] == "merged"))["count"] == 3
    clock.t += 30
    c = e.trigger("low", key="slack:C1")
    assert c != a
    await until(q, ended(c))


async def test_quiet_hours_clamp_and_single_pass(bulbs):
    clock = Clock(NIGHT)
    e = make(bulbs, clock, quiet=("23:00", "08:00", 10))
    q = e.subscribe()
    task = asyncio.create_task(e.run())
    try:
        assert e.snapshot()["quiet"] is True
        job = e.trigger("rep")
        assert (await until(q, ended(job)))["reason"] == "done"
        colour = [s for _, s in bulbs["desk"].sends if s.rgb == (255, 0, 255)]
        assert len(colour) == 1 and colour[0].dimming == 10
        skip = e.trigger("skipq")
        assert (await until(q, ended(skip)))["reason"] == "muted"
        bulbs["desk"].reset()
        once = e.trigger("onceq")
        await until(q, ended(once))
        assert [s.dimming for _, s in bulbs["desk"].sends] == [100]
    finally:
        task.cancel()


async def test_quiet_window_wraps_midnight(bulbs):
    clock = Clock(datetime(2026, 10, 22, 7, 59).timestamp())
    e = make(bulbs, clock, quiet=("23:00", "08:00", 10))
    assert e.snapshot()["quiet"] is True
    clock.t = datetime(2026, 10, 22, 8, 0).timestamp()
    assert e.snapshot()["quiet"] is False


async def test_mute(eng, bulbs, clock):
    e, q = eng
    rep = e.trigger("rep")
    await until(q, lambda ev: ev["type"] == "signal_start")
    e.mute(60)
    assert (await until(q, lambda ev: ev["type"] == "mute"))["until"] == clock.t + 60
    assert (await until(q, ended(rep)))["reason"] == "muted"
    await e.wait_idle()
    bulbs["desk"].reset()
    j = e.trigger("low")
    assert (await until(q, ended(j)))["reason"] == "muted"
    assert bulbs["desk"].sends == []
    assert e.snapshot()["muted_until"] == clock.t + 60
    e.mute(None)
    assert (await until(q, lambda ev: ev["type"] == "mute"))["until"] is None
    j = e.trigger("low")
    assert (await until(q, ended(j)))["reason"] == "done"


async def test_mode_overlay_and_restore_to_mode(eng, bulbs):
    e, q = eng
    on_air = LightState(on=True, dimming=10, rgb=(255, 0, 0))
    e.set_mode("on_air", True)
    assert (await until(q, lambda ev: ev["type"] == "mode"))["active"] is True
    await e.wait_idle()
    assert bulbs["room"].sets[-1][1] == on_air
    assert bulbs["desk"].sets == []
    j = e.trigger("mid")
    await until(q, ended(j))
    await e.wait_idle()
    assert bulbs["room"].sets[-1][1] == on_air
    assert bulbs["desk"].sets[-1][1] == BASE_DESK
    assert e.snapshot()["modes"] == ["on_air"]
    e.set_mode("on_air", False)
    await e.wait_idle()
    assert bulbs["room"].sets[-1][1] == BASE_ROOM
    assert e.snapshot()["modes"] == []


async def test_mode_change_during_signal_applies_after(eng, bulbs):
    e, q = eng
    j = e.trigger("low")
    await until(q, lambda ev: ev["type"] == "signal_start")
    e.set_mode("on_air", True)
    await until(q, ended(j))
    await e.wait_idle()
    assert bulbs["room"].sets[-1][1].rgb == (255, 0, 0)


async def test_bulb_error_does_not_stop_others(eng, bulbs):
    e, q = eng
    bulbs["room"].fail = True
    j = e.trigger("mid")
    ev = await until(q, lambda ev: ev["type"] == "bulb_error")
    assert ev["role"] == "room"
    assert (await until(q, ended(j)))["reason"] == "done"
    await e.wait_idle()
    assert bulbs["desk"].sets[-1][1] == BASE_DESK
    snap = e.snapshot()
    assert snap["bulbs"]["room"]["reachable"] is False
    assert snap["bulbs"]["desk"]["reachable"] is True


async def test_base_refreshed_only_after_idle(eng, bulbs, clock):
    e, q = eng
    await until(q, ended(e.trigger("low")))
    await e.wait_idle()
    manual = LightState(on=True, dimming=30, temp=4000)
    bulbs["desk"].state = manual
    await until(q, ended(e.trigger("low")))
    await e.wait_idle()
    assert bulbs["desk"].sets[-1][1] == BASE_DESK
    bulbs["desk"].state = manual
    clock.t += 60
    await until(q, ended(e.trigger("low")))
    await e.wait_idle()
    assert bulbs["desk"].sets[-1][1] == manual


async def test_snapshot_and_subscribe(eng, bulbs):
    e, q = eng
    snap = e.snapshot()
    assert set(snap) == set(ENGINE_SNAPSHOT_KEYS)
    assert snap["bulbs"]["desk"]["ip"] == "10.0.0.2" and snap["bulbs"]["desk"]["mac"] == "aa"
    e.trigger("low")
    e.trigger("mid", priority=1)
    await until(q, lambda ev: ev["type"] == "signal_start")
    snap = e.snapshot()
    json.dumps(snap)
    assert snap["playing"]["signal"] == "low" and snap["playing"]["repeating"] is False
    assert [j["signal"] for j in snap["queue"]] == ["mid"]
    e.unsubscribe(q)
    small = Engine(bulbs, patterns.loads(TOML), event_queue_size=2)
    sq = small.subscribe()
    for _ in range(3):
        small.mute(None)
    small.mute(5)
    assert sq.qsize() == 2 and sq.get_nowait()["until"] is None and sq.get_nowait()["until"] is not None


async def test_default_patterns_all_play_and_restore(bulbs, clock):
    e = make(bulbs, clock, ps=patterns.load(PKG_DIR / "patterns_default.toml"), time_scale=0.0005)
    q = e.subscribe()
    task = asyncio.create_task(e.run())
    try:
        for name in e.patterns.names():
            j = e.trigger(name)
            ev = await until(q, ended(j), timeout=5)
            assert ev["reason"] in ("done", "timeout"), name
            await e.wait_idle()
            assert bulbs["desk"].state == BASE_DESK, name
            assert bulbs["room"].state == BASE_ROOM, name
    finally:
        task.cancel()


async def test_meeting_t10_drifts_and_skips_dark_bulbs(clock):
    bulbs = {"desk": FakeBulb("desk", initial=BASE_DESK), "room": FakeBulb("room", initial=LightState(on=False))}
    e = make(bulbs, clock, ps=patterns.load(PKG_DIR / "patterns_default.toml"), time_scale=0.0005)
    q = e.subscribe()
    task = asyncio.create_task(e.run())
    try:
        await until(q, ended(e.trigger("meeting_t10")), timeout=5)
        temps = [s.temp for _, s in bulbs["desk"].sends]
        assert max(temps) == 5600 and temps[-1] == 2700
        assert all(s.dimming == 80 for _, s in bulbs["desk"].sends)
        assert bulbs["room"].sends == []
    finally:
        task.cancel()


async def test_shutdown_restores(bulbs, clock):
    e = make(bulbs, clock)
    q = e.subscribe()
    task = asyncio.create_task(e.run())
    e.trigger("rep")
    await until(q, lambda ev: ev["type"] == "signal_start")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bulbs["desk"].state == BASE_DESK and bulbs["room"].state == BASE_ROOM


def test_compose():
    from bulbsig.engine import compose
    cur = LightState(on=True, dimming=40, rgb=(255, 0, 0))
    assert compose(cur, temp=4000) == LightState(on=True, dimming=40, temp=4000)
    assert compose(cur, rgb=(0, 0, 255), bri=70) == LightState(on=True, dimming=70, rgb=(0, 0, 255))
    assert compose(cur, scene=5, speed=120) == LightState(on=True, dimming=40, scene=5, speed=120)
    off = compose(cur, on=False)
    assert not off.on and off.rgb == (255, 0, 0)
    assert compose(off, on=True) == cur
    assert compose(off, bri=60) == LightState(on=True, dimming=60, rgb=(255, 0, 0))
    assert compose(None, on=True) == LightState(on=True, dimming=100, temp=2700)
    assert compose(LightState(on=False), temp=3000).on


async def test_set_light_applies_when_idle(eng, bulbs):
    e, q = eng
    st = e.set_light("desk", temp=4000)
    assert st == LightState(on=True, dimming=100, temp=4000)
    ev = await until(q, lambda ev: ev["type"] == "light")
    assert ev["role"] == "desk" and ev["state"]["temp"] == 4000
    await e.wait_idle()
    assert bulbs["desk"].sets[-1][1] == st
    assert bulbs["room"].sets == []
    with pytest.raises(KeyError):
        e.set_light("hall", on=True)


async def test_set_light_keeps_brightness_of_known_base(eng, bulbs):
    e, q = eng
    job = e.trigger("low")
    await until(q, ended(job))
    await e.wait_idle()
    assert e.set_light("desk", rgb=(0, 200, 100)) == LightState(on=True, dimming=80, rgb=(0, 200, 100))


async def test_set_light_during_signal_wins_over_snapshot(eng, bulbs, clock):
    e, q = eng
    job = e.trigger("low")
    await until(q, lambda ev: ev["type"] == "signal_start" and ev["job"] == job)
    new = e.set_light("desk", rgb=(10, 20, 30))
    clock.t += 60  # stale: the next signal would normally re-read the bulb
    await until(q, ended(job))
    await e.wait_idle()
    assert bulbs["desk"].sets[-1][1] == new
    reads = bulbs["desk"].reads
    clock.t += 60
    job2 = e.trigger("low")
    await until(q, ended(job2))
    await e.wait_idle()
    assert bulbs["desk"].reads == reads + 1  # pin released once applied
    assert bulbs["desk"].sets[-1][1] == new


async def test_set_light_not_reread_before_applied(bulbs, clock):
    e = make(bulbs, clock)
    e.set_light("desk", temp=5000)
    clock.t += 60
    await e._apply_dirty()
    assert bulbs["desk"].reads == 0
    assert bulbs["desk"].sets[-1][1].temp == 5000


async def test_set_light_under_on_air_keeps_overlay(eng, bulbs):
    e, q = eng
    e.set_mode("on_air", True)
    await e.wait_idle()
    e.set_light("room", temp=6500)
    await e.wait_idle()
    assert bulbs["room"].sets[-1][1].rgb == (255, 0, 0)
    e.set_mode("on_air", False)
    await e.wait_idle()
    assert bulbs["room"].sets[-1][1] == LightState(on=True, dimming=50, temp=6500)


async def test_set_light_not_clamped_in_quiet_hours(bulbs):
    e = make(bulbs, Clock(NIGHT), quiet=("23:00", "08:00", 10))
    task = asyncio.create_task(e.run())
    try:
        e.set_light("desk", bri=90, rgb=(255, 255, 255))
        await e.wait_idle()
        assert bulbs["desk"].sets[-1][1].dimming == 90
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
