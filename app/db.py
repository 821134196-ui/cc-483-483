"""SQLite 数据层：连接管理与建表。

所有状态保存在 data/app.db；外观证据与离线配置包保存在 data/ 目录。
"""
import os
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('sales','manager','technician')),
  store_id INTEGER REFERENCES stores(id)
);

CREATE TABLE IF NOT EXISTS booths(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id INTEGER NOT NULL REFERENCES stores(id),
  code TEXT NOT NULL,
  name TEXT NOT NULL,
  grid_x INTEGER NOT NULL,
  grid_y INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','frozen')),
  UNIQUE(store_id, code)
);

CREATE TABLE IF NOT EXISTS devices(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  serial TEXT UNIQUE NOT NULL,
  model TEXT NOT NULL,
  color TEXT NOT NULL,
  store_id INTEGER NOT NULL REFERENCES stores(id),
  booth_id INTEGER REFERENCES booths(id),
  status TEXT NOT NULL DEFAULT 'displayable'
    CHECK(status IN ('displayable','pending_restore','in_transit','returned')),
  dock_serial TEXT,
  cable_serial TEXT,
  account_name TEXT,
  account_version TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS config_packages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model TEXT NOT NULL,
  version TEXT NOT NULL,
  account_version TEXT NOT NULL,
  wallpaper_id TEXT NOT NULL,
  apps_json TEXT NOT NULL,
  path TEXT,
  UNIQUE(model, version)
);

CREATE TABLE IF NOT EXISTS inventories(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id INTEGER NOT NULL REFERENCES stores(id),
  type TEXT NOT NULL CHECK(type IN ('open','close')),
  biz_date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'in_progress'
    CHECK(status IN ('in_progress','completed','cancelled')),
  started_by INTEGER,
  started_at TEXT,
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS inventory_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  inventory_id INTEGER NOT NULL REFERENCES inventories(id),
  device_id INTEGER NOT NULL REFERENCES devices(id),
  position_ok INTEGER,
  battery INTEGER,
  appearance_ok INTEGER,
  online INTEGER,
  serial_verified INTEGER,
  accessories_ok INTEGER,
  conclusion TEXT,
  note TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  checked_by INTEGER,
  checked_at TEXT,
  UNIQUE(inventory_id, device_id)
);

CREATE TABLE IF NOT EXISTS anomalies(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id INTEGER,
  store_id INTEGER,
  type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolving','resolved')),
  title TEXT,
  detail TEXT,
  reported_by INTEGER,
  created_at TEXT,
  resolved_by INTEGER,
  resolved_at TEXT,
  resolution TEXT
);

CREATE TABLE IF NOT EXISTS evidence(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  anomaly_id INTEGER NOT NULL REFERENCES anomalies(id),
  filename TEXT NOT NULL,
  path TEXT NOT NULL,
  uploaded_by INTEGER,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS restore_tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id INTEGER NOT NULL REFERENCES devices(id),
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','success','failed')),
  expected_json TEXT,
  actual_json TEXT,
  diff_json TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0,
  max_retry INTEGER NOT NULL DEFAULT 3,
  last_error TEXT,
  created_by INTEGER,
  created_at TEXT,
  executed_by INTEGER,
  executed_at TEXT
);

CREATE TABLE IF NOT EXISTS transfers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id INTEGER NOT NULL REFERENCES devices(id),
  from_store_id INTEGER NOT NULL,
  from_booth_id INTEGER,
  to_store_id INTEGER NOT NULL,
  to_booth_id INTEGER,
  status TEXT NOT NULL DEFAULT 'requested'
    CHECK(status IN ('requested','in_transit','received','rejected')),
  handover_json TEXT,
  requested_by INTEGER,
  requested_at TEXT,
  approved_by INTEGER,
  approved_at TEXT,
  received_by INTEGER,
  received_at TEXT,
  rejected_reason TEXT,
  version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS warehouse_returns(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id INTEGER NOT NULL REFERENCES devices(id),
  store_id INTEGER NOT NULL,
  reason TEXT,
  status TEXT NOT NULL DEFAULT 'requested' CHECK(status IN ('requested','completed','rejected')),
  checklist_json TEXT,
  requested_by INTEGER,
  created_at TEXT,
  reviewed_by INTEGER,
  reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT,
  level TEXT,
  message TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT,
  action TEXT,
  entity TEXT,
  entity_id INTEGER,
  detail TEXT,
  created_at TEXT
);
"""


class Database:
    """线程安全的 SQLite 封装（每线程一个连接）。"""

    def __init__(self, path):
        self.path = path
        self._local = threading.local()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.init()

    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA journal_mode=WAL")
            self._local.conn = c
        return c

    def init(self):
        c = sqlite3.connect(self.path)
        try:
            c.executescript(SCHEMA)
            c.commit()
        finally:
            c.close()

    def execute(self, sql, params=()):
        cur = self.conn().execute(sql, params)
        self.conn().commit()
        return cur

    def query(self, sql, params=()):
        return [dict(r) for r in self.conn().execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None
