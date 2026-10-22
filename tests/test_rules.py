import pytest

from bulbsig.config import PKG_DIR
from bulbsig.contracts import Notification
from bulbsig.rules import Match, Rules, main

SLACK = "com.tinyspeck.slackmacgap"
ZEN = "app.zen-browser.zen"
ICAL = "com.apple.iCal"


@pytest.fixture(scope="module")
def rules() -> Rules:
    return Rules.load(PKG_DIR / "rules_default.toml")


def n(app: str, title: str = "", body: str = "", subtitle: str = "") -> Notification:
    return Notification(source="test", app=app, title=title, subtitle=subtitle, body=body)


def sig(rules: Rules, *a, **kw) -> str | None:
    m = rules.match(n(*a, **kw))
    return m.signal if m else None


@pytest.mark.parametrize("app,title,body,subtitle,expect", [
    (SLACK, "Alice Smith", "Alice is inviting you to a huddle", "", "ringing"),
    (SLACK, "Huddle", "Bob invited you to a huddle in #eng", "", "ringing"),
    ("us.zoom.xos", "Zoom", "Carol is calling you", "", "ringing"),
    (SLACK, "#incident-db", "Primary is failing over", "", "incident"),
    (SLACK, "Dana in #alerts", "CPU high on api-3", "", "incident"),
    (SLACK, "#eng", "SEV1 declared: checkout is down", "", "incident"),
    (SLACK, "#eng", "we have an outage in eu-west", "", "incident"),
    ("com.pagerduty.ios", "Triggered", "db latency", "", "incident"),
    (SLACK, "GitHub", "CI failed on main: tests", "", "incident"),
    ("com.github.GitHubClient", "Build", "Workflow failing on PR #12", "", "incident"),
    (SLACK, "GitHub", "All checks have passed on #42", "", "success"),
    (ZEN, "GitHub", "dewansh approved your pull request", "", "success"),
    (ICAL, "Standup", "Now", "", "meeting_t0"),
    (ICAL, "Design review", "10:00–10:30", "", "meeting_t5"),
    (ZEN, "Google Calendar", "Standup is starting now · meet.google.com/abc", "", "meeting_t0"),
    (ZEN, "Google Calendar", "Standup starts in 5 minutes", "", "meeting_t5"),
    (SLACK, "Google Calendar", "Event starting in 10 minutes: Planning", "", "meeting_t5"),
    ("com.microsoft.teams2", "Planning", "Meeting starts in 5 min", "", "meeting_t5"),
    (SLACK, "#eng", "@dewansh can you look at this?", "", "mention"),
    (SLACK, "Eve in #random", "Eve mentioned you: lunch?", "", "mention"),
    (SLACK, "#random", "anyone up for lunch?", "", None),
    (SLACK, "Frank", "hey, got a sec?", "", "dm"),
    (SLACK, "Frank", "the deploy is starting", "", "dm"),
    (ZEN, "YouTube", "New video from a channel you follow", "", None),
    (ZEN, "Gmail", "Your build is ready", "", None),
])
def test_default_rules(rules, app, title, body, subtitle, expect):
    assert sig(rules, app, title, body, subtitle=subtitle) == expect


def test_subtitle_channel_counts_as_channel(rules):
    assert sig(rules, SLACK, "Grace", "lol", subtitle="#random") is None
    assert sig(rules, SLACK, "Grace", "@here deploy freeze", subtitle="#eng") == "mention"


def test_keys(rules):
    m = rules.match(n(SLACK, "Frank", "hi"))
    assert m == Match("slack-dm", "dm", "dm:Frank", None)
    assert rules.match(n(SLACK, "#incident", "x")).key == "incident:#incident"


def test_vip_first(rules):
    r = Rules.from_dict({"vip": {"names": ["Big Boss"]}, "rule": [{"name": "all", "signal": "dm"}]})
    m = r.match(n(SLACK, "big boss", "hello"))
    assert (m.rule, m.signal, m.key) == ("vip", "vip", "vip:big boss")
    assert r.match(n(SLACK, "Frank", "Big Boss said hi")).signal == "dm"  # body isn't checked
    assert r.match(n(SLACK, "x", subtitle="from Big Boss")).signal == "vip"


def test_vip_apps():
    r = Rules.from_dict({"apps": {"slack": [SLACK]}, "vip": {"names": ["Boss"], "apps": ["slack"]}})
    assert r.match(n(SLACK, "Boss")).signal == "vip"
    assert r.match(n(ZEN, "Boss")) is None


def test_first_match_wins_and_fields_and():
    r = Rules.from_dict({"rule": [
        {"name": "a", "title": "x", "body": "y", "signal": "one"},
        {"name": "b", "title": "x", "signal": "two"},
        {"name": "c", "any": "x", "signal": "three"},
    ]})
    assert r.match(n("app", "X", "Y")).rule == "a"
    assert r.match(n("app", "x", "z")).rule == "b"
    assert r.match(n("app", "q", "x")).rule == "c"


def test_key_template_priority_and_globs():
    r = Rules.from_dict({"apps": {"web": ["app.*.zen", "org.mozilla.*"]}, "rule": [
        {"name": "w", "apps": ["web"], "signal": "dm", "key": "{source}/{app}/{title}/{nope}/{rule}", "priority": 77},
    ]})
    m = r.match(n("APP.zen-browser.ZEN", "T"))
    assert m == Match("w", "dm", "test/APP.zen-browser.ZEN/T//w", 77)
    assert r.match(n("com.other", "T")) is None


def test_bad_template_falls_back():
    r = Rules.from_dict({"rule": [{"name": "w", "signal": "dm", "key": "{title.nope}"}]})
    assert r.match(n("a", "T")).key == "a:T"


def test_exclude_and_ignore():
    r = Rules.from_dict({"rule": [
        {"name": "skip", "title": "bot", "ignore": True},
        {"name": "m", "not_body": "spam", "signal": "dm"},
    ]})
    assert r.match(n("a", "bot", "x")) is None
    assert r.match(n("a", "Bob", "spam!")) is None
    assert r.match(n("a", "Bob", "hi")).signal == "dm"


def test_focus_allow(rules):
    assert rules.focus_allowed("meeting_t5") and rules.focus_allowed("ringing")
    assert not rules.focus_allowed("dm") and not rules.focus_allowed("mention")


def test_missing_signal_rejected():
    with pytest.raises(ValueError):
        Rules.from_dict({"rule": [{"name": "x", "title": "y"}]})


def test_cli(capsys):
    path = str(PKG_DIR / "rules_default.toml")
    assert main(["test", "--rules", path, "--app", SLACK, "--title", "Frank", "--body", "hi"]) == 0
    assert "signal=dm key=dm:Frank" in capsys.readouterr().out
    assert main(["test", "--rules", path, "--app", ZEN, "--title", "x"]) == 1
    assert "no match" in capsys.readouterr().out
