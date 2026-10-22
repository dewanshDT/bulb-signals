"""macOS Notification Center sensor: reads the usernoted SQLite DB and forwards
new notifications to the bulbsig daemon. Stdlib only, Python 3.9 compatible, so it
also runs standalone: `/usr/bin/python3 sensor_macos.py probe|sensor ...`."""
from __future__ import annotations

import argparse
import collections
import contextlib
import datetime
import json
import os
import plistlib
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterator, List, Optional, Sequence, Set, Tuple, Union
from urllib.parse import quote

try:
    import tomllib  # type: ignore
except ImportError:  # Python < 3.11 (macOS /usr/bin/python3)
    tomllib = None  # type: ignore

MAC_EPOCH = 978307200.0
DEFAULT_URL = "http://127.0.0.1:8766"
CONFIG_PATH = Path.home() / ".config" / "bulbsig" / "config.toml"
WINDOW = 64
TITLE_KEYS = ("titl", "title")
SUBTITLE_KEYS = ("subt", "subtitle")
BODY_KEYS = ("body", "message")

DBLike = Union[str, "os.PathLike[str]", sqlite3.Connection]


class DBError(Exception):
    code = 1


class NoDB(DBError):
    code = 2


class DBPermission(DBError):
    code = 3


class DBSchema(DBError):
    code = 4


def _log(msg: str) -> None:
    sys.stderr.write("%s sensor: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stderr.flush()


# ---------------------------------------------------------------- locating / opening

def db_candidates(override: Optional[str] = None) -> List[Path]:
    o = override or os.environ.get("BULBSIG_NC_DB")
    if o:
        return [Path(o).expanduser()]
    c = [Path.home() / "Library" / "Group Containers" / "group.com.apple.usernoted" / "db2" / "db"]
    try:
        d = subprocess.run(["getconf", "DARWIN_USER_DIR"], capture_output=True, text=True, timeout=5).stdout.strip()
        if d:
            c.append(Path(d) / "com.apple.notificationcenter" / "db2" / "db")
    except (OSError, subprocess.SubprocessError):
        pass
    return c


def _exists(p: Path) -> bool:
    try:
        os.stat(p)
        return True
    except PermissionError:
        return True  # TCC may hide it from stat; opening will report the real problem
    except OSError:
        return False


def find_db(override: Optional[str] = None) -> Path:
    cands = db_candidates(override)
    for p in cands:
        if _exists(p):
            return p
    raise NoDB("Notification Center DB not found; tried: " + ", ".join(str(p) for p in cands))


def _connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect("file:" + quote(str(path)) + "?mode=ro", uri=True, timeout=2.0)
    conn.execute("PRAGMA query_only=1")
    return conn


def _check_permission(path: Path, err: Optional[BaseException] = None) -> None:
    msg = str(err or "").lower()
    if "authoriz" in msg or "not permitted" in msg:
        raise DBPermission("%s: %s" % (path, err))
    try:
        with open(path, "rb") as f:
            f.read(16)
    except PermissionError as e:
        raise DBPermission("%s: %s" % (path, e)) from e
    except OSError:
        pass


@contextlib.contextmanager
def open_db(path: Union[str, "os.PathLike[str]"]) -> Iterator[Tuple[sqlite3.Connection, str]]:
    """Yield (connection, mode); mode is "direct" or "copy". Never writes to the real DB."""
    p = Path(path)
    conn: Optional[sqlite3.Connection] = None
    tmp: Optional[str] = None
    mode = "direct"
    try:
        try:
            conn = _connect_ro(p)
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.Error as e:
            if conn is not None:
                conn.close()
                conn = None
            _check_permission(p, e)
            if not _exists(p):
                raise NoDB("%s: %s" % (p, e))
            # A read-only WAL reader can fail when -shm is missing/unwritable; read a private copy.
            tmp = tempfile.mkdtemp(prefix="bulbsig-nc-")
            try:
                for suf in ("", "-wal", "-shm"):
                    src = Path(str(p) + suf)
                    if suf == "" or src.exists():
                        shutil.copyfile(str(src), os.path.join(tmp, "db" + suf))
            except PermissionError as e2:
                raise DBPermission("%s: %s" % (p, e2)) from e2
            except OSError as e2:
                raise DBError("%s: cannot copy: %s" % (p, e2)) from e2
            try:
                conn = sqlite3.connect(os.path.join(tmp, "db"), timeout=2.0)
                conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            except sqlite3.Error as e3:
                raise DBError("%s: cannot open (direct: %s; copy: %s)" % (p, e, e3)) from e3
            mode = "copy"
        yield conn, mode
    finally:
        if conn is not None:
            conn.close()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


@contextlib.contextmanager
def _conn(db: DBLike) -> Iterator[sqlite3.Connection]:
    if isinstance(db, sqlite3.Connection):
        yield db
    else:
        with open_db(db) as (c, _):
            yield c


def describe_schema(conn: sqlite3.Connection) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        out[name] = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % _qi(name))]
    return out


