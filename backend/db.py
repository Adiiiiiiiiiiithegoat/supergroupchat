import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.getenv("SGC_DB") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "sgc.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, workspace TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'discussing' CHECK(status IN ('discussing','running','paused','done','stopped')),
  round INTEGER NOT NULL DEFAULT 0, planner_session TEXT, executor_session TEXT, created_at TEXT NOT NULL,
  auto_resume_at REAL);
CREATE TABLE IF NOT EXISTS threads(
  id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
  kind TEXT NOT NULL CHECK(kind IN ('main','branch')), parent_message_id INTEGER,
  planner_session TEXT, status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')));
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id), thread_id INTEGER NOT NULL REFERENCES threads(id),
  role TEXT NOT NULL CHECK(role IN ('aditya','planner','executor','system')),
  content TEXT NOT NULL, meta_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
"""

# ponytail: one shared connection, only touched from the event loop thread
conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
conn.row_factory = sqlite3.Row
conn.executescript(SCHEMA)
try:  # dbs created before auto-resume existed
    conn.execute("ALTER TABLE runs ADD COLUMN auto_resume_at REAL")  # unix seconds
except sqlite3.OperationalError:
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def row(r):
    if r is None:
        return None
    d = dict(r)
    if "meta_json" in d:
        d["meta"] = json.loads(d.pop("meta_json"))
    return d


def one(sql, *args):
    return row(conn.execute(sql, args).fetchone())


def all_(sql, *args):
    return [row(r) for r in conn.execute(sql, args).fetchall()]


def insert(sql, *args):
    return conn.execute(sql, args).lastrowid


def get_run(run_id):
    return one("SELECT * FROM runs WHERE id=?", run_id)


def update_run(run_id, **fields):
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE runs SET {sets} WHERE id=?", (*fields.values(), run_id))
    return get_run(run_id)


def main_thread(run_id):
    return one("SELECT * FROM threads WHERE run_id=? AND kind='main'", run_id)


def add_message(run_id, thread_id, role, content, meta=None):
    mid = insert("INSERT INTO messages(run_id, thread_id, role, content, meta_json, created_at) VALUES(?,?,?,?,?,?)",
                 run_id, thread_id, role, content, json.dumps(meta or {}), now())
    return one("SELECT * FROM messages WHERE id=?", mid)
