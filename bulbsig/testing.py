"""In-memory bulb for tests: no network, records every call."""
from __future__ import annotations

import time
from typing import Callable

from .contracts import BulbUnreachable, LightState


class FakeBulb:
    def __init__(self, role: str, mac: str = "a8bb50000000", initial: LightState | None = None, *,
                 ip: str | None = "10.0.0.1", clock: Callable[[], float] = time.monotonic) -> None:
        self.role = role
        self.mac = mac
        self.ip = ip
        self.state = initial or LightState(on=True, dimming=100, temp=2700)
        self.fail = False
        self.confirm = True
        self.clock = clock
        self.sends: list[tuple[float, LightState]] = []
        self.sets: list[tuple[float, LightState]] = []
        self.calls: list[tuple[str, float, LightState | None]] = []
        self.reads = 0

    async def get_state(self) -> LightState:
        self.reads += 1
        self.calls.append(("get", self.clock(), None))
        if self.fail:
            raise BulbUnreachable(f"{self.role}: fake failure")
        return self.state

    def send(self, state: LightState) -> None:
        t = self.clock()
        self.sends.append((t, state))
        self.calls.append(("send", t, state))
        if not self.fail:
            self.state = state

    async def set(self, state: LightState, *, attempts: int = 3) -> bool:
        if self.fail:
            raise BulbUnreachable(f"{self.role}: fake failure")
        t = self.clock()
        self.sets.append((t, state))
        self.calls.append(("set", t, state))
        self.state = state
        return self.confirm

    def reset(self) -> None:
        self.sends.clear()
        self.sets.clear()
        self.calls.clear()
        self.reads = 0
