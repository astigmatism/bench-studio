"""SQLite WAL storage. All changes use short transactions; artifacts stay on disk."""

import contextlib
import json
import sqlite3
from . import config
from common import now

TERMINAL = {"completed", "failed", "interrupted", "invalid", "cancelled"}
ACTIVE = {"starting", "running", "grading", "stopping"}


def connect():
    config.DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(config.DATA / "studio.sqlite3", timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


@contextlib.contextmanager
def transaction():
    c = connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()


def initialize():
    with transaction() as c:
        version = c.execute("PRAGMA user_version").fetchone()[0]
        if version > 1:
            raise RuntimeError(
                "Database is newer than this application; refusing downgrade"
            )
        c.executescript("""
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, status TEXT NOT NULL,
            document TEXT NOT NULL, idempotency_key TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY, document TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS baselines(slot TEXT PRIMARY KEY, run_id TEXT NOT NULL, target TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, created_at TEXT NOT NULL, document TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, document TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS runs_status ON runs(status,created_at);
        PRAGMA user_version=1;
        """)


def pack(v):
    return json.dumps(v, allow_nan=False, separators=(",", ":"))


def unpack(row):
    return json.loads(row["document"]) if row else None


def event(c, rid, value):
    c.execute(
        "INSERT INTO events(run_id,created_at,document) VALUES(?,?,?)",
        (rid, now(), pack(value)),
    )


def put_run(c, m, key=None):
    c.execute(
        "INSERT INTO runs VALUES(?,?,?,?,?)",
        (m["id"], m["created_at"], m["status"], pack(m), key),
    )
    event(c, m["id"], {"type": "run", "status": m["status"]})


def update_run(m, *, c=None):
    if c is None:
        with transaction() as c:
            update_run(m, c=c)
        return
    m["updated_at"] = now()
    c.execute(
        "UPDATE runs SET status=?,document=? WHERE id=?",
        (m["status"], pack(m), m["id"]),
    )
    event(
        c,
        m["id"],
        {"type": "run", "status": m["status"], "progress": m.get("progress", "")},
    )


def get_run(rid):
    with connect() as c:
        return unpack(
            c.execute("SELECT document FROM runs WHERE id=?", (rid,)).fetchone()
        )


def runs():
    with connect() as c:
        return [
            unpack(r)
            for r in c.execute("SELECT document FROM runs ORDER BY created_at DESC")
        ]


def state(key, default=None):
    with connect() as c:
        row = c.execute("SELECT document FROM state WHERE key=?", (key,)).fetchone()
    return unpack(row) if row else default


def set_state(key, value):
    with transaction() as c:
        c.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, pack(value)))
