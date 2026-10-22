from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bulbsig.calendar_ics import CalendarWatcher, main

FIX = Path(__file__).parent / "fixtures" / "cal_basic.ics"
UTC = timezone.utc


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def set(self, hh: int, mm: int, ss: int = 0) -> None:
        self.t = datetime(2026, 10, 22, hh, mm, ss, tzinfo=UTC)


class Rec:
    def __init__(self) -> None:
        self.triggers: list[tuple[str, str]] = []
        self.modes: list[tuple[str, bool]] = []
        self.events: list[dict] = []

    def watcher(self, url: str | Path, clock: Clock, **kw) -> CalendarWatcher:
        return CalendarWatcher([str(url)], trigger=lambda s, k: self.triggers.append((s, k)),
                               set_mode=lambda m, a: self.modes.append((m, a)),
                               publish=self.events.append, now=clock, **kw)

    def signals(self) -> list[str]:
        return [s for s, _ in self.triggers]


def at(hh: int, mm: int, ss: int = 0) -> Clock:
    return Clock(datetime(2026, 10, 22, hh, mm, ss, tzinfo=UTC))


async def run_until(w: CalendarWatcher, clock: Clock, end: datetime, step_s: int = 1) -> None:
    while clock.t < end:
        await w.tick()
        clock.t += timedelta(seconds=step_s)


async def test_parse_filters_and_recurrence():
    clock = at(8, 0)
    w = Rec().watcher(FIX, clock)
    await w.refresh()
    titles = [(m["title"], m["start"]) for m in w.upcoming()]
    assert titles == [
        ("Standup", "2026-10-22T09:00:00+00:00"),
        ("Design review", "2026-10-22T10:00:00+00:00"),
    ]
    assert [m["start"] for m in w.upcoming(hours=26)][-1] == "2026-10-23T09:00:00+00:00"
    assert all(m["phase"] == "upcoming" for m in w.upcoming())
    assert [m["title"] for m in w.upcoming(hours=1.5)] == ["Standup"]


async def test_phase_sequence_and_on_air():
    clock = at(9, 40)
    r = Rec()
    w = r.watcher(FIX, clock)
    await w.refresh()
    await run_until(w, clock, datetime(2026, 10, 22, 10, 31, tzinfo=UTC), step_s=5)
    design = [(s, k) for s, k in r.triggers if "normal-1" in k]
    assert design == [(f"meeting_{p}", f"cal:normal-1:2026-10-22T10:00:00+00:00:{p}")
                      for p in ("t10", "t5", "t2", "t1", "t0")]
    assert r.modes == [("on_air", True), ("on_air", False)]
    assert [e["phase"] for e in r.events] == ["t10", "t5", "t2", "t1", "t0", "end"]
    assert r.events[-1]["title"] == "Design review"


async def test_upcoming_phase_labels():
    clock = at(9, 57)
    w = Rec().watcher(FIX, clock)
    await w.refresh()
    assert w.upcoming()[0]["phase"] == "t5"
    clock.set(10, 5)
    assert w.upcoming()[0]["phase"] == "now"


async def test_missed_phases_fire_only_latest():
    clock = at(9, 57)
    r = Rec()
    w = r.watcher(FIX, clock)
    await w.refresh()
    await w.tick()
    await w.tick()
    assert r.signals() == ["meeting_t5"]
    clock.set(9, 58)
    await w.tick()
    assert r.signals() == ["meeting_t5", "meeting_t2"]


async def test_started_mid_meeting_sets_on_air_without_t0():
    clock = at(10, 15)
    r = Rec()
    w = r.watcher(FIX, clock)
    await w.refresh()
    await w.tick()
    assert r.triggers == [] and r.modes == [("on_air", True)]
    clock.set(10, 2)  # within grace: t0 still fires for a fresh watcher
    r2 = Rec()
    w2 = r2.watcher(FIX, clock)
    await w2.refresh()
    await w2.tick()
    assert r2.signals() == ["meeting_t0"]