def _qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# ---------------------------------------------------------------- decoding

def _unarchive(top: Dict[str, Any]) -> Any:
    objs = top.get("$objects")
    root = (top.get("$top") or {}).get("root")
    if not isinstance(objs, list) or root is None:
        return top

    def res(x: Any, depth: int) -> Any:
        if depth > 30:
            return None
        if isinstance(x, plistlib.UID):
            return res(objs[x.data], depth + 1) if 0 <= x.data < len(objs) else None
        if isinstance(x, dict):
            if "NS.keys" in x and "NS.objects" in x:
                ks = [res(k, depth + 1) for k in x["NS.keys"]]
                return {str(k): res(v, depth + 1) for k, v in zip(ks, x["NS.objects"])}
            if "NS.string" in x:
                return x["NS.string"]
            return {k: res(v, depth + 1) for k, v in x.items() if k != "$class"}
        if isinstance(x, list):
            return [res(v, depth + 1) for v in x]
        return x

    return res(root, 0)


def decode_plist(blob: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(blob, (bytes, bytearray)) or not blob:
        return None
    try:
        obj = plistlib.loads(bytes(blob))
    except Exception:
        return None
    if isinstance(obj, dict) and "$archiver" in obj:
        obj = _unarchive(obj)
    return obj if isinstance(obj, dict) else {"_root": obj}


def _nested(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray)) and bytes(v[:6]) == b"bplist":
        d = decode_plist(v)
        if d is not None:
            return d
    return v


def _search(obj: Any, keys: Sequence[str]) -> Optional[str]:
    """Breadth-first search for the shallowest non-empty string under any of `keys`."""
    queue: Deque[Tuple[Any, int]] = collections.deque([(obj, 0)])
    while queue:
        cur, depth = queue.popleft()
        cur = _nested(cur)
        if isinstance(cur, dict):
            for k in keys:
                v = cur.get(k)
                if isinstance(v, str) and v:
                    return v
            if depth < 12:
                queue.extend((v, depth + 1) for v in cur.values())
        elif isinstance(cur, list) and depth < 12:
            queue.extend((v, depth + 1) for v in cur)
    return None


def _first(d: Dict[str, Any], keys: Sequence[str]) -> Optional[str]:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str):
            return v
    return None


def _mac_time(v: Any) -> Optional[float]:
    if isinstance(v, datetime.datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=datetime.timezone.utc)
        return v.timestamp()
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
        f = float(v)
        if f > 1e11:  # nanoseconds rather than seconds
            f /= 1e9
        return f + MAC_EPOCH
    return None


def _hex(v: Any) -> Optional[str]:
    if isinstance(v, (bytes, bytearray)) and v:
        return bytes(v).hex()
    if isinstance(v, str) and v:
        return v.replace("-", "").lower()
    return None


def normalise(rec_id: int, data: Any, uuid: Any = None, delivered: Any = None,
              app_ident: Optional[str] = None) -> Dict[str, Any]:
    p = decode_plist(data) or {}
    req = _nested(p.get("req"))
    req = req if isinstance(req, dict) else {}

    def text(keys: Sequence[str]) -> str:
        v = _first(req, keys)
        return v if v is not None else (_search(p, keys) or "")

    app = app_ident or (p.get("app") if isinstance(p.get("app"), str) else None) or _search(p, ("app",)) or ""
    t = _mac_time(delivered)
    if t is None:
        t = _mac_time(p.get("date"))
    meta: Dict[str, Any] = {"rec_id": rec_id}
    thread = req.get("iden") if isinstance(req.get("iden"), str) else _search(p, ("iden",))
    if thread:
        meta["thread"] = thread
    uid = _hex(uuid) or _hex(p.get("uuid"))
    return {
        "source": "macos",
        "app": app,
        "title": text(TITLE_KEYS),
        "subtitle": text(SUBTITLE_KEYS),
        "body": text(BODY_KEYS),
        "time": t if t is not None else 0.0,
        "id": "nc:" + (uid or str(rec_id)),
        "meta": meta,
    }


