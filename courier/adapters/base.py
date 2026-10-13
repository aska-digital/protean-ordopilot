"""SessionBackend protocol: the only seam between core and session stores.

Core codes to this protocol; Hermes/local/fake backends implement it.
"""

from dataclasses import dataclass
from typing import Protocol


class DeliveryRejected(Exception):
    """Adapter guarantees this attempt caused no external side effect."""


@dataclass(frozen=True)
class DeliveryEvidence:
    """Backend evidence; references must be opaque IDs, never payload text.

    delivered requires a durable receipt_id; rejected requires evidence_id
    establishing no side effect. unknown must not authorize replay.
    """
    outcome: str
    receipt_id: str = ""
    evidence_id: str = ""


class DurableDeliveryBackend(Protocol):
    """Optional capability; advertise durable_delivery_keys = True only when
    keys and original receipts survive backend/client process restarts.
    Concurrent/repeated append_delivery calls for a key MUST perform at most
    one side effect and return its original DeliveryEvidence. lookup_delivery
    must never report rejected merely because an eventual index is empty.
    """
    durable_delivery_keys: bool

    def append_delivery(self, session_id, message, delivery_key):
        ...  # pragma: no cover

    def lookup_delivery(self, session_id, delivery_key):
        ...  # pragma: no cover


class SessionBackend(Protocol):
    def list_sessions(self):
        """Return [{session_id, slug, summary}]."""
        ...  # pragma: no cover

    def get_session(self, session_id):
        """Return session dict or None."""
        ...  # pragma: no cover

    def create_session(self, slug, summary=""):
        """Create and return session dict."""
        ...  # pragma: no cover

    def append_message(self, session_id, message):
        """Deliver a (redacted) message; return receipt dict."""
        ...  # pragma: no cover

    def resume_session(self, session_id, prompt):
        """Resume a session with a prompt; return result dict."""
        ...  # pragma: no cover
