"""ICS calendar watcher: fires meeting lead-up signals and toggles on_air."""
from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

import icalendar
import recurring_ical_events

log = logging.getLogger("bulbsig.calendar")

LEAD_PHASES: tuple[tuple[str, int], ...] = (("t10", 600), ("t5", 300), ("t2", 120), ("t1", 60), ("t0", 0))
T0_GRACE_S = 300  # starting the daemon mid-meeting must not flash meeting_t0
PAST = timedelta(hours=1)


@dataclass(frozen=True)
class Meeting:
    uid: str
    title: str
    start: datetime
    end: datetime

    @property
    def base(self) -> str:
        return f"cal:{self.uid}:{self.start.isoformat()}"


def _aware(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.astimezone()
    return None


def parse_meetings(cal: icalendar.Calendar, start: datetime, end: datetime) -> list[Meeting]:
    out = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        dtstart = ev.get("DTSTART")
        s = dtstart.dt if dtstart else None
        if s is None or (isinstance(s, date) and not isinstance(s, datetime)):
            continue
        if str(ev.get("STATUS", "")).upper() == "CANCELLED" or str(ev.get("TRANSP", "")).upper() == "TRANSPARENT":
            continue
        s = _aware(s)
        e = _aware(ev["DTEND"].dt) if ev.get("DTEND") else None
        if e is None and ev.get("DURATION"):
            e = s + ev["DURATION"].dt
        out.append(Meeting(str(ev.get("UID", "")) or f"anon-{s.isoformat()}",
                           str(ev.get("SUMMARY", "")) or "(no title)", s, e or s))
    return sorted(out, key=lambda m: (m.start, m.uid))


_NOW = re.compile(r"\b(starting now|starts now|is starting|has started|now)\b", re.I)
_IN_MIN = re.compile(r"\bin (\d{1,3}) ?min", re.I)
_CLOCK = r"(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?"
_RANGE = re.compile(_CLOCK + r"\s*[–—-]\s*" + _CLOCK, re.I)
_ONE = re.compile(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))\s*([ap]\.?m\.?)?|\b(\d{1,2})\s*([ap]\.?m\.?)", re.I)


def _hm(h: str, m: str | None, ampm: str | None) -> tuple[int, int] | None:
    hh, mm = int(h), int(m or 0)
    if ampm:
        pm = ampm.lower().startswith("p")
        if not 1 <= hh <= 12:
            return None
        hh = hh % 12 + (12 if pm else 0)
    return (hh, mm) if hh < 24 and mm < 60 else None


def _at(received: datetime, hm: tuple[int, int]) -> datetime:
    t = received.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    if t < received - timedelta(hours=2):  # a reminder late in the evening for a meeting after midnight
        t += timedelta(days=1)
    return t


def parse_push(text: str, received: datetime, *, default_lead_min: float = 10,
               default_len_min: float = 30) -> tuple[datetime, datetime]:
    """Start/end of a meeting from a calendar web-push (e.g. Google Calendar in a
    browser). Falls back to "starts default_lead_min after the push"."""
    start = end = None
    r = _RANGE.search(text)
    if r and not any(r.group(i) for i in (2, 3, 5, 6)):  # "Q3 – 4 planning" is not a time
        r = None
    if r:
        a = _hm(r.group(1), r.group(2), r.group(3) or r.group(6))
        b = _hm(r.group(4), r.group(5), r.group(6) or r.group(3))
        if a and b:
            start, end = _at(received, a), _at(received, b)
            if end <= start:
                end += timedelta(days=1)
    if start is None and (m := _IN_MIN.search(text)):
        start = received + timedelta(minutes=int(m.group(1)))
    if start is None and _NOW.search(text):
        start = received
    if start is None and (o := _ONE.search(text)):
        hm = _hm(o.group(1), o.group(2), o.group(3)) if o.group(1) else _hm(o.group(4), None, o.group(5))
        if hm:
            start = _at(received, hm)
    if start is None:
        start = received + timedelta(minutes=default_lead_min)
    return start, end or start + timedelta(minutes=default_len_min)


