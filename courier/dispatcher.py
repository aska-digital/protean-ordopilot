"""Dispatcher: RouteDecision + seat/ledger handles -> dispatch result.

Order is fixed: approval gate FIRST, then route-depth, paused-topic park,
seat/CAS, ledger idempotency, deliver. Any check failure => hold/escalate,
never force.
"""

from datetime import datetime, timezone
import hashlib
import json

from . import MAX_ROUTE_DEPTH, SEAT_TTL_S
from . import approvals as approvals_mod
from .adapters.base import DeliveryEvidence, DeliveryRejected
from .catalog import SeatError
from .intake import canonical_hash, utcnow_rfc3339
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
             record_steps=None, _recovery=None):
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

    if _recovery is not None:
        original = ledger.get(_recovery["delivery_key"])
        recovery_target = decision.get("target_session_id")
        if (original is None or
                original.get("canonical_hash") != message["canonical_hash"] or
                (original.get("delivery_payload_hash") is not None and
                 original["delivery_payload_hash"] != canonical_hash(message["text"])) or
                original.get("target_session_id") != recovery_target or
                idempotency_key(message["canonical_hash"], recovery_target) != _recovery["delivery_key"]):
            raise ValueError("recovery requires the original canonical hash, catalog target and delivery key")

    kind = decision["kind"]
    if kind == "ESCALATE":
        return {**base, "result": "parked-clarification",
                "reason": decision.get("reason", "escalated"),
                "target_session_id": decision.get("target_session_id")}

    target_id = decision.get("target_session_id")
    deliver_id = target_id
    if kind == "NEW":
        # Materialize the fresh session behind the scenes unless the
        # decision already names a live one.
        live = catalog.get(target_id) if target_id else None
        if live is None and decision.get("target_slug"):
            live = catalog.get(decision["target_slug"])
        if live is not None:
            target_id = live["session_id"]
            deliver_id = target_id
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
                    deliver_id = created["session_id"]
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
            catalog.acquire_seat(session["slug"], holder)
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

    # 5. Serialize each key before persisting an attempt and calling the backend.
    key = idempotency_key(message["canonical_hash"], target_id)
    with ledger.delivery_lock(key):
        existing = ledger.get(key)
        if existing is not None and existing["delivery_state"] == "attempting":
            # Previous owner crashed or its commit failed. Absence of an ack
            # proves nothing about the external side effect.
            existing = ledger.transition(key, "unknown", reason="interrupted-attempt")
        if existing is not None and existing["delivery_state"] == "delivered":
            return {**base, "result": "duplicate-ack",
                    "reason": "confirmed delivery already recorded",
                    "target_session_id": target_id,
                    "idempotency_key": key, "original": existing}

        transport_id = session.get("backend_session_id") or deliver_id
        if existing is not None:
            transport_id = existing.get("transport_session_id", transport_id)
            if _recovery is None:
                return _unresolved(base, target_id, key, existing)
            if existing["delivery_state"] == "unknown":
                existing = _resolve_unknown(key, transport_id, ledger, backend,
                                            _recovery)
                if existing["delivery_state"] == "delivered":
                    return {**base, "result": "duplicate-ack",
                            "target_session_id": target_id,
                            "idempotency_key": key, "original": existing,
                            "reason": "delivery confirmed by recovery evidence"}
                if existing["delivery_state"] != "rejected" and not _durable(backend):
                    return _unresolved(base, target_id, key, existing)
            ledger.transition(key, "attempting", reason="recovery-attempt",
                              attempt=existing.get("attempt", 1) + 1)
        else:
            ledger.record({
                "idempotency_key": key,
                "message_id": message["message_id"],
                "canonical_hash": message["canonical_hash"],
                "delivery_payload_hash": canonical_hash(message["text"]),
                "target_session_id": target_id,
                "transport_session_id": transport_id,
                "decision_kind": kind,
                "provider": decision.get("provider"),
                "model": decision.get("model"),
                "delivery_state": "attempting", "attempt": 1,
                "result": "escalated"})

        # 6. Unmodified adapters remain valid; only opted-in durable adapters
        # receive keys and may safely replay an uncertain attempt.
        try:
            if _durable(backend):
                evidence = backend.append_delivery(transport_id, message, key)
                state, metadata = _evidence(evidence)
            else:
                receipt = backend.append_message(transport_id, message)
                # Existing adapter success means confirmed completion; an
                # explicit unsuccessful/missing receipt remains uncertain.
                state = "delivered" if isinstance(receipt, dict) and receipt.get("ok") is True else "unknown"
                metadata = {"receipt_hash": _reference_hash(receipt),
                            "confirmation_source": "adapter-return"} if state == "delivered" else {}
        except DeliveryRejected:
            state, metadata = "rejected", {}
        except Exception:  # noqa: BLE001 — timeout/error text may contain secrets
            state, metadata = "unknown", {}
        stored = ledger.transition(key, state, reason="backend-" + state, **metadata)
        if state != "delivered":
            return _unresolved(base, target_id, key, stored)
        out = {**base, "result": "delivered", "target_session_id": target_id,
               "idempotency_key": key, "delivery_state": state}
        if record_steps is not None:
            out["steps"] = record_steps
        return out


