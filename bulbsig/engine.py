"""Signal engine: queue, priorities, merging, quiet hours, modes and restore."""
from __future__ import annotations

import asyncio
import itertools
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from datetime import time as dtime
from typing import Any, Callable, Iterator

from .contracts import Bulb, LightState
from .patterns import MIN_STEP_MS, Look, Pattern, PatternSet, lerp_state, resolve_priority


@dataclass(eq=False)
class Job:
    id: str
    signal: str
    pattern: Pattern
    priority: int
    key: str | None
    origin: dict[str, Any] | None
    seq: int
    count: int = 1
    played_s: float = 0.0
    started: float | None = None
    stop: str | None = None


def _state_json(s: LightState | None) -> dict[str, Any] | None:
    if s is None:
        return None
    d = asdict(s)
    if d["rgb"] is not None:
        d["rgb"] = list(d["rgb"])
    return d


def compose(cur: LightState | None, *, on: bool | None = None, bri: int | None = None,
            rgb: tuple[int, int, int] | None = None, temp: int | None = None,
            scene: int | None = None, speed: int | None = None) -> LightState:
    """The resting state after a change. An off state keeps its colour so turning on brings it back."""
    if rgb is not None:
        look = LightState(on=True, rgb=tuple(rgb))  # type: ignore[arg-type]
    elif temp is not None:
        look = LightState(on=True, temp=temp)
    elif scene:
        look = LightState(on=True, scene=scene, speed=speed)
    elif cur is not None and (cur.rgb or cur.temp or cur.scene):
        look = cur
    else:
        look = LightState(on=True, temp=2700)
    if on is None:
        on = True if bri is not None or look is not cur else (cur.on if cur else True)
    dim = bri if bri is not None else ((cur.dimming if cur else None) or 100)
    return replace(look, on=on, dimming=dim)


def _hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


