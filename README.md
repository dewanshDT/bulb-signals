# bulb-signals

Turns notifications (Slack, Zen browser, meetings) into colour and blink
signals on two WiZ bulbs, and puts the bulbs back exactly as they were.

```text
Mac: sensor_macos ──POST /v1/notify──▶ daemon (rules → engine → WiZ UDP 38899) ──▶ bulbs
ICS calendar ─────────────────────────▶ daemon (countdown, ON AIR)
TUI ◀──────────── /v1/state + /v1/events (SSE) ── daemon
```

## Install

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/bulbsig --help
```

Config lives in `~/.config/bulbsig/config.toml` (or `$BULBSIG_CONFIG`):

```toml
[daemon]
listen = "127.0.0.1:8766"
token = "change-me"            # required if listen is not loopback

[bulbs.desk]
mac = "9877d5a438a2"           # ip is a hint; bulbs are found by MAC
ip = "192.168.1.9"

[bulbs.room]
mac = "9877d5e910a8"
ip = "192.168.1.14"

[me]
handles = ["Dewansh"]          # your Slack @handle(s); add "here", "channel" to count those

[vip]
names = ["Tony", "Yermie"]     # whole-word, case-insensitive; sender gets the gold sparkle

[quiet]
start = "23:00"
end = "08:00"
max_bri = 10

[calendar]
ics = ["https://calendar.google.com/calendar/ical/.../basic.ics"]   # Google "secret address in iCal format"
push_lead_min = 10             # Google Calendar reminder lead, used when a push has no readable time
# on_air_until_ack = true
```

## Run

```sh
bulbsig bulbs discover | status | on | off | color ff8800 | temp 2700 | scene fireplace   [-t desk|room|all]
bulbsig daemon                     # engine + rules + calendar + HTTP API
bulbsig tui                        # dashboard: space ack · p play · m mute · o ON AIR · f focus · / filter
bulbsig preview dm mention         # play signals directly (daemon must be stopped) or --via-daemon
bulbsig patterns list | validate
bulbsig rules test --app com.tinyspeck.slackmacgap --title Alice --body "hi"
bulbsig calendar --hours 24
```

Anything can raise a signal:

```sh
curl -H "Authorization: Bearer $TOKEN" -d '{"signal":"success"}' http://127.0.0.1:8766/v1/signal
curl -H "Authorization: Bearer $TOKEN" -d '{"source":"ci","app":"github","title":"CI failed"}' http://127.0.0.1:8766/v1/notify
```

## On the Mac

1. Give Terminal **Full Disk Access** (System Settings → Privacy & Security →
   Full Disk Access), then check that notifications can be read:

   ```sh
   sw_vers
   /usr/bin/python3 bulbsig/sensor_macos.py probe --schema -n 10
   /usr/bin/python3 bulbsig/sensor_macos.py probe --raw -n 3 > probe-raw.txt
   ```

   | exit code | meaning |
   |---|---|
   | 0 | records decoded |
   | 2 | no DB found |
   | 3 | no Full Disk Access |
   | 4 | schema not understood: send `probe-raw.txt` |

2. Watch live without the daemon:
   `/usr/bin/python3 bulbsig/sensor_macos.py sensor --dry-run`

3. Install the LaunchAgents with `deploy/macos/install.sh --all` (or
   `--sensor-only` when the daemon runs elsewhere; `--uninstall` removes
   them). The script prints the exact Python binary that needs Full Disk
   Access. Logs are written to `~/Library/Logs/bulbsig/`.

The sensor is stdlib-only and runs on the system Python. Only the daemon and
the TUI need the venv.

## Daemon on a VM / LXC

`deploy/linux/bulbsig-daemon.service` is the systemd unit. Set `listen` to the
LAN address and set a token. Point the Mac sensor at it with `[daemon] url`.

## Customising

- **Signals:** `bulbsig/patterns_default.toml`. Copy it and set
  `patterns_file = "..."` in config. Run `bulbsig patterns validate`.
- **Rules:** `bulbsig/rules_default.toml`, overridden with
  `rules_file = "..."`. The VIP list, the focus allow-list and the app aliases
  live there.
- **Slack rules use the shapes seen on macOS 27.0.** The title is the
  workspace. The subtitle is `#channel` or the DM sender. The body is
  `Sender: text` in channels.
  - Channel chatter, Sentry alerts and other people's mentions stay dark.
  - Mentions of your `[me] handles`, DMs, huddle invites and Slackbot
    reminders light up.
  - Zen web pushes carry `via <site>` in the subtitle; meetings are matched
    by site.
- **Meetings come from two sources, merged by title and start time:**
  - **Google Calendar pushes in Zen.** The reminder (10 min before by
    default) is enough. The daemon reads the start/end from its text, or
    assumes `push_lead_min`, then runs the whole countdown
    (T-10 → T-5 → T-2 → T-1 → now) and turns ON AIR on and off.
  - **The `[calendar] ics` feed**, if set.

## Tests

```sh
.venv/bin/pytest                      # unit tests
.venv/bin/pytest --hardware -s        # also drives the real bulbs, restoring them afterwards
```

On macOS the WiZ unit tests need extra loopback addresses first:
`for i in 2 3 4 5 6 7; do sudo ifconfig lo0 alias 127.0.0.$i up; done`.
