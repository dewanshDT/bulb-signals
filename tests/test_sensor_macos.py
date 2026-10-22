from __future__ import annotations

import ast
import json
import os
import plistlib
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid as uuidlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from bulbsig import sensor_macos as sm

SRC = Path(sm.__file__)
MAC_EPOCH = 978307200.0

SCHEMA = """
CREATE TABLE app (app_id INTEGER PRIMARY KEY, identifier VARCHAR, badge INTEGER NULL);
CREATE TABLE record (rec_id INTEGER PRIMARY KEY, app_id INTEGER, uuid BLOB, data BLOB,
  request_date REAL, request_last_date REAL, delivered_date REAL, presented Bool,
  style INTEGER, snooze_fire_date REAL);
"""


def blob(app: str, title: str = "", subtitle: str = "", body: str = "", iden: str = "", date: float = 0.0) -> bytes:
    req = {"titl": title, "subt": subtitle, "body": body}
    if iden:
        req["iden"] = iden
    return plistlib.dumps({"app": app, "date": date, "req": req, "srce": ["x"]}, fmt=plistlib.FMT_BINARY)


class NCDB:
    """Synthetic Notification Center DB; the writer connection stays open like usernoted's."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.c = sqlite3.connect(str(path), isolation_level=None)
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.executescript(SCHEMA)

    def app_id(self, ident: str) -> int:
        r = self.c.execute("SELECT app_id FROM app WHERE identifier=?", (ident,)).fetchone()
        if r:
            return r[0]
        return self.c.execute("INSERT INTO app(identifier) VALUES (?)", (ident,)).lastrowid

    def add(self, app: str = "com.tinyspeck.slackmacgap", title: str = "t", subtitle: str = "", body: str = "",
            data: bytes | None = None, uid: bytes | None = None, delivered: float | None = None,
            iden: str = "", app_row: bool = True) -> int:
        delivered = time.time() - MAC_EPOCH if delivered is None else delivered
        data = blob(app, title, subtitle, body, iden, delivered) if data is None else data
        return self.c.execute(
            "INSERT INTO record(app_id, uuid, data, delivered_date, presented) VALUES (?,?,?,?,1)",
            (self.app_id(app) if app_row else 999, uid or uuidlib.uuid4().bytes, data, delivered)).lastrowid

    def delete(self, rec_id: int) -> None:
        self.c.execute("DELETE FROM record WHERE rec_id=?", (rec_id,))

    def close(self) -> None:
        self.c.close()


@pytest.fixture
def nc(tmp_path):
    (tmp_path / "db2").mkdir()
    d = NCDB(tmp_path / "db2" / "db")
    yield d
    d.close()


class Recorder:
    def __init__(self, port: int = 0, status: int = 200) -> None:
        self.reqs: list[dict] = []
        self.status = status
        rec = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                rec.reqs.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                 "body": json.loads(self.rfile.read(n) or b"null")})
                self.send_response(rec.status)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.port = self.srv.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def stop(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()

    def notifies(self) -> list[dict]:
        return [r["body"] for r in self.reqs if r["path"] == "/v1/notify"]


@pytest.fixture
def server():
    s = Recorder()
    yield s
    s.stop()


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make_sensor(nc: NCDB, tmp_path: Path, url: str = "http://127.0.0.1:9", **kw) -> sm.Sensor:
    kw.setdefault("clock", Clock())
    return sm.Sensor(url, kw.pop("token", "tok"), db=str(nc.path), state_path=tmp_path / "state.json", **kw)


# ---------------------------------------------------------------- portability

def test_py39_syntax_and_stdlib_only():
    src = SRC.read_text()
    tree = ast.parse(src, feature_version=(3, 9))
    guarded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for sub in node.body:
                for n in ast.walk(sub):
                    if isinstance(n, ast.Import):
                        guarded.update(a.name for a in n.names)
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import"
            mods.add(node.module.split(".")[0])
    assert "bulbsig" not in mods
    assert mods <= set(sys.stdlib_module_names) | {"__future__"}, mods - set(sys.stdlib_module_names)
    assert "tomllib" in guarded  # 3.11+ only, so it must be try-imported
    assert "from __future__ import annotations" in src


def test_standalone_script_runs_outside_package(nc, tmp_path):
    nc.add(title="standalone")
    r = subprocess.run([sys.executable, "-I", str(SRC), "probe", "--db", str(nc.path), "--json"],
                       capture_output=True, text=True, cwd=str(tmp_path), timeout=30)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["records"][0]["title"] == "standalone"
    r = subprocess.run([sys.executable, "-I", str(SRC)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "probe|sensor" in r.stdout


# ---------------------------------------------------------------- find / open / decode

def test_find_db_override_and_env(nc, tmp_path, monkeypatch):
    assert sm.find_db(str(nc.path)) == nc.path
    with pytest.raises(sm.NoDB):
        sm.find_db(str(tmp_path / "nope"))
    monkeypatch.setenv("BULBSIG_NC_DB", str(nc.path))
    assert sm.find_db() == nc.path
    monkeypatch.setenv("BULBSIG_NC_DB", str(tmp_path / "nope"))
    with pytest.raises(sm.NoDB):
        sm.find_db()


def test_default_candidates(monkeypatch):
    monkeypatch.delenv("BULBSIG_NC_DB", raising=False)
    c = sm.db_candidates()
    assert str(c[0]).endswith("Library/Group Containers/group.com.apple.usernoted/db2/db")


def test_decode_standard_record_with_app_join(nc):
    uid = uuidlib.uuid4().bytes
    when = 800000000.0
    rid = nc.add("com.apple.MobileSMS", "Alice", "re: lunch", "see you at 1", uid=uid, delivered=when, iden="thr-1")
    [n] = sm.read_records(nc.path)
    assert n == {
        "source": "macos", "app": "com.apple.MobileSMS", "title": "Alice", "subtitle": "re: lunch",
        "body": "see you at 1", "time": when + MAC_EPOCH, "id": "nc:" + uid.hex(),
        "meta": {"rec_id": rid, "thread": "thr-1"},
    }
    assert set(n) == {"source", "app", "title", "subtitle", "body", "time", "id", "meta"}


def test_decode_matches_contract_notification(nc):
    from bulbsig.contracts import Notification
    nc.add(title="x", body="y")
    [n] = sm.read_records(nc.path)
    assert Notification.from_json(n).to_json() == n


def test_app_falls_back_to_plist_and_time_to_plist_date(nc):
    data = plistlib.dumps({"app": "com.example.plistonly", "date": 700000000.0, "req": {"titl": "T", "body": "B"}},
                          fmt=plistlib.FMT_BINARY)
    nc.c.execute("INSERT INTO record(app_id, uuid, data, delivered_date) VALUES (424242, NULL, ?, NULL)", (data,))
    [n] = sm.read_records(nc.path)
    assert n["app"] == "com.example.plistonly"
    assert n["time"] == 700000000.0 + MAC_EPOCH
    assert n["id"] == "nc:%d" % n["meta"]["rec_id"]
    assert "thread" not in n["meta"]


def test_fallback_decoder_unexpected_nesting(nc):
    inner = plistlib.dumps({"content": {"titl": "Deep title", "body": "Deep body"}}, fmt=plistlib.FMT_BINARY)
    data = plistlib.dumps({"app": "com.x", "payload": {"wrapped": [{"blob": inner}], "subtitle": "Sub"}},
                          fmt=plistlib.FMT_BINARY)
    nc.add("com.x", data=data)
    [n] = sm.read_records(nc.path)
    assert (n["title"], n["subtitle"], n["body"]) == ("Deep title", "Sub", "Deep body")


def test_keyed_archive_and_garbage():
    UID = plistlib.UID
    arch = {"$archiver": "NSKeyedArchiver", "$version": 100000, "$top": {"root": UID(1)},
            "$objects": ["$null", {"NS.keys": [UID(2), UID(3)], "NS.objects": [UID(4), UID(5)]},
                         "titl", "body", "KA title", "KA body"]}
    n = sm.normalise(1, plistlib.dumps(arch, fmt=plistlib.FMT_BINARY))
    assert (n["title"], n["body"]) == ("KA title", "KA body")
    g = sm.normalise(7, b"not a plist", None, None, "com.y")
    assert g["app"] == "com.y" and g["title"] == "" and g["id"] == "nc:7"


def test_read_records_after_and_limit(nc):
    ids = [nc.add(title=str(i)) for i in range(5)]
    assert [n["title"] for n in sm.read_records(nc.path, after_rec_id=ids[1], limit=2)] == ["2", "3"]
    assert sm.max_rec_id(nc.path) == ids[-1]


def test_open_db_copy_fallback(nc, monkeypatch):
    nc.add(title="via copy")
    real = sm._connect_ro
    monkeypatch.setattr(sm, "_connect_ro", lambda p: (_ for _ in ()).throw(sqlite3.OperationalError("unable to open database file")))
    with sm.open_db(nc.path) as (c, mode):
        assert mode == "copy"
        assert [n["title"] for n in sm.read_records(c)] == ["via copy"]
    monkeypatch.setattr(sm, "_connect_ro", real)
    with sm.open_db(nc.path) as (c, mode):
        assert mode == "direct"


def test_schema_error(tmp_path):
    p = tmp_path / "db"
    sqlite3.connect(str(p)).executescript("CREATE TABLE other(x); INSERT INTO other VALUES (1);")
    with pytest.raises(sm.DBSchema):
        sm.read_records(p)


# ---------------------------------------------------------------- sensor polling

def test_first_run_does_not_replay_then_incremental(nc, tmp_path):
    nc.add(title="old1")
    nc.add(title="old2")
    s = make_sensor(nc, tmp_path, dry_run=True, emit=lambda line: None)
    assert s.poll() == 0 and not s.buffer
    nc.add(title="new1")
    nc.add(title="new2")
    assert s.poll() == 2
    assert [e[1]["title"] for e in s.buffer] == ["new1", "new2"]
    assert s.poll() == 0


def test_apps_filter_is_deliberate_skip(nc, tmp_path):
    s = make_sensor(nc, tmp_path, apps={"com.keep"})
    s.poll()
    nc.add("com.drop", "a")
    r = nc.add("com.keep", "b")
    nc.add("com.drop", "c")
    assert s.poll() == 1 and s.buffer[0][1]["title"] == "b"
    assert s.acked() == r - 1
    s.buffer.clear()
    s.save_state()
    assert json.loads((tmp_path / "state.json").read_text())["rec_id"] == r + 1


def test_rec_id_backwards_resets(nc, tmp_path, capfd):
    for _ in range(3):
        nc.add()
    s = make_sensor(nc, tmp_path)
    s.poll()
    nc.c.execute("DELETE FROM record")
    r = nc.add(title="after reset")
    assert r == 1
    assert s.poll() == 0
    assert "went backwards" in capfd.readouterr().err
    assert s.cursor == 1
    nc.add(title="next")
    assert s.poll() == 1 and s.buffer[0][1]["title"] == "next"


def test_reused_rec_id_within_one_poll_is_detected(nc, tmp_path):
    s = make_sensor(nc, tmp_path)
    s.poll()
    r = nc.add(title="first")
    assert s.poll() == 1
    s.buffer.clear()
    nc.delete(r)
    assert nc.add(title="second") == r  # INTEGER PRIMARY KEY reuses max+1
    assert s.poll() == 1 and s.buffer[0][1]["title"] == "second"


def test_state_persists_across_restart_without_duplicates(nc, tmp_path, server):
    nc.add(title="history")
    s = make_sensor(nc, tmp_path, url=server.url)
    s.step()
    nc.add(title="a")
    nc.add(title="b")
    s.step()
    assert [n["title"] for n in server.notifies()] == ["a", "b"]
    del s
    s2 = make_sensor(nc, tmp_path, url=server.url)
    s2.step()
    assert len(server.notifies()) == 2
    nc.add(title="c")
    s2.step()
    assert [n["title"] for n in server.notifies()] == ["a", "b", "c"]


def test_undelivered_survive_restart(nc, tmp_path, server):
    s = make_sensor(nc, tmp_path, url="http://127.0.0.1:9")
    s.poll()
    nc.add(title="x")
    nc.add(title="y")
    s.step()
    assert len(s.buffer) == 2
    s2 = make_sensor(nc, tmp_path, url=server.url)
    s2.step()
    assert [n["title"] for n in server.notifies()] == ["x", "y"]


def test_state_for_other_db_starts_fresh(nc, tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"db": "/elsewhere", "rec_id": 0}))
    nc.add()
    s = make_sensor(nc, tmp_path, dry_run=True, emit=lambda line: None)
    assert s.poll() == 0 and s.cursor == 1


# ---------------------------------------------------------------- HTTP

def test_post_with_auth_and_heartbeat(nc, tmp_path, server):
    clock = Clock()
    s = make_sensor(nc, tmp_path, url=server.url, token="sekrit", clock=clock)
    s.step()
    assert [r["path"] for r in server.reqs] == ["/v1/heartbeat"]
    assert server.reqs[0]["body"] == {"source": "macos-sensor"}
    nc.add("com.apple.mail", "Mail", "Subj", "Body")
    s.step()
    [req] = [r for r in server.reqs if r["path"] == "/v1/notify"]
    assert req["auth"] == "Bearer sekrit"
    assert req["body"]["source"] == "macos" and req["body"]["app"] == "com.apple.mail"
    assert len([r for r in server.reqs if r["path"] == "/v1/heartbeat"]) == 1
    clock.t += 61
    s.step()
    assert len([r for r in server.reqs if r["path"] == "/v1/heartbeat"]) == 2


def test_retry_after_server_was_down(nc, tmp_path):
    down = Recorder()
    port = down.port
    down.stop()
    clock = Clock()
    s = make_sensor(nc, tmp_path, url="http://127.0.0.1:%d" % port, clock=clock)
    s.poll()
    nc.add(title="queued1")
    nc.add(title="queued2")
    s.step()
    assert len(s.buffer) == 2 and s.backoff == 1.0
    clock.t += 1
    s.step()
    assert len(s.buffer) == 2 and s.backoff == 2.0
    up = Recorder(port=port)
    try:
        s.step()  # still backing off
        assert not up.notifies()
        clock.t += 2
        s.step()
        assert [n["title"] for n in up.notifies()] == ["queued1", "queued2"]
        assert not s.buffer and s.backoff == 0
    finally:
        up.stop()


def test_bounded_buffer(nc, tmp_path):
    s = make_sensor(nc, tmp_path, buffer_max=3)
    s.poll()
    for i in range(5):
        nc.add(title=str(i))
    s.poll()
    assert [e[1]["title"] for e in s.buffer] == ["2", "3", "4"]


def test_rejected_payload_is_skipped(nc, tmp_path, server):
    server.status = 400
    s = make_sensor(nc, tmp_path, url=server.url)
    s.poll()
    nc.add()
    s.step()
    assert not s.buffer


def test_resolve_target_precedence(tmp_path, monkeypatch):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[bulbs.desk]\nmac = "x"\n[daemon]\nlisten = "0.0.0.0:9000"\ntoken = "filetok" # c\n')
    monkeypatch.delenv("BULBSIG_URL", raising=False)
    monkeypatch.delenv("BULBSIG_TOKEN", raising=False)
    assert sm.resolve_target(None, None, cfg) == ("http://0.0.0.0:9000", "filetok")
    monkeypatch.setenv("BULBSIG_URL", "http://env:1/")
    monkeypatch.setenv("BULBSIG_TOKEN", "envtok")
    assert sm.resolve_target(None, None, cfg) == ("http://env:1", "envtok")
    assert sm.resolve_target("http://cli:2", "clitok", cfg) == ("http://cli:2", "clitok")
    monkeypatch.delenv("BULBSIG_URL")
    assert sm.resolve_target(None, None, tmp_path / "missing.toml")[0] == sm.DEFAULT_URL
    assert sm._mini_toml_daemon(cfg.read_text()) == {"listen": "0.0.0.0:9000", "token": "filetok"}


def test_sensor_process_dry_run_and_sigterm(nc, tmp_path):
    nc.add(title="history")
    p = subprocess.Popen([sys.executable, "-I", str(SRC), "sensor", "--dry-run", "--interval", "0.1",
                          "--db", str(nc.path), "--state", str(tmp_path / "st.json")],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.time() + 10
        while not (tmp_path / "st.json").exists() and time.time() < deadline:
            time.sleep(0.05)
        nc.add(title="live one")
        timer = threading.Timer(10, p.kill)
        timer.start()
        line = p.stdout.readline()
        timer.cancel()
        assert json.loads(line)["title"] == "live one"
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=10) == 0
    finally:
        if p.poll() is None:
            p.kill()
        p.stdout.close()
        p.stderr.close()


# ---------------------------------------------------------------- probe

def test_probe_ok(nc, capsys):
    nc.add("com.apple.MobileSMS", "Bob", "", "hello")
    assert sm.probe_main(["--db", str(nc.path), "-n", "5", "--raw", "--schema"]) == 0
    out = capsys.readouterr().out
    assert "opens: yes" in out and "Bob" in out and "hello" in out
    assert "record(rec_id" in out and "titl: str = 'Bob'" in out


def test_probe_json(nc, capsys):
    nc.add(title="J")
    assert sm.probe_main(["--db", str(nc.path), "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["records"][0]["title"] == "J" and d["exit"] == 0 and "record" in d["schema"]


def test_probe_missing_db(tmp_path, capsys):
    assert sm.probe_main(["--db", str(tmp_path / "none")]) == 2


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file modes")
def test_probe_permission_denied(nc, capsys):
    nc.add()
    os.chmod(nc.path, 0)
    try:
        assert sm.probe_main(["--db", str(nc.path)]) == 3
    finally:
        os.chmod(nc.path, 0o644)
    out = capsys.readouterr().out
    assert "Full Disk Access" in out and os.path.realpath(sys.executable) in out


def test_probe_schema_not_understood(tmp_path, capsys):
    p = tmp_path / "db"
    sqlite3.connect(str(p)).executescript("CREATE TABLE other(x);")
    assert sm.probe_main(["--db", str(p)]) == 4
    assert "other(x)" in capsys.readouterr().out


def test_probe_undecodable_records(nc, capsys):
    nc.add(data=b"\x00garbage")
    assert sm.probe_main(["--db", str(nc.path)]) == 4