def _record_query(conn: sqlite3.Connection) -> str:
    sch = describe_schema(conn)
    rc = set(sch.get("record", []))
    if not {"rec_id", "data"} <= rc:
        raise DBSchema("table 'record' with rec_id/data not found; tables: %s" % sorted(sch))
    ac = set(sch.get("app", []))
    join = "app_id" in rc and {"app_id", "identifier"} <= ac
    return "SELECT r.rec_id, r.data, %s, %s, %s FROM record r %s" % (
        "r.uuid" if "uuid" in rc else "NULL",
        "r.delivered_date" if "delivered_date" in rc else "NULL",
        "a.identifier" if join else "NULL",
        "LEFT JOIN app a ON a.app_id = r.app_id" if join else "",
    )


def fetch_raw(db: DBLike, after_rec_id: int = 0, limit: int = 500, newest: bool = False) -> List[Tuple[Any, ...]]:
    with _conn(db) as c:
        q = _record_query(c)
        if newest:
            rows = c.execute(q + " ORDER BY r.rec_id DESC LIMIT ?", (limit,)).fetchall()
            return rows[::-1]
        return c.execute(q + " WHERE r.rec_id > ? ORDER BY r.rec_id LIMIT ?", (after_rec_id, limit)).fetchall()


def read_records(db: DBLike, after_rec_id: int = 0, limit: int = 500) -> List[Dict[str, Any]]:
    return [normalise(*r) for r in fetch_raw(db, after_rec_id, limit)]


def max_rec_id(db: DBLike) -> int:
    with _conn(db) as c:
        _record_query(c)
        return int(c.execute("SELECT coalesce(max(rec_id), 0) FROM record").fetchone()[0])


# ---------------------------------------------------------------- probe

def fda_help() -> str:
    exe = sys.executable or "python3"
    real = os.path.realpath(exe)
    lines = [
        "Permission denied reading the Notification Center database.",
        "Grant Full Disk Access:",
        "  System Settings -> Privacy & Security -> Full Disk Access -> '+'",
        "  - when running interactively: add your terminal app (Terminal, iTerm, ...), then quit and reopen it",
        "  - when running under launchd: add the python binary itself:",
        "      " + real,
    ]
    if real != exe:
        lines.append("      (sys.executable is %s)" % exe)
    app = Path(sys.base_prefix) / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    if app.exists():
        lines.append("    framework builds may run as %s; add that too if access is still denied" % app)
    lines.append("  In the file picker press Cmd+Shift+G and paste the path. Re-run after toggling it on.")
    return "\n".join(lines)


