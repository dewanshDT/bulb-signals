"""Stdlib-only smoke test for the macOS sensor, run under the system python3
(not pytest) so CI proves it works on the interpreter launchd will use."""
from __future__ import annotations

import os
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

MAC_EPOCH = 978307200
HERE = os.path.dirname(os.path.abspath(__file__))
SENSOR = os.path.join(HERE, "..", "bulbsig", "sensor_macos.py")


def main() -> int:
    d = tempfile.mkdtemp()
    db = os.path.join(d, "db")
    c = sqlite3.connect(db)
    c.executescript(
        "CREATE TABLE app (app_id INTEGER PRIMARY KEY, identifier TEXT);"
        "CREATE TABLE record (rec_id INTEGER PRIMARY KEY, app_id INTEGER, uuid BLOB, data BLOB,"
        " delivered_date REAL, presented INTEGER);"
    )
    c.execute("INSERT INTO app(identifier) VALUES ('com.tinyspeck.slackmacgap')")
    now = time.time() - MAC_EPOCH
    blob = plistlib.dumps({"app": "com.tinyspeck.slackmacgap", "date": now,
                           "req": {"titl": "Alice", "subt": "", "body": "smoke"}}, fmt=plistlib.FMT_BINARY)
    c.execute("INSERT INTO record(app_id, uuid, data, delivered_date, presented) VALUES (1, ?, ?, ?, 1)",
              (uuid.uuid4().bytes, blob, now))
    c.commit()
    c.close()
    out = subprocess.run([sys.executable, "-I", SENSOR, "probe", "--db", db, "-n", "5"],
                         capture_output=True, text=True)
    print(out.stdout)
    print(out.stderr, file=sys.stderr)
    if out.returncode != 0 or "Alice" not in out.stdout or "smoke" not in out.stdout:
        print(f"FAIL: probe exit {out.returncode}")
        return 1
    print(f"OK: sensor probe works on {sys.version.split()[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
