import pytest

from bulbsig import wiz


@pytest.mark.parametrize("cmd,args,body", [
    ("on", [], {"on": True}),
    ("off", [], {"on": False}),
    ("bri", ["40"], {"bri": 40}),
    ("temp", ["9000"], {"temp": 6500}),
    ("color", ["#FF8800"], {"color": "ff8800"}),
    ("color", ["ff8800", "120"], {"color": "ff8800", "bri": 100}),
    ("scene", ["fireplace", "120"], {"scene": 5, "speed": 120}),
])
def test_light_body(cmd, args, body):
    assert wiz.light_body(cmd, args) == body


def test_light_body_rejects():
    with pytest.raises(ValueError):
        wiz.light_body("color", ["red"])
    with pytest.raises(ValueError):
        wiz.light_body("scene", ["nope"])


def test_main_uses_daemon_when_up(monkeypatch):
    seen = []
    monkeypatch.setattr(wiz, "daemon_up", lambda cfg: True)
    monkeypatch.setattr(wiz, "via_daemon", lambda cfg, cmd, args, target: seen.append((cmd, target)) or 0)
    monkeypatch.setattr(wiz.asyncio, "run", lambda coro: coro.close() or seen.append("direct") or 0)
    assert wiz.main(["color", "ff0000"]) == 0
    assert wiz.main(["--direct", "color", "ff0000"]) == 0
    assert wiz.main(["status"]) == 0
    assert wiz.main(["-t", "192.168.1.9", "on"]) == 0
    assert seen == [("color", "all"), "direct", "direct", "direct"]