def _fmt_time(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "?"


def key_tree(obj: Any, indent: int = 2) -> List[str]:
    out: List[str] = []
    pad = " " * indent

    def short(v: Any) -> str:
        s = repr(v)
        return s if len(s) <= 100 else s[:97] + "..."

    if isinstance(obj, dict):
        for k, v in obj.items():
            v = _nested(v)
            if isinstance(v, (dict, list)):
                out.append("%s%s: %s" % (pad, k, type(v).__name__))
                out.extend(key_tree(v, indent + 2))
            else:
                out.append("%s%s: %s = %s" % (pad, k, type(v).__name__, short(v)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:50]):
            out.extend(key_tree({"[%d]" % i: v}, indent))
    return out


def _sw_vers() -> Optional[str]:
    try:
        r = subprocess.run(["sw_vers"], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def probe_main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="bulbsig probe", description="dump recent macOS Notification Center records")
    ap.add_argument("--db", help="DB path (default: auto; env BULBSIG_NC_DB)")
    ap.add_argument("-n", type=int, default=20, help="number of recent records (default 20)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--raw", action="store_true", help="dump each record's plist key tree")
    ap.add_argument("--schema", action="store_true", help="print tables and columns")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    report: Dict[str, Any] = {"python": sys.version.split()[0], "executable": sys.executable}
    say = (lambda s: None) if a.json else print

    def done(code: int, err: Optional[str] = None) -> int:
        if err:
            report["error"] = err
        report["exit"] = code
        if a.json:
            print(json.dumps(report, indent=2, default=str))
        return code

    sw = _sw_vers()
    report["sw_vers"] = sw
    say("== system\n" + (sw or "sw_vers not available (not macOS?)") + "\npython %s (%s)" % (report["python"], sys.executable))
    try:
        db = find_db(a.db)
    except NoDB as e:
        say("== database\n" + str(e))
        return done(2, str(e))
    report["db"] = str(db)
    say("== database\npath: %s" % db)
    try:
        with open_db(db) as (conn, mode):
            report["open"] = mode
            say("opens: yes (%s)" % ("read-only, direct" if mode == "direct" else "via temporary copy"))
            schema = describe_schema(conn)
            if a.schema or a.json:
                report["schema"] = schema
            if a.schema and not a.json:
                _print_schema(schema)
            try:
                raw = fetch_raw(conn, limit=max(a.n, 0), newest=True)
                total = conn.execute("SELECT count(*), coalesce(max(rec_id), 0) FROM record").fetchone()
            except DBSchema as e:
                if not a.schema and not a.json:
                    _print_schema(schema)
                report["schema"] = schema
                say("schema not understood: %s" % e)
                return done(4, str(e))
    except DBPermission as e:
        report["open"] = False
        if a.json:
            report["help"] = fda_help()
        say("opens: NO\n%s\n\n%s" % (e, fda_help()))
        return done(3, str(e))
    except DBError as e:
        report["open"] = False
        say("opens: NO\n%s" % e)
        return done(e.code, str(e))
    report["count"], report["max_rec_id"] = int(total[0]), int(total[1])
    say("records: %d, max rec_id: %d" % (report["count"], report["max_rec_id"]))
    say("== last %d records" % len(raw))
    recs: List[Dict[str, Any]] = []
    decoded = 0
    for row in raw:
        n = normalise(*row)
        p = decode_plist(row[1])
        if p is not None and (n["title"] or n["subtitle"] or n["body"]):
            decoded += 1
        if a.raw:
            n = dict(n, raw=p)
        recs.append(n)
        say("[%s] #%s %s\n    title:    %s\n    subtitle: %s\n    body:     %s" % (
            _fmt_time(n["time"]), n["meta"]["rec_id"], n["app"] or "?", n["title"], n["subtitle"], n["body"]))
        if a.raw:
            say("    plist:" if p is not None else "    plist: <could not decode %d bytes>" % len(row[1] or b""))
            for line in key_tree(p or {}, 6):
                say(line)
    report["records"] = recs
    report["decoded"] = decoded
    if raw and decoded == 0:
        report["schema"] = schema
        if not a.schema and not a.json:
            _print_schema(schema)
        say("no record yielded a title/subtitle/body; re-run with --raw and share the output")
        return done(4, "records present but none decoded")
    return done(0)


def _print_schema(schema: Dict[str, List[str]]) -> None:
    print("== schema")
    for t, cols in schema.items():
        print("  %s(%s)" % (t, ", ".join(cols)))


# ---------------------------------------------------------------- sensor

def default_state_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "bulbsig" / "sensor-state.json"
    return Path.home() / ".config" / "bulbsig" / "sensor-state.json"


def _mini_toml_daemon(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    section = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            section = s.strip("[] \t")
            continue
        if section != "daemon":
            continue
        m = re.match(r"""^([A-Za-z_][\w-]*)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|'([^']*)')\s*(?:#.*)?$""", s)
        if m:
            out[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3)
    return out


def load_daemon_config(path: Optional[Path] = None) -> Dict[str, str]:
    p = path or Path(os.environ.get("BULBSIG_CONFIG") or CONFIG_PATH).expanduser()
    try:
        text = p.read_text()
    except OSError:
        return {}
    if tomllib is not None:
        try:
            d = tomllib.loads(text).get("daemon", {})
            return {k: str(v) for k, v in d.items() if isinstance(v, (str, int))}
        except Exception:
            return {}
    return _mini_toml_daemon(text)


def resolve_target(url: Optional[str], token: Optional[str], cfg_path: Optional[Path] = None) -> Tuple[str, str]:
    cfg = load_daemon_config(cfg_path)
    u = url or os.environ.get("BULBSIG_URL") or cfg.get("url") or (
        "http://" + cfg["listen"] if cfg.get("listen") else DEFAULT_URL)
    t = token if token is not None else (os.environ.get("BULBSIG_TOKEN") or cfg.get("token") or "")
    return u.rstrip("/"), t


def http_post(url: str, payload: Dict[str, Any], token: str = "", timeout: float = 3.0) -> int:
    """Return the HTTP status, or 0 if the daemon was unreachable."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(r.status)
    except urllib.error.HTTPError as e:
        return int(e.code)
    except (urllib.error.URLError, OSError, ValueError):
        return 0


class Sensor:
    def __init__(self, url: str, token: str = "", db: Optional[str] = None, state_path: Optional[Path] = None,
                 apps: Optional[Set[str]] = None, dry_run: bool = False, buffer_max: int = 200,
                 heartbeat_s: float = 60.0, clock: Callable[[], float] = time.monotonic,
                 emit: Optional[Callable[[str], None]] = None) -> None:
        self.url, self.token, self.db_override = url.rstrip("/"), token, db
        self.db: Optional[Path] = None
        self.state_path = state_path or default_state_path()
        self.apps = apps or set()
        self.dry_run, self.buffer_max, self.heartbeat_s = dry_run, buffer_max, heartbeat_s
        self.clock = clock
        self.emit = emit or (lambda line: print(line, flush=True))
        self.cursor: Optional[int] = None
        self.slots: Dict[int, str] = {}
        # [rec_id or None, notification]; None once a DB reset made the rec_id meaningless
        self.buffer: Deque[List[Any]] = collections.deque()
        self.backoff = 0.0
        self.next_try = 0.0
        self.next_hb = 0.0
        self.down = False

    # state ---------------------------------------------------------
    def _load_state(self) -> None:
        try:
            st = json.loads(self.state_path.read_text())
            if st.get("db") not in (None, str(self.db)):
                _log("state file is for another DB (%s); starting fresh" % st.get("db"))
                return
            self.cursor = int(st["rec_id"])
            self.slots = {int(k): str(v) for k, v in (st.get("slots") or {}).items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.cursor = None

    def acked(self) -> Optional[int]:
        pending = [e[0] for e in self.buffer if e[0] is not None]
        if self.cursor is None:
            return None
        return min(min(pending) - 1, self.cursor) if pending else self.cursor

    def save_state(self) -> None:
        ack = self.acked()
        if ack is None:
            return
        pending = {e[0] for e in self.buffer}
        slots = {str(k): v for k, v in self.slots.items() if k not in pending and k <= self.cursor}
        data = {"db": str(self.db), "rec_id": ack, "slots": slots, "updated": time.time()}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(self.state_path.name + ".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(str(tmp), str(self.state_path))
        except OSError as e:
            _log("cannot write state %s: %s" % (self.state_path, e))

    # polling -------------------------------------------------------
    def poll(self) -> int:
        if self.db is None:
            self.db = find_db(self.db_override)
            _log("reading %s" % self.db)
            self._load_state()
        with open_db(self.db) as (conn, _):
            top = max_rec_id(conn)
            if self.cursor is None:
                self.cursor = top
                self.slots = {n["meta"]["rec_id"]: n["id"] for n in read_records(conn, max(0, top - WINDOW))}
                _log("first run: starting after rec_id %d (history not replayed)" % top)
                self.save_state()
                return 0
            if top < self.cursor:
                _log("rec_id went backwards (%d -> %d; DB reset or records removed); resetting to %d"
                     % (self.cursor, top, top))
                self.cursor = top
                self.slots = {}
                for e in self.buffer:
                    e[0] = None
                self.save_state()
                return 0
            rows = read_records(conn, max(0, self.cursor - WINDOW))
        added = 0
        for n in rows:
            rid, nid = n["meta"]["rec_id"], n["id"]
            known = self.slots.get(rid)
            # A dismissed record's rec_id can be reused (INTEGER PRIMARY KEY), so compare ids too.
            if not (known != nid if known is not None else rid > self.cursor):
                continue
            self.slots[rid] = nid
            self.cursor = max(self.cursor, rid)
            if self.apps and n["app"] not in self.apps:
                continue
            self.buffer.append([rid, n])
            added += 1
            if len(self.buffer) > self.buffer_max:
                old = self.buffer.popleft()
                _log("buffer full; dropping %s" % old[1]["id"])
        lo = self.cursor - 2 * WINDOW
        self.slots = {k: v for k, v in self.slots.items() if k > lo}
        self.save_state()
        return added

    # delivery ------------------------------------------------------
    def _deliver(self, n: Dict[str, Any]) -> str:
        if self.dry_run:
            self.emit(json.dumps(n, ensure_ascii=False))
            return "ok"
        st = http_post(self.url + "/v1/notify", n, self.token)
        if 200 <= st < 300:
            return "ok"
        if st in (400, 413, 422):
            _log("daemon rejected %s (HTTP %d); skipping" % (n["id"], st))
            return "drop"
        if not self.down:
            _log("delivery failed (%s); buffering and retrying" % ("unreachable" if st == 0 else "HTTP %d" % st))
        return "retry"

    def flush(self) -> int:
        sent = 0
        changed = False
        while self.buffer and self.clock() >= self.next_try:
            r = self._deliver(self.buffer[0][1])
            if r == "retry":
                self.down = True
                self.backoff = min(max(1.0, self.backoff * 2), 30.0)
                self.next_try = self.clock() + self.backoff
                break
            if self.down:
                _log("daemon reachable again")
            self.down = False
            self.backoff = 0.0
            self.buffer.popleft()
            changed = True
            sent += r == "ok"
        if changed:
            self.save_state()
        return sent

    def heartbeat(self, force: bool = False) -> Optional[int]:
        if self.dry_run or (not force and self.clock() < self.next_hb):
            return None
        self.next_hb = self.clock() + self.heartbeat_s
        return http_post(self.url + "/v1/heartbeat", {"source": "macos-sensor"}, self.token)

    def step(self) -> None:
        self.poll()
        self.flush()
        self.heartbeat()

    def run(self, stop: threading.Event, interval: float = 1.0) -> None:
        last_err = ""
        while not stop.is_set():
            wait = interval
            try:
                self.step()
                last_err = ""
            except DBError as e:
                msg = str(e) + ("\n" + fda_help() if isinstance(e, DBPermission) else "")
                if msg != last_err:
                    _log(msg)
                last_err = msg
                if isinstance(e, NoDB):
                    self.db = None
                wait = max(interval, 10.0)
                self.flush()
                self.heartbeat()
            except sqlite3.Error as e:
                if str(e) != last_err:
                    _log("sqlite: %s" % e)
                last_err = str(e)
            stop.wait(wait)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="bulbsig sensor", description="forward macOS notifications to the bulbsig daemon")
    ap.add_argument("--url", help="daemon URL (env BULBSIG_URL, config [daemon] url; default %s)" % DEFAULT_URL)
    ap.add_argument("--token", help="bearer token (env BULBSIG_TOKEN, config [daemon] token)")
    ap.add_argument("--interval", type=float, default=1.0, help="poll interval seconds (default 1.0)")
    ap.add_argument("--apps", help="comma-separated bundle ids to forward (default: all)")
    ap.add_argument("--state", help="state file (default %s)" % default_state_path())
    ap.add_argument("--db", help="Notification Center DB path (default: auto; env BULBSIG_NC_DB)")
    ap.add_argument("--dry-run", action="store_true", help="print notifications instead of posting")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    url, token = resolve_target(a.url, a.token)
    apps = {x.strip() for x in (a.apps or "").split(",") if x.strip()}
    s = Sensor(url, token, db=a.db, state_path=Path(a.state).expanduser() if a.state else None,
               apps=apps, dry_run=a.dry_run)
    stop = threading.Event()

    def _stop(signum: int, _frame: Any) -> None:
        _log("signal %d; exiting" % signum)
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    _log("forwarding to %s%s%s" % (url, " (dry run)" if a.dry_run else "",
                                   " apps=" + ",".join(sorted(apps)) if apps else ""))
    s.run(stop, max(0.1, a.interval))
    return 0


if __name__ == "__main__":
    _cmds = {"probe": probe_main, "sensor": main}
    if len(sys.argv) < 2 or sys.argv[1] not in _cmds:
        print("usage: %s probe|sensor [args]  (--help for each)" % os.path.basename(sys.argv[0]))
        sys.exit(0 if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help") else 2)
    sys.exit(_cmds[sys.argv[1]](sys.argv[2:]) or 0)