async def fetch(url: str, *, timeout: float = 20) -> bytes:
    u = urlparse(url)
    if u.scheme in ("http", "https", "webcal"):
        import aiohttp
        if u.scheme == "webcal":
            url = "https" + url[len("webcal"):]
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.get(url) as r:
                r.raise_for_status()
                return await r.read()
    path = Path(unquote(u.path)) if u.scheme == "file" else Path(url).expanduser()
    return await asyncio.to_thread(path.read_bytes)


async def _call(fn: Callable[..., Any] | None, *a: Any) -> None:
    if fn is None:
        return
    try:
        r = fn(*a)
        if inspect.isawaitable(r):
            await r
    except Exception:
        log.exception("calendar callback %r failed", getattr(fn, "__name__", fn))


class CalendarWatcher:
    def __init__(self, urls: list[str], *, trigger: Callable[[str, str], Any],
                 set_mode: Callable[[str, bool], Any],
                 publish: Callable[[dict[str, Any]], Any] | None = None,
                 poll_s: float = 300, now: Callable[[], datetime] | None = None,
                 lead_phases: tuple[tuple[str, int], ...] = LEAD_PHASES,
                 on_air_until_ack: bool = False, overrun_every_s: float = 60) -> None:
        self.urls = list(urls)
        self.trigger, self.set_mode, self.publish = trigger, set_mode, publish
        self.poll_s = poll_s
        self.now = now or (lambda: datetime.now().astimezone())
        self.phases = tuple(sorted(lead_phases, key=lambda p: -p[1]))
        self.on_air_until_ack = on_air_until_ack
        self.overrun_every_s = overrun_every_s
        self.cals: dict[str, icalendar.Calendar] = {}
        self.errors: dict[str, str] = {}
        self.meetings: list[Meeting] = []
        self._done: dict[str, tuple[int, datetime]] = {}  # base -> (latest phase index fired, end)
        self._live: set[str] = set()  # instances seen in progress
        self._on_air = False
        self._overrun: Meeting | None = None
        self._last_overrun: datetime | None = None
        self._manual_off: set[str] = set()
        self.adhoc: dict[str, Meeting] = {}

    def add_adhoc(self, title: str, start: datetime, end: datetime) -> Meeting | None:
        """A meeting learnt from a notification rather than a feed. Returns None if
        the feed (or an earlier push) already has it."""
        t = title.strip().casefold()
        for m in [*self.meetings, *self.adhoc.values()]:
            if m.title.strip().casefold() == t and abs((m.start - start).total_seconds()) <= 120:
                return None
        m = Meeting(f"push-{start.strftime('%Y%m%dT%H%M')}-{abs(hash(t)) % 10**8}", title.strip() or "(meeting)",
                    start, end)
        self.adhoc[m.base] = m
        self.meetings = sorted([*self.meetings, m], key=lambda x: (x.start, x.uid))
        return m

    async def refresh(self) -> None:
        for url in self.urls:
            try:
                self.cals[url] = icalendar.Calendar.from_ical(await fetch(url))
                self.errors.pop(url, None)
            except Exception as e:  # keep the last good parse
                self.errors[url] = f"{type(e).__name__}: {e}"
                log.warning("calendar fetch failed for %s: %s", url, e)
        now = self.now()
        self.adhoc = {b: m for b, m in self.adhoc.items() if m.end > now - PAST}
        self.meetings = self._expand(now, timedelta(hours=24))

    def _expand(self, now: datetime, ahead: timedelta) -> list[Meeting]:
        seen: dict[str, Meeting] = {}
        for cal in self.cals.values():
            for m in parse_meetings(cal, now - PAST, now + ahead):
                seen.setdefault(m.base, m)
        feed = list(seen.values())
        for m in self.adhoc.values():
            t = m.title.strip().casefold()
            if not any(f.title.strip().casefold() == t and abs((f.start - m.start).total_seconds()) <= 120 for f in feed):
                seen.setdefault(m.base, m)
        return sorted(seen.values(), key=lambda m: (m.start, m.uid))

    def _phase(self, m: Meeting, now: datetime) -> int:
        idx = -1
        for i, (_, lead) in enumerate(self.phases):
            if now >= m.start - timedelta(seconds=lead):
                idx = i
        return idx

    def _event(self, m: Meeting, phase: str) -> dict[str, Any]:
        return {"type": "meeting", "phase": phase, "title": m.title, "uid": m.uid,
                "start": m.start.isoformat(), "end": m.end.isoformat()}

    async def tick(self) -> None:
        now = self.now()
        live = []
        for m in self.meetings:
            if now >= m.end:
                if m.base in self._live:
                    self._live.discard(m.base)
                    await _call(self.publish, self._event(m, "end"))
                continue
            idx = self._phase(m, now)
            prev = self._done.get(m.base, (-1, m.end))[0]
            if idx > prev:
                self._done[m.base] = (idx, m.end)
                name, lead = self.phases[idx]
                stale = lead == 0 and (now - m.start).total_seconds() > T0_GRACE_S
                if not stale:
                    await _call(self.trigger, f"meeting_{name}", f"{m.base}:{name}")
                    await _call(self.publish, self._event(m, name))
            if now >= m.start:
                live.append(m)
                self._live.add(m.base)
        self._manual_off &= {m.base for m in live}
        wanted = [m for m in live if m.base not in self._manual_off]
        if wanted:
            self._overrun = None
            if not self._on_air:
                self._on_air = True
                await _call(self.set_mode, "on_air", True)
        elif self._on_air:
            if self.on_air_until_ack:
                if self._overrun is None:
                    self._overrun = self._last_ended(now)
                    self._last_overrun = None
                await self._tick_overrun(now)
            else:
                self._on_air = False
                await _call(self.set_mode, "on_air", False)
        for base, (_, end) in list(self._done.items()):
            if end < now - 2 * PAST:
                del self._done[base]

    def _last_ended(self, now: datetime) -> Meeting | None:
        ended = [m for m in self.meetings if m.end <= now]
        return max(ended, key=lambda m: m.end) if ended else None

    async def _tick_overrun(self, now: datetime) -> None:
        if self._last_overrun is None or (now - self._last_overrun).total_seconds() >= self.overrun_every_s:
            self._last_overrun = now
            m = self._overrun
            key = f"{m.base}:overrun" if m else "cal:overrun"
            await _call(self.trigger, "meeting_overrun", key)
            if m:
                await _call(self.publish, self._event(m, "overrun"))

    def mode_changed(self, mode: str, active: bool) -> None:
        """Told by the daemon when on_air is switched externally."""
        if mode != "on_air" or active:
            return
        now = self.now()
        self._manual_off |= {m.base for m in self.meetings if m.start <= now < m.end}
        self._on_air = False
        self._overrun = None

    def upcoming(self, hours: float = 24) -> list[dict[str, Any]]:
        now = self.now()
        ms = self._expand(now, timedelta(hours=hours)) if hours > 24 else self.meetings
        horizon = now + timedelta(hours=hours)
        out = []
        for m in ms:
            if m.end <= now or m.start >= horizon:
                continue
            idx = self._phase(m, now)
            phase = "now" if now >= m.start else (self.phases[idx][0] if idx >= 0 else "upcoming")
            out.append({"title": m.title, "start": m.start.isoformat(), "end": m.end.isoformat(),
                        "phase": phase, "uid": m.uid})
        return out

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        last = -float("inf")
        while True:
            if loop.time() - last >= self.poll_s:
                last = loop.time()
                await self.refresh()
            try:
                await self.tick()
            except Exception:
                log.exception("calendar tick failed")
            await asyncio.sleep(1)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="bulbsig calendar")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--url", action="append", help="ICS URL or file (repeatable; default: config)")
    a = ap.parse_args(argv)
    urls = a.url
    if not urls:
        from . import config
        urls = config.load().ics_urls
    if not urls:
        print("no ICS URLs configured ([calendar] ics = [...]) and no --url given")
        return 1
    w = CalendarWatcher(urls, trigger=lambda s, k: None, set_mode=lambda m, on: None)
    asyncio.run(w.refresh())
    for url, err in w.errors.items():
        print(f"! {url}: {err}")
    items = w.upcoming(a.hours)
    if not items:
        print(f"no meetings in the next {a.hours:g}h")
    for it in items:
        s = datetime.fromisoformat(it["start"]).astimezone()
        e = datetime.fromisoformat(it["end"]).astimezone()
        print(f"{s:%a %H:%M}-{e:%H:%M}  {it['phase']:8}  {it['title']}")
    return 0 if len(w.errors) < len(urls) else 1
