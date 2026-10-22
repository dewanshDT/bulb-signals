"""`bulbsig <command> ...` — dispatches to each module's own main(argv)."""
from __future__ import annotations

import importlib
import sys

COMMANDS = {
    "bulbs": ("bulbsig.wiz", "main", "control/inspect bulbs (discover, status, on, off, color, ...)"),
    "daemon": ("bulbsig.server", "main", "run the bulb daemon (engine + HTTP API + calendar)"),
    "preview": ("bulbsig.preview", "main", "play a signal on the real bulbs (direct, or via the daemon)"),
    "patterns": ("bulbsig.patterns", "main", "list / validate signal patterns"),
    "rules": ("bulbsig.rules", "main", "test which rule a notification would match"),
    "calendar": ("bulbsig.calendar_ics", "main", "show upcoming meetings from the ICS feeds"),
    "probe": ("bulbsig.sensor_macos", "probe_main", "macOS: dump recent Notification Center records"),
    "sensor": ("bulbsig.sensor_macos", "main", "macOS: forward notifications to the daemon"),
    "tui": ("bulbsig.tui", "main", "terminal UI (client of the daemon)"),
}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("usage: bulbsig <command> [args]\n")
        for name, (_, _, help_) in COMMANDS.items():
            print(f"  {name:10} {help_}")
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd not in COMMANDS:
        print(f"unknown command {cmd!r}; try --help", file=sys.stderr)
        return 2
    mod, fn, _ = COMMANDS[cmd]
    return getattr(importlib.import_module(mod), fn)(rest) or 0


if __name__ == "__main__":
    sys.exit(main())
