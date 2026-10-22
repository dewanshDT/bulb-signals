import asyncio
import json

import pytest

from bulbsig import wiz
from bulbsig.config import BulbCfg, Config
from bulbsig.contracts import BulbUnreachable, LightState


class FakeBulb(asyncio.DatagramProtocol):
    def __init__(self, mac: str, pilot: dict | None = None):
        self.mac = mac
        self.pilot = pilot or {"state": True, "sceneId": 0, "temp": 2700, "dimming": 80}
        self.drop = 0
        self.ignore_sets = 0
        self.received: list[dict] = []
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def apply(self, p: dict) -> None:
        if "state" in p:
            self.pilot["state"] = p["state"]
        if "dimming" in p:
            self.pilot["dimming"] = p["dimming"]
        if "sceneId" in p:
            for k in ("r", "g", "b", "c", "w", "temp"):
                self.pilot.pop(k, None)
            self.pilot["sceneId"], self.pilot["speed"] = p["sceneId"], p.get("speed", 100)
        elif "r" in p:
            self.pilot = {k: v for k, v in self.pilot.items() if k not in ("temp", "speed")}
            self.pilot.update(sceneId=0, r=p["r"], g=p["g"], b=p["b"], c=0, w=0)
        elif "temp" in p:
            for k in ("r", "g", "b", "c", "w", "speed"):
                self.pilot.pop(k, None)
            self.pilot.update(sceneId=0, temp=round(p["temp"] / 100) * 100)

    def datagram_received(self, data, addr):
        msg = json.loads(data)
        self.received.append(msg)
        if self.drop:
            self.drop -= 1
            return
        m, p = msg["method"], msg.get("params", {})
        if m == "getPilot":
            res = {"mac": self.mac, "rssi": -60, **self.pilot}
        elif m == "setPilot":
            if self.ignore_sets:
                self.ignore_sets -= 1
            else:
                self.apply(p)
            res = {"success": True}
        elif m == "getSystemConfig":
            res = {"mac": self.mac, "moduleName": "FAKE_SHRGB"}
        else:
            return
        self.transport.sendto(json.dumps({"method": m, "env": "pro", "result": res}).encode(), addr)


@pytest.fixture
async def lan(tmp_path, monkeypatch):
    monkeypatch.setattr(wiz, "CACHE", tmp_path / "bulbs-cache.json")
    loop = asyncio.get_running_loop()
    a = FakeBulb("aaaaaaaaaaa1")
    ta, _ = await loop.create_datagram_endpoint(lambda: a, local_addr=("127.0.0.2", 0))
    port = ta.get_extra_info("sockname")[1]
    b = FakeBulb("bbbbbbbbbbb2", {"state": False, "sceneId": 0, "r": 255, "g": 0, "b": 0, "c": 0, "w": 0, "dimming": 50})
    tb, _ = await loop.create_datagram_endpoint(lambda: b, local_addr=("127.0.0.5", port))
    net = await wiz.WizNet(port=port, bind="127.0.0.1").start()
    yield net, a, b
    net.close()
    ta.close()
    tb.close()


def bulb(net, mac="aaaaaaaaaaa1", ip="127.0.0.2", **kw):
    kw.setdefault("timeout", 0.2)
    return wiz.WizBulb("desk", mac, ip, net=net, **kw)


async def test_get_state_parses_white_and_rgb(lan):
    net, a, b = lan
    assert await bulb(net).get_state() == LightState(True, dimming=80, temp=2700)
    assert await bulb(net, b.mac, "127.0.0.5").get_state() == LightState(False, dimming=50, rgb=(255, 0, 0))


async def test_get_state_scene_keeps_speed(lan):
    net, a, _ = lan
    a.pilot = {"state": True, "sceneId": 5, "speed": 120, "dimming": 40}
    assert await bulb(net).get_state() == LightState(True, dimming=40, scene=5, speed=120)


