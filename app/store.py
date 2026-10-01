"""Durable operation journal; credentials and uploaded bytes are never stored."""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, owner TEXT, created TEXT, status TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS operations (
                key TEXT PRIMARY KEY, owner TEXT, phase TEXT, status TEXT, updated TEXT);
        """)
        self.db.execute("UPDATE jobs SET status='interrupted' WHERE status='running'")
        self.db.commit()

    def job(self, owner, payload):
        ident = uuid4().hex
        with self.lock:
            self.db.execute("INSERT INTO jobs VALUES (?,?,?,?,?)",
                            (ident, owner, self.now(), "running", json.dumps(payload, ensure_ascii=False)))
            self.db.commit()
        return ident

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def save_job(self, ident, status, payload):
        with self.lock:
            self.db.execute("UPDATE jobs SET status=?,payload=? WHERE id=?",
                            (status, json.dumps(payload, ensure_ascii=False), ident))
            self.db.commit()

    def jobs(self, owner):
        with self.lock:
            rows = self.db.execute("SELECT * FROM jobs WHERE owner=? ORDER BY created DESC LIMIT 100", (owner,)).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def get_job(self, ident, owner):
        with self.lock:
            row = self.db.execute("SELECT * FROM jobs WHERE id=? AND owner=?", (ident, owner)).fetchone()
        return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    def operation(self, key):
        with self.lock:
            row = self.db.execute("SELECT * FROM operations WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def mark(self, key, owner, phase, status):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO operations VALUES (?,?,?,?,?)",
                            (key, owner, phase, status, self.now()))
            self.db.commit()

    def clear(self, owner):
        with self.lock:
            self.db.execute("DELETE FROM jobs WHERE owner=?", (owner,))
            # Retain only unresolved operation fingerprints to prevent duplicate comments.
            self.db.execute("DELETE FROM operations WHERE owner=? AND status IN ('done','failed')", (owner,))
            self.db.commit()
            self.db.execute("VACUUM")

    def close(self):
        self.db.close()
