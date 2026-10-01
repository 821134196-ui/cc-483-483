"""SQLite 连接与 schema。"""
import json
import os
import sqlite3
import threading
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('staff','manager','tech')),
    pin TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS stores (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    open_time TEXT NOT NULL DEFAULT '09:00',
    close_time TEXT NOT NULL DEFAULT '21:30',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS booths (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    pos_x REAL NOT NULL,
    pos_y REAL NOT NULL,
    pos_w REAL NOT NULL,
    pos_h REAL NOT NULL,
    frozen INTEGER NOT NULL DEFAULT 0,
    UNIQUE(store_id, code)
);

CREATE TABLE IF NOT EXISTS devices (
    serial TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    color TEXT NOT NULL,
    store_id INTEGER REFERENCES stores(id),
    booth_id INTEGER REFERENCES booths(id),
    dock_code TEXT,
    cable_code TEXT,
    status TEXT NOT NULL DEFAULT 'on_display'
        CHECK(status IN ('on_display','pending_restore','restoring','in_transfer','returned')),
    registered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- 同一展台同一时刻只能有一台未退仓设备（序列号是主键，天然不可能属于两个展位）
CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_booth
    ON devices(booth_id) WHERE booth_id IS NOT NULL AND status != 'returned';

CREATE TABLE IF NOT EXISTS device_runtime (
    serial TEXT PRIMARY KEY REFERENCES devices(serial),
    online INTEGER NOT NULL DEFAULT 1,
    battery INTEGER NOT NULL DEFAULT 100,
    appearance TEXT NOT NULL DEFAULT 'ok' CHECK(appearance IN ('ok','scratch')),
    network_state TEXT NOT NULL DEFAULT 'wifi' CHECK(network_state IN ('wifi','cellular','offline')),
    logged_in INTEGER NOT NULL DEFAULT 1,
    account_version TEXT,
    apps TEXT NOT NULL DEFAULT '[]',
    wallpaper TEXT,
    settings TEXT NOT NULL DEFAULT '{}',
    content_flag TEXT,
    faulty INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS docks (
    code TEXT PRIMARY KEY,
    device_serial TEXT REFERENCES devices(serial),
    connected INTEGER NOT NULL DEFAULT 1,
    alarm INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS demo_profiles (
    model TEXT PRIMARY KEY,
    account_version TEXT NOT NULL,
    apps TEXT NOT NULL,
    wallpaper TEXT NOT NULL,
    settings TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory_runs (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    day TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('open','close')),
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
    version INTEGER NOT NULL DEFAULT 1,
    opened_by TEXT, opened_at TEXT,
    closed_by TEXT, closed_at TEXT,
    UNIQUE(store_id, day, kind)
);

CREATE TABLE IF NOT EXISTS inventory_items (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES inventory_runs(id),
    serial TEXT NOT NULL,
    expected_booth_id INTEGER,
    actual_booth_id INTEGER,
    battery INTEGER,
    appearance TEXT,
    network_state TEXT,
    dock_ok INTEGER,
    cable_ok INTEGER,
    missing INTEGER NOT NULL DEFAULT 0,
    result TEXT CHECK(result IN ('ok','abnormal','skipped')),
    note TEXT,
    evidence_path TEXT,
    checked_by TEXT,
    checked_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(run_id, serial)
);

CREATE TABLE IF NOT EXISTS exceptions (
    id INTEGER PRIMARY KEY,
    serial TEXT,
    kind TEXT NOT NULL CHECK(kind IN (
        'offline','cable_alarm','scratch','account_logout','settings_drift',
        'restore_failed','misplaced','closing_missing','transfer_rejected')),
    detail TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
    reporter TEXT, reporter_role TEXT,
    run_id INTEGER,
    evidence_paths TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    resolved_at TEXT, resolved_by TEXT, resolution TEXT
);

CREATE TABLE IF NOT EXISTS restore_jobs (
    id INTEGER PRIMARY KEY,
    serial TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','running','success','failed')),
    reason TEXT,
    expected TEXT NOT NULL,
    actual TEXT,
    diff TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_error TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    executed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_restore_active ON restore_jobs(serial, status);

CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY,
    serial TEXT NOT NULL,
    from_store_id INTEGER NOT NULL,
    from_booth_id INTEGER NOT NULL,
    to_store_id INTEGER NOT NULL,
    to_booth_id INTEGER,
    status TEXT NOT NULL CHECK(status IN ('requested','approved','received','rejected','recalled')),
    requested_by TEXT, approved_by TEXT, received_by TEXT,
    handover TEXT,
    reject_reason TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT, received_at TEXT
);

CREATE TABLE IF NOT EXISTS returns (
    id INTEGER PRIMARY KEY,
    serial TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
    requested_by TEXT, reviewed_by TEXT,
    note TEXT,
    evidence_paths TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    store_id INTEGER,
    created_at TEXT NOT NULL,
    read INTEGER NOT NULL DEFAULT 0
);
"""

_lock = threading.RLock()
_tls = threading.local()


def now():
    return datetime.now().replace(microsecond=0).isoformat()


def today():
    return datetime.now().date().isoformat()


def get_db(path):
    conns = getattr(_tls, "conns", None)
    if conns is None:
        conns = _tls.conns = {}
    conn = conns.get(path)
    if conn is None:
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conns[path] = conn
    return conn


class DB:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self.lock():
            get_db(path).executescript(SCHEMA)
            get_db(path).commit()

    def lock(self):
        return _lock

    def conn(self):
        return get_db(self.path)

    def query(self, sql, args=()):
        return self.conn().execute(sql, args).fetchall()

    def one(self, sql, args=()):
        return self.conn().execute(sql, args).fetchone()

    def execute(self, sql, args=()):
        return self.conn().execute(sql, args)

    def commit(self):
        self.conn().commit()

    @staticmethod
    def jloads(s, default):
        if not s:
            return default
        try:
            return json.loads(s)
        except (TypeError, ValueError):
            return default
