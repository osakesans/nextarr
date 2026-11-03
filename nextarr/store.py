"""SQLite state: log, approvals, search history, availability cache."""
import json
import os
import sqlite3
import threading
import time

from . import config

_lock = threading.RLock()
_conn = None


def db():
    global _conn
    if _conn is None:
        os.makedirs(config.CONFIG_DIR, exist_ok=True)
        _conn = sqlite3.connect(os.path.join(config.CONFIG_DIR, "nextarr.db"),
                                check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript("""
        CREATE TABLE IF NOT EXISTS log(ts REAL, level TEXT, msg TEXT);
        CREATE INDEX IF NOT EXISTS log_ts ON log(ts);
        CREATE TABLE IF NOT EXISTS approvals(id INTEGER PRIMARY KEY, created REAL, expires REAL,
            token TEXT, status TEXT, payload TEXT);
        CREATE TABLE IF NOT EXISTS searches(episode_id INTEGER PRIMARY KEY, ts REAL);
        CREATE TABLE IF NOT EXISTS availability(series_id INTEGER, season INTEGER, ts REAL,
            available INTEGER, PRIMARY KEY(series_id, season));
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
        """)
    return _conn


def log(msg, level="info"):
    with _lock:
        db().execute("INSERT INTO log VALUES(?,?,?)", (time.time(), level, msg))
        db().execute("DELETE FROM log WHERE ts < ?", (time.time() - 90 * 86400,))
        db().commit()
    print(f"[{level}] {msg}", flush=True)


def recent_log(limit=200, level=None):
    with _lock:
        if level:
            q = db().execute("SELECT * FROM log WHERE level=? ORDER BY ts DESC LIMIT ?", (level, limit))
        else:
            q = db().execute("SELECT * FROM log ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in q]


def count_log_since(level, since):
    with _lock:
        return db().execute("SELECT COUNT(*) FROM log WHERE level=? AND ts>=?", (level, since)).fetchone()[0]


def kv_get(key, default=None):
    with _lock:
        r = db().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default


def kv_set(key, value):
    with _lock:
        db().execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, json.dumps(value)))
        db().commit()


def last_search(episode_id):
    with _lock:
        r = db().execute("SELECT ts FROM searches WHERE episode_id=?", (episode_id,)).fetchone()
        return r[0] if r else 0


def mark_searched(ids):
    with _lock:
        now = time.time()
        db().executemany("INSERT OR REPLACE INTO searches VALUES(?,?)", [(i, now) for i in ids])
        db().commit()


def availability(series_id, season):
    with _lock:
        r = db().execute("SELECT ts, available FROM availability WHERE series_id=? AND season=?",
                         (series_id, season)).fetchone()
        return (r[0], bool(r[1])) if r else (0, None)


def set_availability(series_id, season, available):
    with _lock:
        db().execute("INSERT OR REPLACE INTO availability VALUES(?,?,?,?)",
                     (series_id, season, time.time(), int(available)))
        db().commit()


def unavailable_series():
    with _lock:
        return {r[0] for r in db().execute("SELECT series_id FROM availability WHERE available=0")}


def create_approval(token, payload, expiry_days):
    with _lock:
        now = time.time()
        cur = db().execute("INSERT INTO approvals(created, expires, token, status, payload) VALUES(?,?,?,?,?)",
                           (now, now + expiry_days * 86400, token, "pending", json.dumps(payload)))
        db().commit()
        return cur.lastrowid


def approvals(status=None):
    with _lock:
        db().execute("UPDATE approvals SET status='expired' WHERE status='pending' AND expires<?", (time.time(),))
        db().commit()
        if status:
            q = db().execute("SELECT * FROM approvals WHERE status=? ORDER BY id DESC", (status,))
        else:
            q = db().execute("SELECT * FROM approvals ORDER BY id DESC LIMIT 50")
        out = []
        for r in q:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out


def get_approval(aid):
    for a in approvals():
        if a["id"] == aid:
            return a
    return None


def set_approval_status(aid, status):
    with _lock:
        db().execute("UPDATE approvals SET status=? WHERE id=?", (status, aid))
        db().commit()
