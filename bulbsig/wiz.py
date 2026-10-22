"""WiZ bulbs over the local UDP API (port 38899).

  bulbsig bulbs discover                 sweep the LAN for bulbs and cache them
  bulbsig bulbs status
  bulbsig bulbs on | off | toggle
  bulbsig bulbs bri 40                   brightness 10-100
  bulbsig bulbs temp 2700                white temperature 2200-6500 K
  bulbsig bulbs color ff8800 [bri]
  bulbsig bulbs scene fireplace [speed]  see `bulbsig bulbs scenes`
  bulbsig bulbs raw '{"method":"getPilot","params":{}}'

Add -t <role|mac|ip|all> to target bulbs (default: all).
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import math
import socket
import subprocess
import sys
import time
import weakref
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import config as config_mod
from .contracts import BulbUnreachable, LightState

PORT = 38899
CACHE = Path.home() / ".config" / "bulbsig" / "bulbs-cache.json"

SCENES = {
    "ocean": 1, "romance": 2, "sunset": 3, "party": 4, "fireplace": 5,
    "cozy": 6, "forest": 7, "pastel": 8, "wakeup": 9, "bedtime": 10,
    "warmwhite": 11, "daylight": 12, "coolwhite": 13, "nightlight": 14,
    "focus": 15, "relax": 16, "truecolors": 17, "tvtime": 18,
    "plantgrowth": 19, "spring": 20, "summer": 21, "fall": 22,
    "deepdive": 23, "jungle": 24, "mojito": 25, "club": 26,
    "christmas": 27, "halloween": 28, "candlelight": 29,
    "goldenwhite": 30, "pulse": 31, "steampunk": 32,
}

Msg = dict[str, Any]


class WizNet(asyncio.DatagramProtocol):
    """One UDP endpoint for all bulbs; replies are matched by (source ip, method)."""

    def __init__(self, port: int = PORT, bind: str = "0.0.0.0"):
        self.port, self.bind = port, bind
        self.transport: asyncio.DatagramTransport | None = None
        self.listeners: list[Callable[[str, Msg], None]] = []
        self._pending: dict[tuple[str, str], list[asyncio.Future[Msg]]] = {}
        self._backlog: list[tuple[str, bytes]] = []
        self._opening: asyncio.Future[Any] | None = None

    async def start(self) -> WizNet:
        if self.transport is None:
            if self._opening is None:
                loop = asyncio.get_running_loop()
                self._opening = asyncio.ensure_future(
                    loop.create_datagram_endpoint(lambda: self, local_addr=(self.bind, 0)))
            await asyncio.shield(self._opening)
        return self

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()

    @property
    def closed(self) -> bool:
        return self.transport is not None and self.transport.is_closing()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        backlog, self._backlog = self._backlog, []
        for ip, data in backlog:
            self._sendto(ip, data)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            msg = json.loads(data)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        ip = addr[0]
        for f in self._pending.pop((ip, msg.get("method", "")), []):
            if not f.done():
                f.set_result(msg)
        for cb in list(self.listeners):
            cb(ip, msg)

    def error_received(self, exc: Exception) -> None:
        pass

    def _sendto(self, ip: str, data: bytes) -> None:
        try:
            self.transport.sendto(data, (ip, self.port))  # type: ignore[union-attr]
        except (OSError, ValueError):
            pass

    def send(self, ip: str, payload: Msg) -> None:
        data = json.dumps(payload).encode()
        if self.transport is None:
            self._backlog.append((ip, data))
            asyncio.ensure_future(self.start())
        else:
            self._sendto(ip, data)

    async def request(self, ip: str, method: str, params: Msg | None = None, *,
                      timeout: float = 1.0, tries: int = 3) -> Msg | None:
        await self.start()
        loop = asyncio.get_running_loop()
        key = (ip, method)
        for _ in range(tries):
            fut: asyncio.Future[Msg] = loop.create_future()
            self._pending.setdefault(key, []).append(fut)
            self.send(ip, {"method": method, "params": params or {}})
            try:
                return await asyncio.wait_for(fut, timeout)
            except TimeoutError:
                pass
            finally:
                waiting = self._pending.get(key, [])
                if fut in waiting:
                    waiting.remove(fut)
                if not waiting:
                    self._pending.pop(key, None)
        return None


_nets: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, WizNet] = weakref.WeakKeyDictionary()


def shared_net() -> WizNet:
    loop = asyncio.get_running_loop()
    net = _nets.get(loop)
    if net is None or net.closed:
        net = _nets[loop] = WizNet()
    return net


async def get_net() -> WizNet:
    return await shared_net().start()


def local_subnets() -> list[ipaddress.IPv4Network]:
    nets: list[ipaddress.IPv4Network] = []
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for line in out.splitlines():
        f = line.split()
        if len(f) < 4 or f[1].startswith(("docker", "br-", "veth", "virbr")):
            continue
        net = ipaddress.ip_interface(f[3]).network
        if net not in nets and net.num_addresses <= 1024:
            nets.append(net)  # type: ignore[arg-type]
    if not nets:
        # No iproute2 (macOS): assume a /24 around the default-route address.
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.0.2.1", 9))
                nets.append(ipaddress.ip_interface(s.getsockname()[0] + "/24").network)  # type: ignore[arg-type]
        except OSError:
            pass
    return nets


def load_cache() -> dict[str, Any]:
    try:
        d = json.loads(CACHE.read_text())
        if isinstance(d, dict) and isinstance(d.get("bulbs"), dict):
            return d
    except (OSError, ValueError):
        pass
    return {"bulbs": {}}


def save_cache(found: dict[str, dict]) -> None:
    cache = load_cache()
    ips = {b["ip"] for b in found.values()}
    cache["bulbs"] = {m: b for m, b in cache["bulbs"].items() if b.get("ip") not in ips} | found
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache, indent=2) + "\n")
    except OSError:
        pass


async def sweep(ips: list[str], *, port: int = PORT, wait: float = 1.5,
                rounds: int = 2, want: str | None = None) -> dict[str, Msg]:
    # Unicast to every host: this LAN's firewall drops replies to broadcasts.
    # Own socket: sends to dead hosts sit in the ARP queue and fill the socket's
    # send buffer for ~3 s, which would stall live traffic on the shared one.
    net = await WizNet(port).start()
    found: dict[str, Msg] = {}
    hit = asyncio.Event()

    def on_msg(ip: str, msg: Msg) -> None:
        r = msg.get("result")
        if msg.get("method") == "getSystemConfig" and isinstance(r, dict) and r.get("mac"):
            found[ip] = r
            if want and r["mac"] == want:
                hit.set()

    net.listeners.append(on_msg)
    try:
        for _ in range(rounds):
            for i, ip in enumerate([ip for ip in ips if ip not in found]):
                net.send(ip, {"method": "getSystemConfig", "params": {}})
                if i % 32 == 31:
                    await asyncio.sleep(0.003)
                if hit.is_set():
                    break
            try:
                await asyncio.wait_for(hit.wait(), wait)
            except TimeoutError:
                pass
            if hit.is_set() or len(found) == len(ips):
                break
    finally:
        net.close()
    return found


async def discover(subnets: list[str] | None = None, *, port: int = PORT,
                   want: str | None = None, wait: float = 1.5) -> dict[str, dict]:
    nets = [ipaddress.ip_network(s, strict=False) for s in subnets] if subnets else local_subnets()
    ips = list(dict.fromkeys(str(h) for n in nets for h in n.hosts()))
    replies = await sweep(ips, port=port, wait=wait, want=want)
    found = {r["mac"]: {"ip": ip, "module": r.get("moduleName")} for ip, r in replies.items()}
    if found:
        save_cache(found)
    return found


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(v)))


def matches(want: LightState, got: LightState) -> bool:
    if not want.on:
        return not got.on
    if not got.on:
        return False
    if want.dimming is not None and got.dimming != _clamp(want.dimming, 10, 100):
        return False
    if want.scene:
        return got.scene == want.scene and (want.speed is None or got.speed in (None, want.speed))
    if want.rgb is not None:
        return got.rgb == tuple(want.rgb)
    if want.temp is not None:
        return got.temp is not None and abs(got.temp - _clamp(want.temp, 2200, 6500)) <= 50
    return True


class WizBulb:
    def __init__(self, role: str, mac: str, ip: str | None, *, net: WizNet | None = None,
                 subnets: list[str] | None = None,
                 finder: Callable[[str], Awaitable[str | None]] | None = None,
                 timeout: float = 1.0, tries: int = 3, resweep_s: float = 30.0):
        self.role, self.mac, self.ip = role, mac.replace(":", "").lower(), ip
        self.net, self.subnets, self.finder = net, subnets, finder
        self.timeout, self.tries, self.resweep_s = timeout, tries, resweep_s
        self._last_sweep = -math.inf
        self._finding: asyncio.Future[str | None] | None = None

    def __repr__(self) -> str:
        return f"WizBulb({self.role!r}, {self.mac!r}, {self.ip!r})"

    def _n(self) -> WizNet:
        return self.net or shared_net()

    def send(self, state: LightState) -> None:
        if self.ip:
            self._n().send(self.ip, {"method": "setPilot", "params": state.to_params()})

    async def request(self, method: str, params: Msg | None = None, *, tries: int | None = None) -> Msg | None:
        if not self.ip:
            return None
        return await self._n().request(self.ip, method, params, timeout=self.timeout,
                                       tries=self.tries if tries is None else tries)

    async def _pilot(self) -> Msg | None:
        r = (await self.request("getPilot") or {}).get("result")
        if not isinstance(r, dict):
            return None
        # A DHCP reshuffle can hand our IP to another bulb.
        if self.mac and r.get("mac") and r["mac"] != self.mac:
            return None
        return r

    async def _find(self, mac: str) -> str | None:
        found = await discover(self.subnets, port=self._n().port, want=mac)
        return found.get(mac, {}).get("ip")

    async def _relocate(self) -> bool:
        if self._finding is None or self._finding.done():
            if not self.mac or time.monotonic() - self._last_sweep < self.resweep_s:
                return False
            self._last_sweep = time.monotonic()
            self._finding = asyncio.ensure_future((self.finder or self._find)(self.mac))
        try:
            ip = await asyncio.shield(self._finding)
        except Exception:
            return False
        if ip:
            self.ip = ip
        return bool(ip)

    async def get_state(self) -> LightState:
        r = await self._pilot()
        if r is None and await self._relocate():
            r = await self._pilot()
        if r is None:
            raise BulbUnreachable(f"{self.role} ({self.mac or '?'} @ {self.ip}) not responding")
        return LightState.from_pilot(r)

    async def set(self, state: LightState, *, attempts: int = 3) -> bool:
        for _ in range(attempts):
            await self.request("setPilot", state.to_params(), tries=1)
            try:
                got = await self.get_state()
            except BulbUnreachable:
                return False
            if matches(state, got):
                return True
        return False


async def open_bulbs(cfg: config_mod.Config, *, net: WizNet | None = None) -> dict[str, WizBulb]:
    subnets, port = cfg.subnets or None, net.port if net else PORT
    known: dict[str, dict] = load_cache()["bulbs"]
    specs = [(b.role, b.mac, b.ip) for b in cfg.bulbs]
    if not specs:
        if not known:
            known = await discover(subnets, port=port)
        specs = [(mac, mac, None) for mac in sorted(known)]
    if any(not hint and mac not in known for _, mac, hint in specs):
        known = known | await discover(subnets, port=port)
    return {role: WizBulb(role, mac, known.get(mac, {}).get("ip") or hint, net=net, subnets=subnets)
            for role, mac, hint in specs}


def describe(s: LightState) -> str:
    parts = ["on" if s.on else "off", f"bri={s.dimming}"]
    if s.scene:
        name = next((k for k, v in SCENES.items() if v == s.scene), s.scene)
        parts.append(f"scene={name}" + (f" speed={s.speed}" if s.speed is not None else ""))
    elif s.rgb is not None:
        parts.append("color={:02x}{:02x}{:02x}".format(*s.rgb))
    elif s.temp is not None:
        parts.append(f"temp={s.temp}K")
    return " ".join(parts)


def pick(bulbs: dict[str, WizBulb], target: str | None) -> list[WizBulb]:
    if not target or target == "all":
        return list(bulbs.values())
    mac = target.replace(":", "").lower()
    hit = [b for b in bulbs.values() if target in (b.role, b.ip) or b.mac == mac]
    if hit:
        return hit
    try:
        ipaddress.ip_address(target)
    except ValueError:
        return []
    return [WizBulb(target, "", target)]


def parse_state(cmd: str, args: list[str]) -> LightState:
    if cmd == "on":
        return LightState(True)
    if cmd == "off":
        return LightState(False)
    if cmd == "bri":
        return LightState(True, dimming=_clamp(int(args[0]), 10, 100))
    if cmd == "temp":
        return LightState(True, temp=_clamp(int(args[0]), 2200, 6500))
    if cmd == "color":
        h = args[0].lstrip("#")
        if len(h) != 6:
            raise ValueError(f"bad colour {args[0]!r}; want RRGGBB")
        rgb = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
        return LightState(True, dimming=int(args[1]) if len(args) > 1 else None, rgb=rgb)
    if cmd == "scene":
        name = args[0].lower().replace(" ", "").replace("-", "")
        if name not in SCENES:
            raise ValueError(f"unknown scene; try: {', '.join(SCENES)}")
        return LightState(True, scene=SCENES[name], speed=int(args[1]) if len(args) > 1 else None)
    raise ValueError(f"unknown command {cmd!r}; see --help")


async def _cli(a: argparse.Namespace, cfg: config_mod.Config) -> int:
    if a.cmd == "discover":
        found = await discover(cfg.subnets or None)
        roles = {b.mac: b.role for b in cfg.bulbs}
        for mac, b in sorted(found.items(), key=lambda kv: ipaddress.ip_address(kv[1]["ip"])):
            print(f"{b['ip']:15} {mac} {b['module'] or '?':20} {roles.get(mac, '')}".rstrip())
        print(f"{len(found)} bulb(s), cached in {CACHE}")
        return 0
    want = None if a.cmd in ("status", "toggle", "raw") else parse_state(a.cmd, a.args)
    bulbs = pick(await open_bulbs(cfg), a.target)
    if not bulbs:
        print(f"no bulb matches {a.target!r}", file=sys.stderr)
        return 1
    if a.cmd == "raw":
        payload = json.loads(a.args[0])
        replies = await asyncio.gather(*(b.request(payload["method"], payload.get("params")) for b in bulbs))
        for b, r in zip(bulbs, replies):
            print(f"{b.role:8} {b.ip:15} {json.dumps(r) if r else 'no reply'}")
        return 0 if all(replies) else 1
    if want is None:
        states = await asyncio.gather(*(b.get_state() for b in bulbs), return_exceptions=True)
        if a.cmd == "status":
            for b, s in zip(bulbs, states):
                print(f"{b.role:8} {b.ip or '?':15} {describe(s) if isinstance(s, LightState) else 'unreachable'}")
            return 0 if all(isinstance(s, LightState) for s in states) else 1
        want = LightState(not any(isinstance(s, LightState) and s.on for s in states))
    oks = await asyncio.gather(*(b.set(want) for b in bulbs))
    for b, ok in zip(bulbs, oks):
        if not ok:
            print(f"{b.role} ({b.ip}): not confirmed", file=sys.stderr)
    return 0 if all(oks) else 1


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="bulbsig bulbs", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-t", "--target", default="all", help="role, mac, ip or all (default: all)")
    p.add_argument("cmd")
    p.add_argument("args", nargs="*")
    a = p.parse_args(argv)
    if a.cmd == "scenes":
        print("\n".join(SCENES))
        return 0
    try:
        return asyncio.run(_cli(a, config_mod.load()))
    except (ValueError, IndexError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
