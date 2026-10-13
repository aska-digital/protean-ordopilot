"""Delivery ledger: SQLite + JSONL mirror with idempotency keys.

idempotency_key = sha256(canonical_hash + "|" + target_session_id).
Only confirmed delivered keys may be acknowledged as duplicates.
SQLite is authoritative; JSONL is a best-effort transition mirror.
The payload column holds only the canonical hash, never message text.
"""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import sqlite3
import threading

from .intake import utcnow_rfc3339

def _synchronized(method):
    def wrapped(self, *args, **kwargs):
        with self._mutex:
            return method(self, *args, **kwargs)
    return wrapped


SCHEMA = """CREATE TABLE IF NOT EXISTS deliveries (
  idempotency_key TEXT PRIMARY KEY,
  record TEXT NOT NULL,
  ts TEXT NOT NULL
)"""


def idempotency_key(canonical_hash, target_session_id):
    return hashlib.sha256(
        ("%s|%s" % (canonical_hash, target_session_id)).encode("utf-8")
    ).hexdigest()


class DeliveryLedger:
    def __init__(self, sqlite_path, jsonl_path=None):
        self.sqlite_path = os.path.realpath(sqlite_path)
        self.jsonl_path = jsonl_path or (os.path.splitext(self.sqlite_path)[0] +
                                         ".jsonl")
        os.makedirs(os.path.dirname(os.path.abspath(sqlite_path)),
                    exist_ok=True)
        self._mutex = threading.RLock()
        self._conn = sqlite3.connect(self.sqlite_path, check_same_thread=False)
        self._conn.execute(SCHEMA)
        self._conn.commit()

    def _sanitize(self, record):
        rec = {k: v for k, v in dict(record).items()
               if k not in ("text", "payload", "body")}
        return rec

    @contextmanager
    def delivery_lock(self, key):
        """Coordinate the whole attempt across threads/processes and restarts.

        A crash releases the lock, never the persisted uncertain attempt.
        Local POSIX filesystem locking is required (not network filesystems).
        """
        filename = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
        directory = self.sqlite_path + ".locks"
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, filename), "a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _mirror(self, rec):
        # SQLite commits first. A failed mirror must never turn an acknowledged
        # write into an exception/replay; readers must consult SQLite.
        try:
            with open(self.jsonl_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(rec, sort_keys=True) + "\n")
        except OSError:
            pass

    @_synchronized
    def record(self, delivery):
        """Insert; on UNIQUE conflict return ('duplicate', original)."""
        rec = self._sanitize(delivery)
        rec.setdefault("ts_utc", utcnow_rfc3339())
        rec.setdefault("delivery_state", "delivered" if
                       rec.get("result") == "delivered" else "unknown")
        rec.setdefault("revision", 1)
        key = rec.get("idempotency_key")
        if not key:
            raise ValueError("delivery needs idempotency_key")
        with self._mutex:
            try:
                self._conn.execute(
                    "INSERT INTO deliveries(idempotency_key, record, ts) "
                    "VALUES (?,?,?)",
                    (key, json.dumps(rec, sort_keys=True), rec["ts_utc"]))
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return "duplicate", self.get(key)
            except sqlite3.Error:
                self._conn.rollback()
                raise
            self._mirror(rec)
        return "inserted", rec

    @_synchronized
    def transition(self, key, state, **metadata):
        """Update under delivery_lock; retain a monotonically numbered mirror."""
        if state not in {"attempting", "delivered", "rejected", "unknown"}:
            raise ValueError("invalid delivery state")
        with self._mutex:
            rec = self.get(key)
            if rec is None:
                raise ValueError("unknown delivery key")
            rec.update(self._sanitize(metadata))
            rec.update(delivery_state=state,
                       result="delivered" if state == "delivered" else "escalated",
                       ts_utc=utcnow_rfc3339(),
                       revision=rec.get("revision", 0) + 1)
            try:
                self._conn.execute(
                    "UPDATE deliveries SET record=?, ts=? WHERE idempotency_key=?",
                    (json.dumps(rec, sort_keys=True), rec["ts_utc"], key))
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise
            self._mirror(rec)
            return rec

    @_synchronized
    def get(self, key):
        with self._mutex:
            row = self._conn.execute(
                "SELECT record FROM deliveries WHERE idempotency_key=?",
                (key,)).fetchone()
        if row is None:
            return None
        rec = json.loads(row[0])
        rec.setdefault("delivery_state", "delivered" if
                       rec.get("result") == "delivered" else "unknown")
        return rec

    @_synchronized
    def find_by_message(self, message_id):
        cur = self._conn.execute("SELECT record FROM deliveries")
        for (blob,) in cur.fetchall():
            rec = json.loads(blob)
            if rec.get("message_id") == message_id:
                return rec
        return None

    @_synchronized
    def recent_for_session(self, session_id, limit=20):
        cur = self._conn.execute(
            "SELECT record FROM deliveries ORDER BY ts DESC LIMIT ?",
            (limit * 5,))
        out = []
        for (blob,) in cur.fetchall():
            rec = json.loads(blob)
            if rec.get("target_session_id") == session_id:
                out.append(rec)
                if len(out) >= limit:
                    break
        return out

    @_synchronized
    def count(self):
        cur = self._conn.execute("SELECT COUNT(*) FROM deliveries")
        return cur.fetchone()[0]

    @_synchronized
    def anonymize_session(self, session_id):
        """Forget-verb helper: strip message links for a session's rows."""
        cur = self._conn.execute("SELECT idempotency_key, record "
                                 "FROM deliveries")
        n = 0
        for key, blob in cur.fetchall():
            rec = json.loads(blob)
            if rec.get("target_session_id") == session_id:
                rec["message_id"] = "forgotten"
                rec["canonical_hash"] = "forgotten"
                rec["anonymized"] = True
                self._conn.execute(
                    "UPDATE deliveries SET record=? WHERE idempotency_key=?",
                    (json.dumps(rec, sort_keys=True), key))
                n += 1
        self._conn.commit()
        return n

    @_synchronized
    def drop_session(self, session_id):
        cur = self._conn.execute("SELECT idempotency_key, record "
                                 "FROM deliveries")
        keys = [k for k, blob in cur.fetchall()
                if json.loads(blob).get("target_session_id") == session_id]
        for k in keys:
            self._conn.execute(
                "DELETE FROM deliveries WHERE idempotency_key=?", (k,))
        self._conn.commit()
        return len(keys)

    @_synchronized
    def close(self):
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass
