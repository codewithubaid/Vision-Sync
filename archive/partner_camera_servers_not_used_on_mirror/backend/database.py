from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import time
from uuid import uuid4


class Database:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS incidents (
                id TEXT PRIMARY KEY, camera_id TEXT NOT NULL, camera_name TEXT NOT NULL,
                type TEXT NOT NULL, timestamp TEXT NOT NULL, confidence REAL,
                snapshot BLOB, human_confirmed INTEGER NOT NULL DEFAULT 0,
                target_user TEXT
            );
            CREATE TABLE IF NOT EXISTS devices (
                token TEXT PRIMARY KEY, user_id TEXT NOT NULL, enabled INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                incident_id TEXT NOT NULL REFERENCES incidents(id), token TEXT NOT NULL,
                user_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                error TEXT, PRIMARY KEY(incident_id, token)
            );
            CREATE TABLE IF NOT EXISTS incident_reads (
                incident_id TEXT NOT NULL REFERENCES incidents(id), user_id TEXT NOT NULL,
                PRIMARY KEY(incident_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(incidents)")}
        if "target_user" not in columns:
            self.connection.execute("ALTER TABLE incidents ADD COLUMN target_user TEXT")
            # Older tests were recorded without visibility. Only their recipient can read them.
            self.connection.execute(
                "UPDATE incidents SET target_user=COALESCE((SELECT user_id FROM deliveries "
                "WHERE incident_id=incidents.id LIMIT 1), '') WHERE type='test_notification'"
            )
            self.connection.commit()

    def close(self):
        with self.lock:
            self.connection.close()

    def setting(self, key, default):
        with self.lock:
            row = self.connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row["value"]) if row else default

    def save_settings(self, values):
        with self.lock, self.connection:
            self.connection.executemany(
                "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                [(key, json.dumps(value)) for key, value in values.items()],
            )

    def register_device(self, user_id, token, enabled):
        with self.lock, self.connection:
            # A token can change owners after sign-out/sign-in. Cancel its old jobs.
            self.connection.execute(
                "UPDATE deliveries SET state='cancelled' WHERE token=? AND "
                "state IN ('pending','sending') AND (user_id<>? OR ?=0)", (token, user_id, int(enabled))
            )
            self.connection.execute(
                "INSERT INTO devices VALUES(?,?,?) ON CONFLICT(token) DO UPDATE SET "
                "user_id=excluded.user_id,enabled=excluded.enabled", (token, user_id, int(enabled))
            )

    def remove_device(self, user_id, token):
        with self.lock, self.connection:
            self.connection.execute("DELETE FROM devices WHERE user_id=? AND token=?", (user_id, token))
            self.connection.execute(
                "UPDATE deliveries SET state='cancelled' WHERE user_id=? AND token=? "
                "AND state IN ('pending','sending')",
                (user_id, token),
            )

    def device_count(self, user_id):
        with self.lock:
            return self.connection.execute(
                "SELECT count(*) FROM devices WHERE user_id=? AND enabled=1", (user_id,)
            ).fetchone()[0]

    def create_incident(self, config, snapshot, event_type="motion", target_user=None):
        incident_id = uuid4().hex
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT INTO incidents(id,camera_id,camera_name,type,timestamp,snapshot,target_user) "
                "VALUES(?,?,?,?,?,?,?)",
                (incident_id, config.camera_id, config.camera_name, event_type, timestamp, snapshot, target_user),
            )
            devices = self.connection.execute("SELECT token,user_id FROM devices WHERE enabled=1").fetchall()
            if config.fcm_enabled:
                self.connection.executemany(
                    "INSERT INTO deliveries(incident_id,token,user_id) VALUES(?,?,?)",
                    [(incident_id, row["token"], row["user_id"]) for row in devices
                     if row["user_id"] in config.allowed_users
                     and (target_user is None or row["user_id"] == target_user)],
                )
        return incident_id

    def _incident(self, row, user_id):
        result = dict(row)
        result.pop("snapshot", None)
        result["has_snapshot"] = bool(result.pop("snapshot_size", 0))
        result["human_confirmed"] = bool(result["human_confirmed"])
        result["read"] = self.connection.execute(
            "SELECT 1 FROM incident_reads WHERE incident_id=? AND user_id=?", (result["id"], user_id)
        ).fetchone() is not None
        counts = self.connection.execute(
            "SELECT state,count(*) AS n FROM deliveries WHERE incident_id=? AND user_id=? GROUP BY state",
            (result["id"], user_id),
        ).fetchall()
        result["delivery"] = {row["state"]: row["n"] for row in counts}
        return result

    def list_incidents(self, user_id, limit=50, offset=0):
        with self.lock:
            rows = self.connection.execute(
                "SELECT id,camera_id,camera_name,type,timestamp,confidence,human_confirmed,"
                "length(snapshot) AS snapshot_size FROM incidents "
                "WHERE target_user IS NULL OR target_user=? ORDER BY rowid DESC LIMIT ? OFFSET ?",
                (user_id, limit, offset),
            ).fetchall()
            total = self.connection.execute(
                "SELECT count(*) FROM incidents WHERE target_user IS NULL OR target_user=?", (user_id,)
            ).fetchone()[0]
            return {"total": total, "items": [self._incident(row, user_id) for row in rows]}

    def incident(self, incident_id, user_id):
        with self.lock:
            row = self.connection.execute(
                "SELECT id,camera_id,camera_name,type,timestamp,confidence,human_confirmed,"
                "length(snapshot) AS snapshot_size FROM incidents WHERE id=? "
                "AND (target_user IS NULL OR target_user=?)", (incident_id, user_id)
            ).fetchone()
            return self._incident(row, user_id) if row else None

    def snapshot(self, incident_id, user_id):
        with self.lock:
            row = self.connection.execute(
                "SELECT snapshot FROM incidents WHERE id=? AND (target_user IS NULL OR target_user=?)",
                (incident_id, user_id),
            ).fetchone()
            return row["snapshot"] if row else None

    def mark_read(self, incident_id, user_id):
        with self.lock, self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO incident_reads SELECT id,? FROM incidents "
                "WHERE id=? AND (target_user IS NULL OR target_user=?)", (user_id, incident_id, user_id)
            )

    def next_delivery(self):
        with self.lock, self.connection:
            # A persisted lease prevents duplicate claims and permits retry after a crash.
            self.connection.execute("BEGIN IMMEDIATE")
            now = time.time()
            self.connection.execute(
                "UPDATE deliveries SET state='failed',error='Delivery lease expired' "
                "WHERE state='sending' AND next_attempt<=? AND attempts>=5", (now,)
            )
            row = self.connection.execute(
                "SELECT d.*,i.type,i.timestamp,i.camera_name,i.camera_id FROM deliveries d "
                "JOIN devices v ON v.token=d.token AND v.user_id=d.user_id AND v.enabled=1 "
                "JOIN incidents i ON i.id=d.incident_id "
                "WHERE d.state IN ('pending','sending') AND d.next_attempt<=? "
                "AND d.attempts<5 ORDER BY i.rowid LIMIT 1", (now,)
            ).fetchone()
            if row is None:
                return None
            job = dict(row)
            job["attempts"] += 1
            self.connection.execute(
                "UPDATE deliveries SET state='sending',attempts=?,next_attempt=? "
                "WHERE incident_id=? AND token=?", (job["attempts"], now + 60, job["incident_id"], job["token"])
            )
            return job

    def delivery_result(self, job, error=None, permanent=False):
        attempts = job["attempts"]
        state = "sent" if error is None else ("failed" if permanent or attempts >= 5 else "pending")
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE deliveries SET state=?,attempts=?,next_attempt=?,error=? "
                "WHERE incident_id=? AND token=? AND user_id=? AND state='sending' AND attempts=?",
                (state, attempts, time.time() + min(300, 5 * 2 ** attempts), error,
                 job["incident_id"], job["token"], job["user_id"], attempts),
            )
