"""In-process fake backend for tests and offline CLI use.

Implements SessionBackend with fault injection (fail_next / timeout_next)
so dispatcher fallback paths are testable without network.
"""

import uuid

from .base import DeliveryRejected, SessionBackend  # noqa: F401


class BackendError(Exception):
    pass


class BackendRejected(DeliveryRejected, BackendError):
    pass


class BackendTimeout(BackendError):
    pass


class LocalBackend:
    def __init__(self):
        self.sessions = {}
        self.appended = []  # [(session_id, message_id)]
        self.fail_next = None
        self.timeout_next = False

    def list_sessions(self):
        return [{"session_id": s["session_id"], "slug": s["slug"],
                 "summary": s.get("summary", "")}
                for s in self.sessions.values()]

    def get_session(self, session_id):
        for s in self.sessions.values():
            if s["session_id"] == session_id or s.get("slug") == session_id:
                return dict(s)
        return None

    def seed(self, slug, summary="", session_id=None):
        ref = {"session_id": session_id or "sess-" + uuid.uuid4().hex[:8],
               "slug": slug, "summary": summary, "messages": []}
        self.sessions[slug] = ref
        return dict(ref)

    def create_session(self, slug, summary=""):
        if slug in self.sessions:
            raise BackendError("exists: %s" % slug)
        return self.seed(slug, summary)

    def _maybe_fail(self):
        if self.timeout_next:
            self.timeout_next = False
            raise BackendTimeout("injected timeout")
        if self.fail_next is not None:
            err = self.fail_next
            self.fail_next = None
            raise BackendRejected(err)

    def append_message(self, session_id, message):
        self._maybe_fail()
        target = self.get_session(session_id)
        if target is None:
            raise BackendRejected("unknown session: %s" % session_id)
        self.sessions[target["slug"]]["messages"].append(message)
        self.appended.append((target["session_id"],
                              message.get("message_id")))
        return {"ok": True, "session_id": target["session_id"]}

    def resume_session(self, session_id, prompt):
        self._maybe_fail()
        target = self.get_session(session_id)
        if target is None:
            raise BackendRejected("unknown session: %s" % session_id)
        return {"ok": True, "session_id": target["session_id"],
                "prompt_chars": len(prompt)}
