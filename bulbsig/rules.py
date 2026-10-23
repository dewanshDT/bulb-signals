"""Notification -> signal rules, loaded from TOML (see rules_default.toml)."""
from __future__ import annotations

import argparse
import fnmatch
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import Notification

FIELDS = ("title", "subtitle", "body", "head", "any", "sender", "channel")
NEVER = "(?!)"
DEFAULT_KEY = "{app}:{title}"


@dataclass(frozen=True)
class Match:
    rule: str
    signal: str
    key: str
    priority: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {"rule": self.rule, "signal": self.signal, "key": self.key, "priority": self.priority}


@dataclass
class Rule:
    name: str
    signal: str | None
    apps: list[str] = field(default_factory=list)
    match: dict[str, re.Pattern[str]] = field(default_factory=dict)
    exclude: dict[str, re.Pattern[str]] = field(default_factory=dict)
    key: str = DEFAULT_KEY
    priority: int | None = None


class _Blank(dict):
    def __missing__(self, k: str) -> str:
        return ""


_SENDER = re.compile(r"^([^:\n]{1,60}): ")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def derived(n: Notification) -> dict[str, str]:
    """Slack on macOS: title = workspace; subtitle = "#channel", a private
    channel's bare slug, or the DM sender(s); body = "Sender: text" except in
    1:1 DMs. Web pushes: subtitle = "via <site>"."""
    sub = n.subtitle.strip()
    m = _SENDER.match(n.body)
    if sub.startswith("#"):
        channel = sub
    elif m and _SLUG.match(sub):  # private channels show no '#'; names are lowercase, no spaces
        channel = "#" + sub
    else:
        channel = ""
    if channel or not sub or sub.startswith("via ") or (m and "," in sub):
        sender = m.group(1) if m else ""
    else:
        sender = sub
    return {"sender": sender, "channel": channel}


def _texts(n: Notification) -> dict[str, str]:
    return {
        "title": n.title, "subtitle": n.subtitle, "body": n.body,
        "head": f"{n.title}\n{n.subtitle}",
        "any": f"{n.title}\n{n.subtitle}\n{n.body}",
        **derived(n),
    }


def me_pattern(handles: list[str]) -> str:
    hs = [re.escape(h.strip().lstrip("@")) for h in handles if h.strip().lstrip("@")]
    return "|".join(hs) if hs else NEVER


def _app_ok(app: str, pats: list[str]) -> bool:
    a = app.lower()
    return not pats or any(fnmatch.fnmatchcase(a, p.lower()) for p in pats)


class Rules:
    def __init__(self, rules: list[Rule], *, vip: list[str] | None = None,
                 vip_apps: list[str] | None = None, focus_allow: set[str] | None = None,
                 apps: dict[str, list[str]] | None = None) -> None:
        self.rules = rules
        self.vip = [v for v in (vip or []) if v.strip()]
        self.vip_apps = vip_apps or []
        self.focus_allow: set[str] = set(focus_allow or ())
        self.apps = apps or {}
        self.me: list[str] = []

    @classmethod
    def from_dict(cls, d: dict[str, Any], *, me: list[str] | None = None) -> "Rules":
        handles = list(me) if me is not None else list(d.get("me", {}).get("handles", []))
        me_re = me_pattern(handles)

        def rx(src: str) -> re.Pattern[str]:
            return re.compile(src.replace("{me}", me_re), re.I | re.S)

        aliases = {k: [v] if isinstance(v, str) else list(v) for k, v in d.get("apps", {}).items()}

        def expand(names: list[str] | str | None) -> list[str]:
            if isinstance(names, str):
                names = [names]
            return [p for n in names or [] for p in aliases.get(n, [n])]

        rules = []
        for i, r in enumerate(d.get("rule", [])):
            if "signal" not in r and not r.get("ignore"):
                raise ValueError(f"rule {r.get('name', i)!r} needs signal or ignore = true")
            rules.append(Rule(
                name=r.get("name", f"rule{i}"),
                signal=None if r.get("ignore") else r["signal"],
                apps=expand(r.get("apps")),
                match={f: rx(r[f]) for f in FIELDS if f in r},
                exclude={f: rx(r["not_" + f]) for f in FIELDS if "not_" + f in r},
                key=r.get("key", DEFAULT_KEY),
                priority=r.get("priority"),
            ))
        vip = d.get("vip", {})
        out = cls(rules, vip=list(vip.get("names", [])), vip_apps=expand(vip.get("apps")),
                  focus_allow=set(d.get("focus", {}).get("allow", [])), apps=aliases)
        out.me = handles
        return out

    @classmethod
    def load(cls, path: str | Path, *, me: list[str] | None = None) -> "Rules":
        return cls.from_dict(tomllib.loads(Path(path).read_text()), me=me)

    def _key(self, tpl: str, n: Notification, rule: str) -> str:
        vals = _Blank({k: v for k, v in n.to_json().items() if isinstance(v, (str, int, float))})
        vals.update(derived(n))
        vals["rule"] = rule
        try:
            return tpl.format_map(vals)
        except (ValueError, IndexError, AttributeError):
            return f"{n.app}:{n.title}"

    def match(self, n: Notification) -> Match | None:
        texts = _texts(n)
        if self.vip and _app_ok(n.app, self.vip_apps):
            who = f"{texts['head']}\n{texts['sender']}".lower()
            for name in self.vip:
                if name.lower() in who:
                    return Match("vip", "vip", f"vip:{name.lower()}")
        for r in self.rules:
            if not _app_ok(n.app, r.apps):
                continue
            if all(p.search(texts[f]) for f, p in r.match.items()) and \
                    not any(p.search(texts[f]) for f, p in r.exclude.items()):
                if r.signal is None:
                    return None
                return Match(r.name, r.signal, self._key(r.key, n, r.name), r.priority)
        return None

    def focus_allowed(self, signal: str) -> bool:
        return any(fnmatch.fnmatchcase(signal, p) for p in self.focus_allow)


def config_handles(cfg: Any) -> list[str] | None:
    """[me] handles in config.toml override the rules file's own list."""
    h = cfg.raw.get("me", {}).get("handles")
    return list(h) if h is not None else None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="bulbsig rules")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("test", help="show which rule a notification would match")
    t.add_argument("--app", required=True, help="bundle id, e.g. com.tinyspeck.slackmacgap")
    t.add_argument("--title", default="")
    t.add_argument("--subtitle", default="")
    t.add_argument("--body", default="")
    t.add_argument("--rules", help="rules TOML (default: config rules_file)")
    a = ap.parse_args(argv)
    from . import config
    cfg = config.load()
    rules = Rules.load(Path(a.rules) if a.rules else cfg.rules_file, me=config_handles(cfg))
    n = Notification(source="cli", app=a.app, title=a.title, subtitle=a.subtitle, body=a.body)
    m = rules.match(n)
    if m is None:
        print("no match")
        return 1
    focus = "allowed" if rules.focus_allowed(m.signal) else "suppressed"
    print(f"rule={m.rule} signal={m.signal} key={m.key} priority={m.priority} focus={focus}")
    return 0