async def test_send_is_fire_and_forget(lan):
    net, a, _ = lan
    bulb(net).send(LightState(True, dimming=30, rgb=(1, 2, 3)))
    wiz.WizBulb("x", "", "203.0.113.9", net=net).send(LightState(False))
    wiz.WizBulb("y", "", None, net=net).send(LightState(False))
    await asyncio.sleep(0.05)
    assert a.received[-1] == {"method": "setPilot", "params": {"state": True, "dimming": 30, "r": 1, "g": 2, "b": 3}}
    assert (a.pilot["r"], a.pilot["dimming"]) == (1, 30)


async def test_send_before_net_started(lan):
    _, a, _ = lan
    net = wiz.WizNet(port=lan[0].port, bind="127.0.0.1")
    bulb(net).send(LightState(False))
    await asyncio.sleep(0.05)
    assert a.pilot["state"] is False
    net.close()


async def test_get_state_retries_lost_replies(lan):
    net, a, _ = lan
    a.drop = 2
    assert (await bulb(net).get_state()).on
    assert sum(m["method"] == "getPilot" for m in a.received) == 3


async def test_set_reads_back_and_retries(lan):
    net, a, _ = lan
    a.ignore_sets = 1
    want = LightState(True, dimming=60, rgb=(0, 128, 255))
    assert await bulb(net).set(want)
    assert sum(m["method"] == "setPilot" for m in a.received) == 2
    assert await bulb(net).get_state() == want


async def test_set_gives_up(lan):
    net, a, _ = lan
    a.ignore_sets = 99
    assert not await bulb(net).set(LightState(True, rgb=(9, 9, 9)), attempts=2)
    assert sum(m["method"] == "setPilot" for m in a.received) == 2


async def test_set_restores_every_kind(lan):
    net, a, _ = lan
    bl = bulb(net)
    for s in (LightState(True, 70, temp=4000), LightState(True, 30, scene=5, speed=150),
              LightState(True, 100, rgb=(255, 0, 0)), LightState(False)):
        assert await bl.set(s)
        snap = await bl.get_state()
        assert await bl.set(LightState(False) if s.on else LightState(True, 50, rgb=(0, 255, 0)))
        assert await bl.set(snap)
        after = await bl.get_state()
        assert wiz.matches(snap, after)
        if s.on:
            assert after == snap


async def test_rediscovery_via_injected_finder(lan):
    net, a, _ = lan
    calls = []

    async def finder(mac):
        calls.append(mac)
        return "127.0.0.2"

    bl = bulb(net, ip="127.0.0.9", finder=finder)
    assert (await bl.get_state()).dimming == 80
    assert bl.ip == "127.0.0.2" and calls == ["aaaaaaaaaaa1"]


async def test_wrong_mac_at_ip_triggers_rediscovery(lan):
    net, _, b = lan
    found = []

    async def finder(mac):
        found.append(mac)
        return "127.0.0.5"

    bl = bulb(net, mac=b.mac, ip="127.0.0.2", finder=finder)
    assert not (await bl.get_state()).on
    assert bl.ip == "127.0.0.5"


async def test_rediscovery_via_sweep_updates_cache(lan):
    net, _, b = lan
    bl = bulb(net, mac=b.mac, ip="127.0.0.9", subnets=["127.0.0.0/29"])
    assert (await bl.get_state()).rgb == (255, 0, 0)
    assert bl.ip == "127.0.0.5"
    assert wiz.load_cache()["bulbs"][b.mac]["ip"] == "127.0.0.5"


async def test_unreachable(lan):
    net, _, _ = lan

    async def finder(mac):
        return None

    with pytest.raises(BulbUnreachable):
        await bulb(net, ip="127.0.0.9", finder=finder).get_state()
    with pytest.raises(BulbUnreachable):
        await bulb(net, mac="", ip="127.0.0.9").get_state()
    assert not await bulb(net, ip="127.0.0.9", finder=finder).set(LightState(True))


