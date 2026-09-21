"""SQLite data layer for the public site.

Single file, stdlib sqlite3. init_db() creates/migrates the schema and is called
ONCE at process start (app.py module load), not per request.
"""
import os
import sqlite3
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
DB_PATH = DATA_DIR / "site.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    ip TEXT,
    submitter_name TEXT,
    prompt_text TEXT NOT NULL,
    display_prompt TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    triage_note TEXT,
    photo_id INTEGER,
    updated_at TEXT,
    target TEXT,
    source TEXT NOT NULL DEFAULT 'visitor',
    planned_for TEXT,
    position REAL
);

CREATE TABLE IF NOT EXISTS site_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS likes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_id INTEGER NOT NULL,
    voter_id TEXT NOT NULL,
    ip TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(photo_id, voter_id)
);

CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT UNIQUE NOT NULL,
    title TEXT,
    object TEXT,
    kind TEXT,
    taken TEXT,
    frames INTEGER,
    sub REAL,
    total_exp TEXT,
    filter TEXT,
    gain TEXT,
    notes TEXT,
    credit TEXT,
    request_id INTEGER,
    created_at TEXT,
    width INTEGER,
    height INTEGER,
    bytes INTEGER,
    featured INTEGER NOT NULL DEFAULT 1
);
"""


def get_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "full").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "thumb").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    # Migration: likes used to be keyed on ip; move to voter_id.
    cols = _columns(conn, "likes")
    if cols and "voter_id" not in cols:
        conn.executescript("""
            ALTER TABLE likes RENAME TO likes_old;
            CREATE TABLE likes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                photo_id INTEGER NOT NULL,
                voter_id TEXT NOT NULL,
                ip TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(photo_id, voter_id)
            );
            INSERT INTO likes (photo_id, voter_id, ip, created_at)
                SELECT photo_id, 'ip:' || ip, ip, created_at FROM likes_old;
            DROP TABLE likes_old;
        """)
    # Migration: photos.featured (the site stores every picture; only featured ones
    # show on the public gallery). Existing rows were all hand-picked, so they stay featured.
    if "featured" not in _columns(conn, "photos"):
        conn.execute("ALTER TABLE photos ADD COLUMN featured INTEGER NOT NULL DEFAULT 1")
    # Migration: request scheduling columns. target = the object the approver chose (what the
    # telescope resolves); source = visitor | ai | owner; planned_for = tonight | next | null;
    # position = manual order among AI picks (visitors are always in arrival order).
    rc = _columns(conn, "requests")
    for col, ddl in (("target", "TEXT"), ("source", "TEXT NOT NULL DEFAULT 'visitor'"), ("planned_for", "TEXT"), ("position", "REAL")):
        if col not in rc:
            conn.execute(f"ALTER TABLE requests ADD COLUMN {col} {ddl}")
    conn.commit()
    conn.close()
