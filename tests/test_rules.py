import pytest

from bulbsig.config import PKG_DIR
from bulbsig.contracts import Notification
from bulbsig.rules import Match, Rules, main

SLACK = "com.tinyspeck.slackmacgap"
ZEN = "app.zen-browser.zen"
ICAL = "com.apple.iCal"


@pytest.fixture(scope="module")
def rules() -> Rules:
    return Rules.load(PKG_DIR / "rules_default.toml", me=["Dewansh"])


def n(app: str, title: str = "", body: str = "", subtitle: str = "") -> Notification:
    return Notification(source="test", app=app, title=title, subtitle=subtitle, body=body)


def sig(rules: Rules, *a, **kw) -> str | None:
    m = rules.match(n(*a, **kw))
    return m.signal if m else None


WS = "acme"  # Slack puts the workspace in the title


# Shapes copied from real macOS 27.0 records (text is synthetic).
@pytest.mark.parametrize("app,title,subtitle,body,expect", [
    (SLACK, WS, "#random", "Sam: lunch?", None),
    (SLACK, WS, "#pr-reviews", "Sam: @Alex can you review this?", None),
    (SLACK, WS, "#pr-reviews", "Sam: @dewansh can you review this?", "mention"),
    (SLACK, WS, "#pr-reviews", "Sam: @Dewansh RE: tabs PR", "mention"),
    (SLACK, WS, "#eng", "Sam: ping @dewanshx", None),
    (SLACK, WS, "#eng", "Sam: I'll call you now, we know the deploy is starting", None),
    (SLACK, WS, "#auto-sentry", "Sentry: 🔴 TRPCError\nInternal server error", None),
    (SLACK, WS, "#eng", "Sentry: 🔴 TypeError @Dewansh", None),
    (SLACK, WS, "Sam Lee", "hey, got a minute?", "dm"),
    (SLACK, WS, "Sharooq", "bro what discussion is this", "dm"),
    (SLACK, WS, "internal-dev", "Vivek: @Sharooq can you check", None),
    (SLACK, WS, "internal-dev", "Vivek: @Dewansh can you check", "mention"),
    (SLACK, WS, "internal-dev", "Vivek: deploy is done", None),
    (SLACK, WS, "Sam Lee", "Note: call me later", "dm"),
    (SLACK, WS, "Sam Lee, Ana", "Ana: thoughts on this?", "dm"),
    (SLACK, WS, "Sam Lee", "Sam Lee is inviting you to a huddle", "ringing"),
    (SLACK, WS, "#eng", "Sam: huddle in 5?", None),
    (SLACK, WS, "Slackbot", "Reminder: submit timesheet", "dm"),
    (SLACK, WS, "#incident-db", "Pat: primary is failing over", "incident"),
    (SLACK, WS, "#alerts", "Bot: CPU high on api-3", "incident"),
    (ZEN, "Session ready", "via claude.ai", "Your Claude Code session is ready", None),
    (ZEN, "Standup", "via calendar.google.com", "Now · Google Meet", "meeting_t0"),
    (ZEN, "Standup", "via calendar.google.com", "10:00 – 10:30am", "meeting_t5"),
    (ZEN, "Sam joined", "via meet.google.com", "Sam is waiting for you", "meeting_t0"),
    (ZEN, "CI", "via github.com", "Workflow failed on main", "incident"),
    (ZEN, "PR", "via github.com", "Alex approved your pull request", "success"),
    (ZEN, "Gmail", "via mail.google.com", "Your build failed", None),
    ("notion.id", "Roadmap", "", "Alex mentioned you in Roadmap", "mention"),
    ("com.pagerduty.ios", "Triggered", "", "db latency", "incident"),
    (ICAL, "Standup", "", "Now", "meeting_t0"),
    (ICAL, "Design review", "", "10:00–10:30", "meeting_t5"),
    ("us.zoom.xos", "Zoom", "", "Carol is calling you", "ringing"),
])
def test_default_rules(rules, app, title, subtitle, body, expect):
    assert sig(rules, app, title, body, subtitle=subtitle) == expect


def test_derived_sender_and_channel():
    from bulbsig.rules import derived
    assert derived(n(SLACK, WS, "Sam: hi", "#eng")) == {"sender": "Sam", "channel": "#eng"}
    assert derived(n(SLACK, WS, "hi", "Sam Lee")) == {"sender": "Sam Lee", "channel": ""}
    assert derived(n(ZEN, "T", "x", "via claude.ai")) == {"sender": "", "channel": ""}
    assert derived(n(SLACK, WS, "Vivek: hi", "internal-dev")) == {"sender": "Vivek", "channel": "#internal-dev"}
    assert derived(n(SLACK, WS, "Ana: hi", "Sam Lee, Ana")) == {"sender": "Ana", "channel": ""}


def test_keys(rules):
    assert rules.match(n(SLACK, WS, "hi", "Sam Lee")) == Match("slack-dm", "dm", "dm:Sam Lee", None)
    assert rules.match(n(SLACK, WS, "Sam: @Dewansh x", "#eng")).key == "mention:#eng"
    assert rules.match(n(SLACK, WS, "Sam Lee is inviting you to a huddle", "Sam Lee")).key == "call:Sam Lee"
    assert rules.match(n(SLACK, WS, "Pat: x", "#incident-db")).key == "incident:#incident-db"


def test_no_handles_means_no_mentions():
    r = Rules.load(PKG_DIR / "rules_default.toml", me=[])
    assert r.match(n(SLACK, WS, "Sam: @dewansh hi", "#eng")) is None
    r = Rules.load(PKG_DIR / "rules_default.toml", me=["@dewansh", "here"])
    assert r.match(n(SLACK, WS, "Sam: @here deploy freeze", "#eng")).signal == "mention"


def test_vip_by_channel_sender():
    r = Rules.from_dict({"vip": {"names": ["Big Boss"]}, "rule": [{"name": "all", "signal": "dm"}]})
    assert r.match(n(SLACK, WS, "Big Boss: need you", "#eng")).signal == "vip"
    assert r.match(n(SLACK, WS, "Sam: Big Boss said hi", "#eng")).signal == "dm"


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
    assert main(["test", "--rules", path, "--app", SLACK, "--title", WS, "--subtitle", "Frank", "--body", "hi"]) == 0
    assert "signal=dm key=dm:Frank" in capsys.readouterr().out
    assert main(["test", "--rules", path, "--app", ZEN, "--title", "x"]) == 1
    assert "no match" in capsys.readouterr().out