async def test_rediscovery_rate_limited(lan):
    net, _, _ = lan
    calls = []

    async def finder(mac):
        calls.append(mac)
        return None

    bl = bulb(net, ip="127.0.0.9", finder=finder, tries=1)
    for _ in range(2):
        with pytest.raises(BulbUnreachable):
            await bl.get_state()
    assert len(calls) == 1


async def test_discover_and_open_bulbs(lan):
    net, a, b = lan
    found = await wiz.discover(["127.0.0.0/29"], port=net.port, wait=0.3)
    assert found == {a.mac: {"ip": "127.0.0.2", "module": "FAKE_SHRGB"},
                     b.mac: {"ip": "127.0.0.5", "module": "FAKE_SHRGB"}}
    cfg = Config(path=wiz.CACHE, bulbs=[BulbCfg("desk", a.mac, "127.0.0.7"), BulbCfg("room", b.mac)])
    bulbs = await wiz.open_bulbs(cfg, net=net)
    assert {r: x.ip for r, x in bulbs.items()} == {"desk": "127.0.0.2", "room": "127.0.0.5"}
    wiz.CACHE.unlink()
    bulbs = await wiz.open_bulbs(Config(path=wiz.CACHE, bulbs=[BulbCfg("desk", a.mac, "127.0.0.2")]), net=net)
    assert bulbs["desk"].ip == "127.0.0.2"


async def test_open_bulbs_discovers_missing(lan):
    net, a, b = lan
    cfg = Config(path=wiz.CACHE, bulbs=[BulbCfg("room", b.mac)], subnets=["127.0.0.0/29"])
    bulbs = await wiz.open_bulbs(cfg, net=net)
    assert bulbs["room"].ip == "127.0.0.5"


def test_matches_tolerances():
    m = wiz.matches
    assert m(LightState(False), LightState(False, 50, rgb=(1, 2, 3)))
    assert not m(LightState(False), LightState(True))
    assert m(LightState(True, 50, temp=2700), LightState(True, 50, temp=2730))
    assert not m(LightState(True, 50, temp=2700), LightState(True, 50, temp=2800))
    assert not m(LightState(True, 50, temp=2700), LightState(True, 51, temp=2700))
    assert m(LightState(True, 5), LightState(True, 10, temp=3000))
    assert m(LightState(True, scene=5, speed=100), LightState(True, 40, scene=5))
    assert not m(LightState(True, rgb=(1, 2, 3)), LightState(True, rgb=(1, 2, 4)))


def test_parse_state_and_describe():
    assert wiz.parse_state("color", ["#ff8800", "40"]) == LightState(True, 40, rgb=(255, 136, 0))
    assert wiz.parse_state("scene", ["Fire-place", "120"]) == LightState(True, scene=5, speed=120)
    assert wiz.parse_state("temp", ["9000"]) == LightState(True, temp=6500)
    assert wiz.describe(LightState(True, 30, scene=5)) == "on bri=30 scene=fireplace"
    with pytest.raises(ValueError):
        wiz.parse_state("scene", ["nope"])


def test_pick():
    bulbs = {"desk": wiz.WizBulb("desk", "98:77:d5:a4:38:a2", "10.0.0.9"),
             "room": wiz.WizBulb("room", "9877d5e910a8", "10.0.0.14")}
    assert [b.role for b in wiz.pick(bulbs, "all")] == ["desk", "room"]
    assert [b.role for b in wiz.pick(bulbs, "9877D5A438A2")] == ["desk"]
    assert [b.role for b in wiz.pick(bulbs, "10.0.0.14")] == ["room"]
    assert wiz.pick(bulbs, "10.0.0.99")[0].ip == "10.0.0.99"
    assert wiz.pick(bulbs, "kitchen") == []


def test_cli_scenes(capsys):
    assert wiz.main(["scenes"]) == 0
    assert "fireplace" in capsys.readouterr().out