class Engine:
    def __init__(
        self,
        bulbs: dict[str, Bulb],
        patterns: PatternSet,
        *,
        quiet: tuple[str, str, int] | None = None,
        merge_window: float = 20.0,
        refresh_idle_s: float = 1.0,
        min_step_ms: int = MIN_STEP_MS,
        time_scale: float = 1.0,
        clock: Callable[[], float] = time.time,
        state_timeout: float = 3.0,
        event_queue_size: int = 256,
    ) -> None:
        self.bulbs = bulbs
        self.patterns = patterns
        self._quiet = (_hhmm(quiet[0]), _hhmm(quiet[1]), int(quiet[2])) if quiet else None
        self._merge_window = merge_window
        self._refresh_idle_s = refresh_idle_s
        self._floor = min_step_ms
        self._scale = time_scale
        self._clock = clock
        self._state_timeout = state_timeout
        self._qsize = event_queue_size

        self._seq = itertools.count()
        self._queue: list[Job] = []
        self._playing: Job | None = None
        self._task: asyncio.Task[str] | None = None
        self._recent: dict[str, tuple[Job, float]] = {}
        self._modes: list[str] = []
        self._muted_until: float | None = None
        self._subs: list[asyncio.Queue[dict[str, Any]]] = []

        self._base: dict[str, LightState] = {}
        self._shown: dict[str, LightState] = {}
        self._overlaid: dict[str, bool] = {}
        self._pinned: set[str] = set()  # base set by set_light and not yet applied: never re-read over it
        self._reachable: dict[str, bool | None] = {r: None for r in bulbs}
        self._dirty: set[str] = set()
        self._touched: set[str] = set()
        self._last_active = float("-inf")
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._closing = False

    # -- public API -------------------------------------------------------

    def trigger(self, signal: str, *, key: str | None = None, priority: int | str | None = None,
                origin: dict[str, Any] | None = None) -> str:
        p = self.patterns.get(signal)
        prio = p.priority if priority is None else resolve_priority(priority)
        now = self._clock()
        if key is not None and (m := self._merge_target(key, now)) is not None:
            m.count += 1
            self._emit({"type": "merged", "job": m.id, "signal": m.signal, "key": key, "count": m.count})
            return m.id
        job = Job(uuid.uuid4().hex[:10], signal, p, prio, key, origin, next(self._seq))
        if self._is_muted(now):
            self._emit({"type": "signal_end", "job": job.id, "signal": signal, "reason": "muted"})
            return job.id
        self._enqueue(job)
        if self._playing is not None and prio > self._playing.priority:
            self._stop(self._playing, "preempted")
        self._kick()
        return job.id

    def ack(self, job_id: str | None = None) -> bool:
        j = self._playing
        if job_id is None:
            if j is not None and j.pattern.repeating:
                self._stop(j, "ack")
                return True
            return False
        if j is not None and j.id == job_id:
            self._stop(j, "ack")
            return True
        for q in self._queue:
            if q.id == job_id:
                self._queue.remove(q)
                self._end(q, "ack")
                return True
        return False

    def set_mode(self, mode: str, active: bool) -> None:
        m = self.patterns.modes.get(mode)
        if m is None:
            raise KeyError(f"unknown mode {mode!r}")
        if active == (mode in self._modes):
            return
        if active:
            self._modes.append(mode)
        else:
            self._modes.remove(mode)
        self._dirty |= {r for r in m.looks if r in self.bulbs}
        self._emit({"type": "mode", "mode": mode, "active": active})
        self._kick()

    def mute(self, seconds: float | None) -> None:
        if seconds:
            self._muted_until = self._clock() + seconds
            if self._playing is not None:
                self._stop(self._playing, "muted")
            for j in self._queue:
                self._end(j, "muted")
            self._queue.clear()
        else:
            self._muted_until = None
        self._emit({"type": "mute", "until": self._muted_until})

    def set_light(self, role: str, **change: Any) -> LightState:
        if role not in self.bulbs:
            raise KeyError(f"unknown bulb {role!r}")
        st = compose(self._base.get(role), **change)
        self._base[role] = st
        self._pinned.add(role)
        self._dirty.add(role)
        self._emit({"type": "light", "role": role, "state": _state_json(st)})
        if self._playing is None:
            self._kick()
        return st

    async def current_bases(self) -> dict[str, LightState]:
        # mid-signal the bulbs show the signal, not their base
        if self._playing is None:
            await self._refresh(list(self.bulbs))
        return dict(self._base)

    def snapshot(self) -> dict[str, Any]:
        j = self._playing
        return {
            "playing": None if j is None else {
                "job": j.id, "signal": j.signal, "started": j.started, "repeating": j.pattern.repeating},
            "queue": [{"job": q.id, "signal": q.signal, "priority": q.priority, "key": q.key}
                      for q in sorted(self._queue, key=self._order)],
            "modes": list(self._modes),
            "muted_until": self._muted_until if self._is_muted(self._clock()) else None,
            "quiet": self._quiet_now(),
            "bulbs": {r: {"mac": b.mac, "ip": getattr(b, "ip", None), "reachable": self._reachable.get(r),
                          "state": _state_json(self._shown.get(r) or self._base.get(r))}
                      for r, b in self.bulbs.items()},
        }

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._qsize)
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        if q in self._subs:
            self._subs.remove(q)

    async def wait_idle(self) -> None:
        await self._idle.wait()

    async def run(self) -> None:
        try:
            while True:
                self._wake.clear()
                if self._dirty:
                    await self._apply_dirty()
                    continue
                if self._queue:
                    job = min(self._queue, key=self._order)
                    self._queue.remove(job)
                    await self._play(job)
                    continue
                self._idle.set()
                await self._wake.wait()
        finally:
            self._closing = True
            self._idle.set()

    # -- internals --------------------------------------------------------

    @staticmethod
    def _order(j: Job) -> tuple[int, int]:
        return (-j.priority, j.seq)

    def _kick(self) -> None:
        self._idle.clear()
        self._wake.set()

    def _emit(self, ev: dict[str, Any]) -> None:
        for q in self._subs:
            if q.full():
                q.get_nowait()
            q.put_nowait(ev)

    def _enqueue(self, job: Job) -> None:
        self._queue.append(job)
        self._emit({"type": "queued", "job": job.id, "signal": job.signal, "priority": job.priority, "key": job.key})

    def _end(self, job: Job, reason: str) -> None:
        self._emit({"type": "signal_end", "job": job.id, "signal": job.signal, "reason": reason})

    def _stop(self, job: Job, reason: str) -> None:
        job.stop = job.stop or reason
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def _merge_target(self, key: str, now: float) -> Job | None:
        if self._playing is not None and self._playing.key == key:
            return self._playing
        for j in self._queue:
            if j.key == key:
                return j
        rec = self._recent.get(key)
        if rec is not None and now - rec[1] <= self._merge_window:
            return rec[0]
        return None

    def _is_muted(self, now: float) -> bool:
        return self._muted_until is not None and now < self._muted_until

    def _quiet_now(self) -> bool:
        if self._quiet is None:
            return False
        s, e, _ = self._quiet
        t = datetime.fromtimestamp(self._clock()).time()
        return s <= t < e if s <= e else (t >= s or t < e)

    def _overlay(self, role: str) -> Look | None:
        look = None
        for name in self._modes:
            look = self.patterns.modes[name].looks.get(role, look)
        return look

    def _target(self, role: str) -> LightState | None:
        base = self._base.get(role)
        look = self._overlay(role)
        return look.apply(base) if look is not None else base

    def _bulb_error(self, role: str, err: BaseException | str) -> None:
        self._reachable[role] = False
        msg = err if isinstance(err, str) else (f"{type(err).__name__}: {err}" if str(err) else type(err).__name__)
        self._emit({"type": "bulb_error", "role": role, "error": msg})

    async def _read(self, role: str) -> None:
        try:
            st = await asyncio.wait_for(self.bulbs[role].get_state(), self._state_timeout)
        except Exception as e:  # any bulb failure must not stop the other bulbs
            self._bulb_error(role, e)
            return
        self._base[role] = st
        self._reachable[role] = True

    async def _refresh(self, roles: list[str]) -> None:
        stale = self._clock() - self._last_active > self._refresh_idle_s
        # A bulb showing a mode overlay is not showing its base, so reading it would lose the base.
        todo = [r for r in roles if (stale or r not in self._base) and not self._overlaid.get(r)
                and r not in self._pinned]
        if todo:
            await asyncio.gather(*(self._read(r) for r in todo))

    async def _restore(self, roles: set[str]) -> None:
        async def one(r: str) -> None:
            base = self._base.get(r)
            t = self._target(r)
            if t is None:
                return
            try:
                ok = await self.bulbs[r].set(t)
            except Exception as e:
                self._bulb_error(r, e)
                return
            if not ok:
                self._bulb_error(r, "restore not confirmed")
            else:
                self._reachable[r] = True
            self._shown[r] = t
            self._overlaid[r] = self._overlay(r) is not None
            if self._base.get(r) is base:
                self._pinned.discard(r)

        await asyncio.gather(*(one(r) for r in sorted(roles) if r in self.bulbs))
        self._last_active = self._clock()

    async def _apply_dirty(self) -> None:
        roles, self._dirty = self._dirty, set()
        await self._refresh(sorted(roles))
        await self._restore(roles)

    def _send(self, role: str, st: LightState) -> None:
        self._touched.add(role)
        self._shown[role] = st
        try:
            self.bulbs[role].send(st)
        except Exception as e:
            self._bulb_error(role, e)

    def _frames(self, p: Pattern, roles: list[str], cur: dict[str, LightState | None],
                clamp: int | None) -> Iterator[tuple[float, list[tuple[str, LightState]]]]:
        def dim(s: LightState, is_base: bool) -> LightState:
            if clamp is None or is_base or not s.on or (s.dimming or 100) <= clamp:
                return s
            return LightState(on=True, dimming=clamp, temp=s.temp, rgb=s.rgb, scene=s.scene, speed=s.speed)

        for st in p.steps:
            targets = [r for r in roles if st.hits(r)]
            if st.wait or not targets:
                yield p.step_ms(st.ms, self._floor), []
                continue
            goal = {r: (self._target(r) or cur[r]) if st.base else st.look.apply(cur[r]) for r in targets}
            if st.ramp > 1:
                start = {r: cur[r] for r in targets}
                sub = p.step_ms(st.ms / st.ramp, self._floor)
                for i in range(1, st.ramp + 1):
                    yield sub, [(r, dim(lerp_state(start[r], g, i / st.ramp), st.base))
                                for r, g in goal.items() if g is not None]
            else:
                yield p.step_ms(st.ms, self._floor), [(r, dim(g, st.base)) for r, g in goal.items() if g is not None]

    async def _perform(self, job: Job, roles: list[str], passes: int | None, clamp: int | None) -> str:
        loop = asyncio.get_running_loop()
        p = job.pattern
        max_s = p.max_duration_s
        budget = None if max_s is None else max_s - job.played_s
        cur = {r: self._target(r) for r in roles}
        t0 = nxt = loop.time()
        n = 0
        try:
            while passes is None or n < passes:
                pass_start = nxt
                for ms, sends in self._frames(p, roles, cur, clamp):
                    if budget is not None and (loop.time() - t0) / self._scale >= budget:
                        return "timeout"
                    for r, s in sends:
                        self._send(r, s)
                        cur[r] = s
                    if ms > 0:
                        nxt += ms / 1000 * self._scale
                        await asyncio.sleep(max(0.0, nxt - loop.time()))
                n += 1
                if nxt == pass_start:
                    break
            return "done"
        finally:
            job.played_s += (loop.time() - t0) / self._scale

    async def _play(self, job: Job) -> None:
        p = job.pattern
        quiet = self._quiet_now()
        if quiet and p.quiet == "skip":
            self._end(job, "muted")
            self._finish(job)
            return
        self._playing = job
        self._touched = set()
        reason = "done"
        try:
            roles = [r for r in self.bulbs if p.touches(r)]
            await self._refresh(roles)
            if p.only_lit:
                roles = [r for r in roles if (t := self._target(r)) is None or t.on]
            if job.stop is None:
                single = quiet or not p.repeating
                passes = 1 if single else (None if p.repeat == "until_ack" else int(p.repeat))
                clamp = self._quiet[2] if quiet and p.quiet == "dim" and self._quiet else None
                job.started = job.started or self._clock()
                self._emit({"type": "signal_start", "job": job.id, "signal": job.signal})
                task = asyncio.create_task(self._perform(job, roles, passes, clamp))
                self._task = task
                try:
                    await asyncio.wait({task})
                finally:
                    if not task.done():  # run() itself was cancelled
                        job.stop = job.stop or "preempted"
                        task.cancel()
                        await asyncio.wait({task})
                if not task.cancelled() and task.exception() is None:
                    reason = task.result()
            reason = job.stop or reason
        finally:
            self._playing = None
            self._task = None
            touched, self._touched = self._touched | self._dirty, set()
            self._dirty = set()
            await self._restore(touched)
        self._end(job, reason)
        if reason == "preempted" and p.repeat == "until_ack" and not self._closing \
                and job.played_s < (p.max_duration_s or 0):
            job.stop = None
            self._enqueue(job)
        else:
            self._finish(job)

    def _finish(self, job: Job) -> None:
        if job.key is not None:
            self._recent[job.key] = (job, self._clock())
            cutoff = self._clock() - self._merge_window
            self._recent = {k: v for k, v in self._recent.items() if v[1] >= cutoff}
