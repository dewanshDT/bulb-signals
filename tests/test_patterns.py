import pytest

from bulbsig import patterns
from bulbsig.contracts import PRIORITY, LightState
from bulbsig.config import PKG_DIR

DEFAULT = PKG_DIR / "patterns_default.toml"


@pytest.fixture(scope="module")
def ps():
    return patterns.load(DEFAULT)


def test_default_has_every_required_signal_and_mode(ps):
    for n in patterns.REQUIRED_SIGNALS:
        assert n in ps.names()
    assert "on_air" in ps.modes
    assert ps.modes["on_air"].looks["room"].rgb == (255, 0, 0)


def test_default_validates_clean(ps):
    assert ps.validate() == []


def test_pattern_fields(ps):
    ring = ps.get("ringing")
    assert ring.priority == PRIORITY["ringing"]
    assert ring.repeat == "until_ack" and ring.repeating
    assert ring.swatch.startswith("#") and len(ring.swatch) == 7
    dm = ps.get("dm")
    assert dm.repeat == 1 and not dm.repeating
    assert dm.touches("desk") and not dm.touches("room")
    assert ps.get("meeting_t0").allow_fast
    assert ps.get("meeting_t10").priority == PRIORITY["ambient"]


def test_every_step_respects_floor_unless_fast(ps):
    for p in ps.patterns.values():
        if p.allow_fast:
            continue
        for s in p.steps:
            sub = s.ms / s.ramp if s.ramp > 1 else s.ms
            assert sub == 0 or sub >= patterns.MIN_STEP_MS, (p.name, s)


def test_unknown_signal_keyerror(ps):
    with pytest.raises(KeyError):
        ps.get("nope")


def test_fast_step_warns_without_opt_in():
    text = """
[pattern.a]
steps = [{ color = "ff0000", ms = 100 }]
[pattern.b]
allow_fast = true
steps = [{ color = "ff0000", ms = 100 }]
"""
    warns = [w for w in patterns.loads(text).validate() if w.startswith("warning: a:") or w.startswith("warning: b:")]
    assert len(warns) == 1 and "floor" in warns[0] and "a:" in warns[0]


def test_validation_errors_and_unknown_keys():
    text = """
[pattern.x]
repeat = "until_ack"
quiet = "loud"
bogus = 1
steps = [{ bulbs = "lamp", color = "00ff00", bri = 150, ms = 300 }]
"""
    out = patterns.loads(text).validate()
    joined = "\n".join(out)
    assert "error: x: quiet" in joined
    assert "error: x: step 0: bri 150" in joined
    assert "unknown bulb role 'lamp'" in joined
    assert "unknown key 'bogus'" in joined
    assert "until_ack without max_s" in joined


def test_parse_errors_raise():
    with pytest.raises(patterns.PatternError):
        patterns.loads('[pattern.x]\nsteps = [{ color = "zzzzzz" }]')
    with pytest.raises(patterns.PatternError):
        patterns.loads('[pattern.x]\npriority = "urgent"\nsteps = [{ color = "ff0000" }]')


def test_priority_and_swatch_defaults():
    ps = patterns.loads('[pattern.dm]\nsteps = [{ color = "#00FF00" }]\n[pattern.z]\nsteps = [{ temp = 3000 }]')
    assert ps.get("dm").priority == PRIORITY["dm"]
    assert ps.get("dm").swatch == "#00ff00"
    assert ps.get("z").priority == PRIORITY["default"]


def test_look_apply_keeps_unset_fields():
    cur = LightState(on=True, dimming=40, temp=3000)
    assert patterns.Look(bri=80).apply(cur) == LightState(on=True, dimming=80, temp=3000)
    assert patterns.Look(temp=5000).apply(cur).dimming == 40
    assert patterns.Look(off=True).apply(cur) == LightState(on=False)


def test_lerp_state():
    a = LightState(on=True, dimming=20, temp=3000)
    b = LightState(on=True, dimming=60, temp=5000)
    mid = patterns.lerp_state(a, b, 0.5)
    assert mid.temp == 4000 and mid.dimming == 40
    c = LightState(on=True, dimming=100, rgb=(255, 0, 0))
    assert patterns.lerp_state(a, c, 0.5).rgb is not None
    assert patterns.lerp_state(a, c, 1.0) == c


def test_cli_list_and_validate(capsys, tmp_path):
    assert patterns.main(["list", str(DEFAULT)]) == 0
    out = capsys.readouterr().out
    assert "ringing" in out and "on_air" in out
    assert patterns.main(["validate", str(DEFAULT)]) == 0
    bad = tmp_path / "bad.toml"
    bad.write_text('[pattern.x]\nquiet = "loud"\nsteps = [{ color = "ff0000" }]')
    assert patterns.main(["validate", str(bad)]) == 1
    bad.write_text("[pattern.x\n")
    assert patterns.main(["validate", str(bad)]) == 1
