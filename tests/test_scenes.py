import pytest

from bulbsig import scenes
from bulbsig.contracts import LightState
from bulbsig.scenes import Scene, SceneError, SceneStore


def test_parse_and_roundtrip():
    text = '''
[scene."Late  Night"]
desk = { on = false }
room = { color = "1F4DFF", bri = 10 }
[scene.cozy]
desk = { scene = "cozy", bri = 40, speed = 80 }
room = { temp = 2200 }
'''
    s = scenes.loads(text)
    assert list(s) == ["late night", "cozy"]
    assert s["late night"].bulbs == {"desk": {"on": False}, "room": {"on": True, "rgb": (31, 77, 255), "bri": 10}}
    assert s["cozy"].bulbs["desk"] == {"on": True, "scene": 6, "speed": 80, "bri": 40}
    assert scenes.loads(scenes.dumps(s)) == s
    j = s["cozy"].to_json()
    assert j["bulbs"]["desk"]["scene"] == "cozy" and j["bulbs"]["room"]["swatch"].startswith("#ff")


@pytest.mark.parametrize("text", [
    '[scene.x]\ndesk = { color = "ff0000", temp = 3000 }\n',
    '[scene.x]\ndesk = { bri = 5 }\n',
    '[scene.x]\ndesk = { scene = "nope" }\n',
    '[scene.x]\ndesk = { hue = 3 }\n',
    '[scene.x]\ndesk = 3\n',
    '[scene.x]\n',
    '[scene."!!"]\ndesk = { on = false }\n',
])
def test_parse_rejects(text):
    with pytest.raises(SceneError):
        scenes.loads(text)


def test_change_of():
    assert scenes.change_of(LightState(on=True, dimming=40, rgb=(1, 2, 3))) == {"on": True, "bri": 40, "rgb": (1, 2, 3)}
    assert scenes.change_of(LightState(on=True, dimming=50, scene=5, speed=100)) == \
        {"on": True, "bri": 50, "scene": 5, "speed": 100}
    assert scenes.change_of(LightState(on=False, dimming=30, temp=2700)) == {"on": False, "bri": 30, "temp": 2700}


def test_store_defaults_then_writes(tmp_path):
    path = tmp_path / "cfg" / "scenes.toml"
    st = SceneStore(path)
    assert "evening" in st.scenes and "all off" in st.scenes and not path.exists()
    assert st.put(Scene("reading", {"desk": {"on": True, "temp": 4000, "bri": 80}})) is False
    assert path.exists() and "reading" in SceneStore(path).scenes
    assert st.put(Scene("reading", {"desk": {"on": False}})) is True
    assert SceneStore(path).get("Reading").bulbs == {"desk": {"on": False}}
    assert st.delete("ALL OFF") and not st.delete("nope")
    assert "all off" not in SceneStore(path).scenes
    assert st.get("!!") is None
    assert not list(path.parent.glob(".scenes-*"))


def test_store_bad_file(tmp_path):
    p = tmp_path / "scenes.toml"
    p.write_text("[scene.x]\ndesk = { bri = 500 }\n")
    with pytest.raises(SceneError):
        SceneStore(p)


def test_cli(monkeypatch, capsys):
    calls = []

    def fake(cfg, method, path, body=None, timeout=5):
        calls.append((method, path, body))
        if path == "/v1/scenes" and method == "GET":
            return [{"name": "movie", "bulbs": {"desk": {"on": False}, "room": {"on": True, "color": "1f4dff", "bri": 10}}}]
        if method == "POST" and path == "/v1/scenes":
            return {"scene": {"name": "late night", "bulbs": {"desk": {"on": True, "temp": 2700}}}, "replaced": False}
        return {}

    monkeypatch.setattr("bulbsig.wiz.daemon_request", fake)
    assert scenes.main([]) == 0
    assert "movie" in capsys.readouterr().out
    assert scenes.main(["apply", "all", "off"]) == 0
    assert scenes.main(["save", "late", "night"]) == 0
    assert scenes.main(["delete", "movie"]) == 0
    assert scenes.main(["apply"]) == 2
    assert calls[1:] == [("POST", "/v1/scenes/all%20off/apply", {}), ("POST", "/v1/scenes", {"name": "late night"}),
                         ("DELETE", "/v1/scenes/movie", None)]
