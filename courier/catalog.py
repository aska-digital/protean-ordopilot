"""Session catalog: Eldunari routing PROJECTION with CAS + seat leases.

Reads are lease-free. Writes require a live seat + expected_rev (integer
CAS); stale revision -> ConflictError, never silent overwrite. The
projection is pointer-only (slug/summary/seat/revision), never transcript.
Writes serialize under flock(<state_home>/.lock).
"""

import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import SEAT_TTL_S, SESSION_SUMMARY_BUDGET_CHARS
from .intake import utcnow_rfc3339

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


class ConflictError(Exception):
    """Stale expected_rev (CLI exit 3)."""


class SeatError(Exception):
    """Seat held live by another holder (CLI exit 4)."""


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _seat_live(ref, now=None):
    if not ref.get("seat_holder"):
        return False
    exp = _parse_ts(ref.get("seat_expires_utc"))
    if exp is None:
        return False
    now = now or datetime.now(timezone.utc)
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp > now


class SessionCatalog:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock_path = os.path.join(os.path.dirname(
            os.path.abspath(path)), ".lock")

    # -- storage ------------------------------------------------------
    @contextmanager
    def _locked(self):
        open(self._lock_path, "a").close()
        with open(self._lock_path, "r+") as lf:
            if fcntl is not None:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self):
        if not os.path.exists(self.path):
            return {"sessions": {}, "events": []}
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("sessions", {})
        data.setdefault("events", [])
        return data

    def _write_unlocked(self, data):
        d = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _mutate(self, fn):
        with self._locked():
            data = self._read_unlocked()
            out = fn(data)
            self._write_unlocked(data)
            return out

    # -- reads (lease-free) -------------------------------------------
    def list_sessions(self):
        return sorted(self._read_unlocked()["sessions"].values(),
                      key=lambda s: s.get("slug", ""))

    def get(self, slug_or_id):
        for s in self._read_unlocked()["sessions"].values():
            if s.get("slug") == slug_or_id or \
                    s.get("session_id") == slug_or_id:
                return dict(s)
        return None

    def events(self, limit=50):
        return self._read_unlocked()["events"][-limit:]

    # -- session lifecycle ---------------------------------------------
    def create_session(self, slug, summary="", session_id=None):
        slug = slug.strip().lower()
        if not slug:
            raise ValueError("slug required")
        summary = summary[:SESSION_SUMMARY_BUDGET_CHARS]

        def _fn(data):
            if slug in data["sessions"]:
                raise ConflictError("session slug exists: %s" % slug)
            ref = {"session_id": session_id or "sess-" + uuid.uuid4().hex[:8],
                   "slug": slug, "revision": 1, "seat_holder": None,
                   "seat_expires_utc": None, "seat_epoch": 0,
                   "summary": summary, "updated_rev": 1, "paused": False}
            data["sessions"][slug] = ref
            data["events"].append({"type": "session-created", "slug": slug,
                                   "session_id": ref["session_id"],
                                   "ts_utc": utcnow_rfc3339()})
            return dict(ref)
        return self._mutate(_fn)

    def _require_live_seat(self, data, slug, holder=None):
        ref = data["sessions"].get(slug)
        if ref is None:
            raise KeyError("unknown session slug: %s" % slug)
        if not _seat_live(ref):
            raise SeatError("no live seat for '%s' (expired or absent)" % slug)
        if holder is not None and ref.get("seat_holder") != holder:
            raise SeatError("seat for '%s' held by %s"
                            % (slug, ref.get("seat_holder")))
        return ref

    def update_session(self, slug, expected_rev, summary=None, holder=None,
                       extra=None):
        """CAS-guarded mutation. Raises ConflictError on stale revision,
        SeatError when a live seat is required but missing/foreign."""

        def _fn(data):
            ref = data["sessions"].get(slug)
            if ref is None:
                raise KeyError("unknown session slug: %s" % slug)
            if int(ref["revision"]) != int(expected_rev):
                raise ConflictError(
                    "stale revision for '%s': expected %s, have %s"
                    % (slug, expected_rev, ref["revision"]))
            self._require_live_seat(data, slug, holder)
            if summary is not None:
                ref["summary"] = summary[:SESSION_SUMMARY_BUDGET_CHARS]
            if extra:
                ref.update(extra)
            ref["revision"] = int(ref["revision"]) + 1
            ref["updated_rev"] = ref["revision"]
            data["events"].append({"type": "session-updated", "slug": slug,
                                   "revision": ref["revision"],
                                   "ts_utc": utcnow_rfc3339()})
            return dict(ref)
        return self._mutate(_fn)

    # -- seats ----------------------------------------------------------
    def acquire_seat(self, slug_or_id, holder, ttl_s=SEAT_TTL_S):
        def _fn(data):
            ref = self._by_id(data, slug_or_id)
            if _seat_live(ref):
                if ref.get("seat_holder") == holder:
                    ref["seat_expires_utc"] = self._exp(ttl_s)
                    self._write_unlocked(data)
                    return dict(ref)
                raise SeatError("seat for '%s' live-held by %s"
                                % (ref["slug"], ref.get("seat_holder")))
            ref["seat_holder"] = holder
            ref["seat_expires_utc"] = self._exp(ttl_s)
            data["events"].append({"type": "seat-acquired",
                                   "slug": ref["slug"], "holder": holder,
                                   "ts_utc": utcnow_rfc3339()})
            return dict(ref)
        return self._mutate(_fn)

    def release_seat(self, slug_or_id, holder):
        def _fn(data):
            ref = self._by_id(data, slug_or_id)
            if ref.get("seat_holder") != holder:
                raise SeatError("cannot release seat held by %s"
                                % ref.get("seat_holder"))
            ref["seat_holder"] = None
            ref["seat_expires_utc"] = None
            data["events"].append({"type": "seat-released",
                                   "slug": ref["slug"], "holder": holder,
                                   "ts_utc": utcnow_rfc3339()})
            return dict(ref)
        return self._mutate(_fn)

    def steal_seat(self, slug_or_id, holder, ttl_s=SEAT_TTL_S):
        """Explicit --steal takeover of a LIVE seat; always recorded."""

        def _fn(data):
            ref = self._by_id(data, slug_or_id)
            prev = ref.get("seat_holder")
            ref["seat_holder"] = holder
            ref["seat_expires_utc"] = self._exp(ttl_s)
            ref["seat_epoch"] = int(ref.get("seat_epoch", 0)) + 1
            data["events"].append({"type": "seat-takeover",
                                   "slug": ref["slug"], "from": prev,
                                   "to": holder, "explicit_steal": True,
                                   "epoch": ref["seat_epoch"],
                                   "ts_utc": utcnow_rfc3339()})
            return dict(ref)
        return self._mutate(_fn)

    def takeover_expired(self, slug_or_id, holder, ttl_s=SEAT_TTL_S):
        """Claim an EXPIRED/absent seat with epoch bump + takeover event."""

        def _fn(data):
            ref = self._by_id(data, slug_or_id)
            if _seat_live(ref):
                raise SeatError("seat live; use --steal for explicit takeover")
            prev = ref.get("seat_holder")
            ref["seat_holder"] = holder
            ref["seat_expires_utc"] = self._exp(ttl_s)
            ref["seat_epoch"] = int(ref.get("seat_epoch", 0)) + 1
            data["events"].append({"type": "seat-takeover",
                                   "slug": ref["slug"], "from": prev,
                                   "to": holder, "explicit_steal": False,
                                   "epoch": ref["seat_epoch"],
                                   "ts_utc": utcnow_rfc3339()})
            return dict(ref)
        return self._mutate(_fn)

    @staticmethod
    def _exp(ttl_s):
        return (datetime.now(timezone.utc) +
                timedelta(seconds=ttl_s)).isoformat(timespec="seconds")

    @staticmethod
    def _by_id(data, slug_or_id):
        for s in data["sessions"].values():
            if s.get("slug") == slug_or_id or \
                    s.get("session_id") == slug_or_id:
                return s
        raise KeyError("unknown session: %s" % slug_or_id)

    def seat_live(self, slug_or_id):
        ref = self.get(slug_or_id)
        return bool(ref) and _seat_live(ref)

    # -- user-control verbs ----------------------------------------------
    def set_paused(self, slug, paused):
        def _fn(data):
            ref = data["sessions"].get(slug)
            if ref is None:
                raise KeyError("unknown session slug: %s" % slug)
            ref["paused"] = bool(paused)
            data["events"].append({"type": "session-resumed" if not paused
                                   else "session-paused", "slug": slug,
                                   "ts_utc": utcnow_rfc3339()})
            return dict(ref)
        return self._mutate(_fn)

    def forget(self, slug):
        def _fn(data):
            if slug not in data["sessions"]:
                raise KeyError("unknown session slug: %s" % slug)
            ref = data["sessions"].pop(slug)
            data["events"].append({"type": "session-forgotten", "slug": slug,
                                   "session_id": ref["session_id"],
                                   "ts_utc": utcnow_rfc3339()})
            return dict(ref)
        return self._mutate(_fn)

    def merge(self, slug_a, slug_b, into, holder=None):
        def _fn(data):
            for s in (slug_a, slug_b):
                if s not in data["sessions"]:
                    raise KeyError("unknown session slug: %s" % s)
            if into in data["sessions"] and into not in (slug_a, slug_b):
                raise ConflictError("target slug exists: %s" % into)
            a, b = data["sessions"][slug_a], data["sessions"][slug_b]
            if holder is not None:
                for ref in (a, b):
                    if not _seat_live(ref) or \
                            ref.get("seat_holder") != holder:
                        raise SeatError("live seat required for '%s'"
                                        % ref["slug"])
            merged_summary = ((a.get("summary", "") + " | " +
                               b.get("summary", "")).strip(" |")) \
                [:SESSION_SUMMARY_BUDGET_CHARS]
            ref = {"session_id": "sess-" + uuid.uuid4().hex[:8], "slug": into,
                   "revision": 1, "seat_holder": None,
                   "seat_expires_utc": None, "seat_epoch": 0,
                   "summary": merged_summary, "updated_rev": 1,
                   "paused": False, "merged_from": [slug_a, slug_b]}
            for s in (slug_a, slug_b):
                data["sessions"].pop(s, None)
            data["sessions"][into] = ref
            data["events"].append({"type": "sessions-merged",
                                   "from": [slug_a, slug_b], "into": into,
                                   "session_id": ref["session_id"],
                                   "ts_utc": utcnow_rfc3339()})
            return dict(ref)
        return self._mutate(_fn)

    def split(self, slug, new_slug, at_message_id=None, holder=None):
        def _fn(data):
            ref = data["sessions"].get(slug)
            if ref is None:
                raise KeyError("unknown session slug: %s" % slug)
            if new_slug in data["sessions"]:
                raise ConflictError("target slug exists: %s" % new_slug)
            if holder is not None:
                self._require_live_seat(data, slug, holder)
            new_ref = {"session_id": "sess-" + uuid.uuid4().hex[:8],
                       "slug": new_slug, "revision": 1, "seat_holder": None,
                       "seat_expires_utc": None, "seat_epoch": 0,
                       "summary": ref.get("summary", "")[
                           :SESSION_SUMMARY_BUDGET_CHARS],
                       "updated_rev": 1, "paused": False,
                       "split_from": slug}
            data["sessions"][new_slug] = new_ref
            data["events"].append({"type": "session-split", "from": slug,
                                   "into": new_slug, "at": at_message_id,
                                   "session_id": new_ref["session_id"],
                                   "ts_utc": utcnow_rfc3339()})
            return dict(new_ref)
        return self._mutate(_fn)

    def record_event(self, event):
        evt = dict(event)
        evt.setdefault("ts_utc", utcnow_rfc3339())

        def _fn(data):
            data["events"].append(evt)
            return evt
        return self._mutate(_fn)
