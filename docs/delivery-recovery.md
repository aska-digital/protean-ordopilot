# Delivery outcomes and explicit recovery

The delivery key is `sha256(canonical_hash + "|" + catalog_session_id)`.
Transport session IDs never enter that key. Approval, route-depth, paused-topic
and seat gates apply before delivery and again before recovery.

SQLite is authoritative. Each key has one row with a state and revision:

| State | Meaning | Ordinary repeat | Explicit recovery |
| --- | --- | --- | --- |
| attempting | Committed before calling the adapter | Becomes unknown once its lock is released | Resolve as unknown |
| delivered | Adapter returned success or recovery supplied evidence | duplicate-ack | duplicate-ack |
| rejected | Adapter explicitly guarantees no side effect | escalated | Safe new attempt |
| unknown | Timeout, generic error, interrupted attempt, or legacy escalated row | escalated | Reconcile, durable keyed replay, or evidence-bearing operator assertion |

Attempts and state transitions are committed before releasing a POSIX file
lock for that delivery key. Concurrent processes using the same ledger cannot
race the external append. Crashes release the lock but retain the uncertain
attempt. Use a shared ledger on a local filesystem with reliable POSIX locking;
separate ledger files do not coordinate. JSONL mirrors committed transitions
with increasing revision numbers. A mirror I/O failure cannot undo an SQLite
commit; the mirror may be incomplete. Neither message text, exception contents,
raw receipts nor operator evidence references are written into delivery rows.

## Public recovery API

Use the original redacted message and the original catalog target. The stored
canonical hash, supplied delivery key and target must agree. New attempts
also retain a separate hash of the delivered redacted text, which recovery
validates. Intake's canonical hash precedes redaction and remains unchanged;
it must not be recomputed from redacted text. Legacy rows lack a payload hash,
so the caller must supply their original redacted payload; only their key,
canonical hash and target can be validated. Recovery cannot create an
unrecorded delivery.

```python
from courier.dispatcher import recover_delivery

result = recover_delivery(
    original_message,
    {"kind": "CONTINUE", "target_session_id": original_catalog_target},
    catalog, ledger, backend,
    delivery_key=original_delivery_key,
)
```

A confirmed rejection retries once through the normal adapter. Unknown outcomes
stay escalated when the backend cannot provide durable replay/reconciliation.
There is no automatic retry of legacy escalated records: their outcomes are
unknown. Never delete those records merely to force delivery.

An operator may resolve an unknown attempt only after examining independent
evidence, such as a backend receipt or an authoritative no-write audit:

```python
result = recover_delivery(
    original_message, original_decision, catalog, ledger, backend,
    delivery_key=original_delivery_key,
    resolution="delivered",  # or "rejected" to authorize a safe retry
    evidence_id="opaque-reference-to-the-reviewed-evidence",
)
```

The evidence reference must be nonempty. Its hash and
`confirmation_source="operator-assertion"` are recorded. This API does not
verify the evidence or confer backend certainty: the caller must authenticate
the operator and validate the assertion. Missing or eventually consistent logs
are insufficient proof of rejection. A delivered assertion returns an
acknowledgement; a rejected assertion permits a new attempt. After a successful
new append, its confirmation source reflects the adapter result instead.

## Adapter contract

Existing `append_message(session_id, message)` adapters keep working. A return
with `{"ok": True, ...}` confirms completion; unsuccessful or malformed return
values remain unknown. `DeliveryRejected` is reserved for failures that the
adapter guarantees caused no external side effect. Every other exception,
including a timeout, is unknown. Success receipts are hashed before storage.

A backend may optionally implement `DurableDeliveryBackend` and advertise
`durable_delivery_keys = True`, providing:

- `append_delivery(session_id, message, delivery_key)` returning
  `DeliveryEvidence("delivered", receipt_id=...)` or a proven rejection.
- `lookup_delivery(session_id, delivery_key)` returning `DeliveryEvidence`.
  A delivered outcome requires a nonempty durable receipt ID; rejection
  requires a nonempty evidence ID establishing no write; all other results
  are unknown. A transiently absent lookup must return unknown.

This opt-in is a strong backend guarantee: every repeated or concurrent call
for the same key performs at most one side effect and returns the original
receipt, including after client or backend restart. An unknown recovery first
looks up evidence, then may replay only through this durable keyed method.
A lookup failure or unknown result never grants permission for an unkeyed
append. An authoritative rejection means no competing or pending write for
that key can later take effect.

`LocalBackend` is an in-memory stand-in. Its `fail_next` and unknown-session
append errors are confirmed pre-write rejection; timeouts remain unknown.
It does not advertise durable keys and provides no delivery durability across
restarts. The Hermes module is a CLI/hook helper, not a complete delivery
adapter; no durable provider capability is claimed here. Tests exercise the
optional contract with a synthetic temporary SQLite backend only.
