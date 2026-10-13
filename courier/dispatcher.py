"""Dispatcher: RouteDecision + seat/ledger handles -> dispatch result.

Order is fixed: approval gate FIRST, then route-depth, paused-topic park,
seat/CAS, ledger idempotency, deliver. Any check failure => hold/escalate,
never force.
"""

from datetime import datetime, timezone

from . import MAX_ROUTE_DEPTH, SEAT_TTL_S
from . import approvals as approvals_mod
from .catalog import SeatError
from .intake import utcnow_rfc3339
from .ledger import idempotency_key


def _parse_ts(ts):
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def dispatch(message, decision, catalog, ledger, backend, holder="courier",
             record_steps=None):
    """Returns result dict with result in {delivered, duplicate-ack,
    held-for-approval, escalated, parked-busy, parked-clarification}."""
    ts = utcnow_rfc3339()
    base = {"message_id": message["message_id"],
            "canonical_hash": message["canonical_hash"],
            "decision_kind": decision["kind"],
            "provider": decision.get("provider"),
            "model": decision.get("model"),
            "ts_utc": ts}

    # 1. Approval gate first.
    gate, info = approvals_mod.check(message, decision)
    if gate == "APPROVAL_REQUIRED":
        info = info or {}
        return {**base, "result": "held-for-approval",
                "reason": info["reason"],
                "action_class": info["action_class"],
                "target_session_id": decision.get("target_session_id")}

    # 2. Loop prevention.
    if message.get("route_depth", 0) > MAX_ROUTE_DEPTH or \
            decision.get("route_depth", 0) > MAX_ROUTE_DEPTH:
        return {**base, "result": "escalated",
                "reason": "route-depth-overflow",
                "target_session_id": decision.get("target_session_id")}

    kind = decision["kind"]
    if kind == "ESCALATE":
        return {**base, "result": "parked-clarification",
                "reason": decision.get("reason", "escalated"),
                "target_session_id": decision.get("target_session_id")}

    target_id = decision.get("target_session_id")
    backend_session_id = None
    if kind == "NEW":
        # Materialize the fresh session behind the scenes unless the
        # decision already names a live one.
        live = catalog.get(target_id) if target_id else None
        if live is None and decision.get("target_slug"):
            live = catalog.get(decision["target_slug"])
        if live is not None:
            target_id = live["session_id"]
            decision = dict(decision, target_session_id=target_id,
                            target_slug=live.get("slug"))
        else:
            slug = (decision.get("target_slug") or
                    ("topic-" + message["message_id"][:8])).lower()
            if catalog.get(slug) is not None:
                slug = "%s-%s" % (slug, message["message_id"][:4])
            ref = catalog.create_session(
                slug, summary=message["text"][:500],
                session_id=target_id or
                "sess-" + message["message_id"][:8])
            target_id = ref["session_id"]
            decision = dict(decision, target_session_id=target_id,
                            target_slug=slug)
            # The fresh session must exist on the delivery backend too;
            # the catalog id stays authoritative for ledger + results.
            try:
                backend.create_session(slug, summary=message["text"][:500])
            except TypeError:
                try:
                    backend.create_session(slug, message["text"][:500])
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001 — exists is fine
                pass
            try:
                created = backend.get_session(slug)
                if created and created.get("session_id"):
                    backend_session_id = created["session_id"]
            except Exception:  # noqa: BLE001
                pass
    if not target_id:
        return {**base, "result": "escalated",
                "reason": "no-target-session",
                "target_session_id": None}

    session = catalog.get(target_id)
    if session is None and decision.get("target_slug"):
        session = catalog.get(decision["target_slug"])
        if session is not None:
            target_id = session["session_id"]
    if session is None:
        return {**base, "result": "escalated",
                "reason": "unknown-target-session",
                "target_session_id": target_id}

    # 3. Paused topics park into clarification (OPEN-4).
    if session.get("paused"):
        return {**base, "result": "parked-clarification",
                "reason": "topic-paused: '%s'" % session.get("slug"),
                "target_session_id": target_id}

    # 4. Seat handling: live-held-by-other + recent ledger activity => busy.
    try:
        try:
            session = catalog.acquire_seat(session["slug"], holder)
        except SeatError:
            if _session_busy(session, ledger):
                return {**base, "result": "parked-busy",
                        "reason": "busy(session_id=%s)" % target_id,
                        "target_session_id": target_id}
            # Seat live but idle: caller must --steal explicitly.
            return {**base, "result": "escalated",
                    "reason": "seat-held-by-%s (use --steal)"
                    % session.get("seat_holder"),
                    "target_session_id": target_id}
    except SeatError:
        return {**base, "result": "escalated",
                "reason": "seat-unavailable",
                "target_session_id": target_id}

    # Preserve the backend's identifier across later dispatches. Routing
    # and ledger keys continue to use the catalog's stable identifier.
    if backend_session_id and \
            session.get("backend_session_id") != backend_session_id:
        session = catalog.update_session(
            session["slug"], session["revision"], holder=holder,
            extra={"backend_session_id": backend_session_id})
    deliver_id = session.get("backend_session_id") or target_id

    # 5. Ledger idempotency before any side effect.
    key = idempotency_key(message["canonical_hash"], target_id)
    existing = ledger.get(key)
    if existing is not None:
        return {**base, "result": "duplicate-ack",
                "reason": "duplicate idempotency key; original returned",
                "target_session_id": target_id,
                "idempotency_key": key, "original": existing}

    # 6. Deliver via backend; typed backend errors => provider-fallback mark.
    try:
        backend.append_message(deliver_id, message)
    except Exception as e:  # noqa: BLE001 — adapter raises typed errors
        status, stored = ledger.record({
            "idempotency_key": key,
            "message_id": message["message_id"],
            "canonical_hash": message["canonical_hash"],
            "target_session_id": target_id,
            "decision_kind": kind,
            "provider": decision.get("provider"),
            "model": decision.get("model"),
            "result": "escalated",
            "reason": "provider-fallback: %s" % e})
        return {**base, "result": "escalated",
                "reason": "provider-fallback: %s" % e,
                "target_session_id": target_id, "idempotency_key": key}

    status, stored = ledger.record({
        "idempotency_key": key,
        "message_id": message["message_id"],
        "canonical_hash": message["canonical_hash"],
        "target_session_id": target_id,
        "decision_kind": kind,
        "provider": decision.get("provider"),
        "model": decision.get("model"),
        "result": "delivered"})
    out = {**base, "result": "delivered" if status == "inserted"
           else "duplicate-ack",
           "target_session_id": target_id, "idempotency_key": key}
    if record_steps is not None:
        out["steps"] = record_steps
    return out


def _session_busy(session, ledger):
    """Busy = seat held live + a delivery within SEAT_TTL_S."""
    holder = session.get("seat_holder")
    if not holder:
        return False
    exp = _parse_ts(session.get("seat_expires_utc"))
    now = datetime.now(timezone.utc)
    if exp is None or exp <= now:
        return False
    for rec in ledger.recent_for_session(session["session_id"], limit=5):
        ts = _parse_ts(rec.get("ts_utc"))
        if ts and (now - ts).total_seconds() < SEAT_TTL_S:
            return True
    return False