def recover_delivery(message, decision, catalog, ledger, backend,
                     holder="courier", *, delivery_key, resolution=None, evidence_id=None):
    """Explicit recovery; approval/route/seat gates run before any recovery.

    Confirmed rejection can retry. Unknown outcomes require a durable adapter
    or evidence-bearing operator resolution: delivered or rejected. A rejected
    resolution asserts no external side effect; it authorizes a new attempt.
    The caller is responsible for validating operator evidence. Only its hash
    is retained. Never use this API to infer rejection from missing logs.
    """
    if resolution not in (None, "delivered", "rejected"):
        raise ValueError("resolution must be delivered or rejected")
    if resolution is not None and (not isinstance(evidence_id, str) or not evidence_id.strip()):
        raise ValueError("operator resolution requires an evidence reference")
    return dispatch(message, decision, catalog, ledger, backend, holder=holder,
                    _recovery={"delivery_key": delivery_key, "resolution": resolution,
                               "evidence_id": evidence_id})


def _reference_hash(reference):
    return hashlib.sha256(json.dumps(reference, sort_keys=True).encode("utf-8")).hexdigest()


def _durable(backend):
    return (getattr(backend, "durable_delivery_keys", False) is True and
            callable(getattr(backend, "append_delivery", None)) and
            callable(getattr(backend, "lookup_delivery", None)))


def _evidence(evidence):
    if not isinstance(evidence, DeliveryEvidence):
        return "unknown", {}
    if evidence.outcome == "delivered" and isinstance(evidence.receipt_id, str) and evidence.receipt_id.strip():
        return "delivered", {"receipt_hash": _reference_hash(evidence.receipt_id),
                             "confirmation_source": "durable-backend"}
    if evidence.outcome == "rejected" and isinstance(evidence.evidence_id, str) and evidence.evidence_id.strip():
        return "rejected", {"evidence_hash": _reference_hash(evidence.evidence_id),
                            "confirmation_source": "durable-backend"}
    return "unknown", {}


def _resolve_unknown(key, transport_id, ledger, backend, recovery):
    resolution = recovery.get("resolution")
    if resolution is not None:
        return ledger.transition(key, resolution, reason="operator-resolution",
                                 evidence_hash=_reference_hash(recovery["evidence_id"]),
                                 confirmation_source="operator-assertion")
    if _durable(backend):
        try:
            state, metadata = _evidence(backend.lookup_delivery(transport_id, key))
        except Exception:  # noqa: BLE001
            state, metadata = "unknown", {}
        return ledger.transition(key, state, reason="backend-reconciliation", **metadata)
    return ledger.get(key)


def _unresolved(base, target_id, key, record):
    return {**base, "result": "escalated",
            "reason": "delivery-%s: explicit recovery required" % record["delivery_state"],
            "delivery_state": record["delivery_state"],
            "target_session_id": target_id, "idempotency_key": key,
            "original": record}


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