def _ics(tmp_path: Path, events: list[tuple[str, str, str]]) -> Path:
    body = "".join(f"BEGIN:VEVENT\nUID:{u}\nSUMMARY:{u}\nDTSTART:{s}\nDTEND:{e}\nEND:VEVENT\n" for u, s, e in events)
    p = tmp_path / "cal.ics"
    p.write_text(f"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:x\n{body}END:VCALENDAR\n")
    return p


async def test_overlapping_meetings_keep_on_air(tmp_path):
    p = _ics(tmp_path, [("a", "20261022T100000Z", "20261022T103000Z"),
                        ("b", "20261022T102000Z", "20261022T110000Z")])
    clock = at(9, 59)
    r = Rec()
    w = r.watcher(p.as_uri(), clock)
    await w.refresh()
    await run_until(w, clock, datetime(2026, 10, 22, 10, 45, tzinfo=UTC), step_s=10)
    assert r.modes == [("on_air", True)]
    await run_until(w, clock, datetime(2026, 10, 22, 11, 1, tzinfo=UTC), step_s=10)
    assert r.modes == [("on_air", True), ("on_air", False)]


async def test_on_air_until_ack_overruns(tmp_path):
    p = _ics(tmp_path, [("a", "20261022T100000Z", "20261022T103000Z")])
    clock = at(10, 29)
    r = Rec()
    w = r.watcher(p, clock, on_air_until_ack=True)
    await w.refresh()
    await run_until(w, clock, datetime(2026, 10, 22, 10, 32, 30, tzinfo=UTC), step_s=5)
    assert r.modes == [("on_air", True)]
    over = [k for s, k in r.triggers if s == "meeting_overrun"]
    assert over == ["cal:a:2026-10-22T10:00:00+00:00:overrun"] * 3
    w.mode_changed("on_air", False)
    await run_until(w, clock, datetime(2026, 10, 22, 10, 40, tzinfo=UTC), step_s=5)
    assert len([s for s, _ in r.triggers if s == "meeting_overrun"]) == 3
    assert r.modes == [("on_air", True)]


async def test_manual_off_mid_meeting_sticks(tmp_path):
    p = _ics(tmp_path, [("a", "20261022T100000Z", "20261022T103000Z")])
    clock = at(10, 1)
    r = Rec()
    w = r.watcher(p, clock)
    await w.refresh()
    await w.tick()
    w.mode_changed("on_air", False)
    await run_until(w, clock, datetime(2026, 10, 22, 10, 35, tzinfo=UTC), step_s=30)
    assert r.modes == [("on_air", True)]


async def test_failed_fetch_keeps_last_good(tmp_path):
    p = _ics(tmp_path, [("a", "20261022T100000Z", "20261022T103000Z")])
    clock = at(9, 0)
    w = Rec().watcher(p, clock)
    await w.refresh()
    p.write_text("this is not ics")
    await w.refresh()
    assert [m["uid"] for m in w.upcoming()] == ["a"] and w.errors
    p.unlink()
    await w.refresh()
    assert [m["uid"] for m in w.upcoming()] == ["a"]
    w2 = Rec().watcher("http://127.0.0.1:9/nope.ics", clock)
    await w2.refresh()
    assert w2.upcoming() == [] and w2.errors


async def test_async_callbacks_and_failures_tolerated(tmp_path):
    p = _ics(tmp_path, [("a", "20261022T100000Z", "20261022T103000Z")])
    got = []

    async def trig(s, k):
        got.append(s)

    def boom(m, a):
        raise RuntimeError("x")

    w = CalendarWatcher([str(p)], trigger=trig, set_mode=boom, now=at(10, 0))
    await w.refresh()
    await w.tick()
    assert got == ["meeting_t0"]


def test_cli(capsys):
    # real clock: fixture events are in the past/future relative to today, so just check it runs
    assert main(["--url", str(FIX), "--hours", "1"]) == 0
    assert capsys.readouterr().out
    assert main(["--url", "/nonexistent.ics"]) == 1
