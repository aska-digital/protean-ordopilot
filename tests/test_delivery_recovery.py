"""Delivery failure/recovery tests use only temporary synthetic stores."""
from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
import os
import sqlite3

import pytest

from courier.adapters.base import DeliveryEvidence
from courier.adapters.local import LocalBackend
from courier.catalog import SessionCatalog
from courier.dispatcher import dispatch, recover_delivery
from courier.ledger import DeliveryLedger, idempotency_key
from tests.conftest import redacted_msg, seed


class DurableBackend:
    """Test-only durable key store: SQLite UNIQUE commits before acknowledgement."""
    durable_delivery_keys = True

    def __init__(self, path, fault=None, absent_lookup=False):
        self.path, self.fault, self.absent_lookup = path, fault, absent_lookup
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS appends (key TEXT PRIMARY KEY, target TEXT)")

    def append_delivery(self, target, message, key):
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT OR IGNORE INTO appends VALUES (?,?)", (key, target))
        if self.fault == "crash":
            os._exit(17)
        if self.fault == "timeout":
            self.fault = None
            raise TimeoutError("synthetic acknowledgement lost")
        return DeliveryEvidence("delivered", receipt_id="receipt-" + key)

    def lookup_delivery(self, target, key):
        if not self.absent_lookup:
            with sqlite3.connect(self.path) as conn:
                row = conn.execute("SELECT target FROM appends WHERE key=?", (key,)).fetchone()
            if row:
                return DeliveryEvidence("delivered", receipt_id="receipt-" + key)
        return DeliveryEvidence("unknown")

    def count(self):
        with sqlite3.connect(self.path) as conn:
            return conn.execute("SELECT count(*) FROM appends").fetchone()[0]


class NonIdempotentBackend:
    """Persistent test side effects without any delivery deduplication."""
    def __init__(self, path):
        self.path = path
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS plain_appends (id INTEGER PRIMARY KEY, target TEXT)")

    def append_message(self, target, message):
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO plain_appends(target) VALUES (?)", (target,))
        return {"ok": True, "receipt_id": "synthetic"}

    def count(self):
        with sqlite3.connect(self.path) as conn:
            return conn.execute("SELECT count(*) FROM plain_appends").fetchone()[0]


def inputs(state, text="harmless synthetic request"):
    ref = seed(state["catalog"], state["backend"], [("synthetic", "fixture")])[0]
    return redacted_msg(text), {"kind": "CONTINUE", "target_session_id": ref["session_id"]}


def run(state, msg, decision, backend=None):
    return dispatch(msg, decision, state["catalog"], state["ledger"], backend or state["backend"])


def recover(state, msg, decision, key, backend=None, **kw):
    return recover_delivery(msg, decision, state["catalog"], state["ledger"],
                            backend or state["backend"], delivery_key=key, **kw)


def test_rejection_safe_explicit_retry_and_coherent_mirror(state):
    msg, decision = inputs(state)
    state["backend"].fail_next = "secret exception text"
    first = run(state, msg, decision)
    assert first["delivery_state"] == "rejected"
    assert run(state, msg, decision)["result"] == "escalated"
    state["ledger"].close()
    state["ledger"] = DeliveryLedger(os.path.join(state["home"], "ledger.sqlite"))
    assert recover(state, msg, decision, first["idempotency_key"])["result"] == "delivered"
    assert run(state, msg, decision)["result"] == "duplicate-ack"
    assert len(state["backend"].appended) == 1
    mirror = open(state["ledger"].jsonl_path, encoding="utf-8").read()
    assert msg["text"] not in mirror and "secret exception text" not in mirror
    records = [json.loads(line) for line in mirror.splitlines()]
    assert [r["delivery_state"] for r in records] == ["attempting", "rejected", "attempting", "delivered"]
    assert [r["revision"] for r in records] == [1, 2, 3, 4]


def test_postwrite_unknown_never_blindly_replays(state):
    msg, decision = inputs(state)
    class LostAck(LocalBackend):
        def append_message(self, session_id, message):
            super().append_message(session_id, message)
            raise TimeoutError("payload secret")
    backend = LostAck()
    backend.seed("synthetic", session_id=decision["target_session_id"])
    first = run(state, msg, decision, backend)
    assert first["delivery_state"] == "unknown"
    assert run(state, msg, decision, backend)["result"] == "escalated"
    assert recover(state, msg, decision, first["idempotency_key"], backend)["delivery_state"] == "unknown"
    assert len(backend.appended) == 1
    with pytest.raises(ValueError, match="evidence"):
        recover(state, msg, decision, first["idempotency_key"], backend, resolution="delivered", evidence_id="  ")
    out = recover(state, msg, decision, first["idempotency_key"], backend,
                  resolution="delivered", evidence_id="verified-receipt-reference")
    assert out["result"] == "duplicate-ack"
    assert out["original"]["confirmation_source"] == "operator-assertion"
    assert len(backend.appended) == 1


@pytest.mark.parametrize("absent_lookup", [False, True])
def test_durable_postwrite_timeout_restart_reconcile_or_keyed_replay(state, tmp_path, absent_lookup):
    msg, decision = inputs(state)
    path = str(tmp_path / "backend.sqlite")
    backend = DurableBackend(path, "timeout")
    first = run(state, msg, decision, backend)
    assert first["delivery_state"] == "unknown"
    state["ledger"].close()
    state["ledger"] = DeliveryLedger(os.path.join(state["home"], "ledger.sqlite"))
    restarted = DurableBackend(path, absent_lookup=absent_lookup)
    out = recover(state, msg, decision, first["idempotency_key"], restarted)
    assert out["result"] in ("delivered", "duplicate-ack")
    assert restarted.count() == 1
    assert run(state, msg, decision, restarted)["result"] == "duplicate-ack"


def _process_dispatch(home, backend_path, msg, decision, start, queue, fault=None, plain=False):
    ledger = DeliveryLedger(os.path.join(home, "ledger.sqlite"))
    backend = NonIdempotentBackend(backend_path) if plain else DurableBackend(backend_path, fault)
    if start:
        start.wait(10)
    out = dispatch(msg, decision, SessionCatalog(os.path.join(home, "routing_projection.json")), ledger, backend)
    if queue:
        queue.put(out["result"])
    ledger.close()


def test_process_crash_after_side_effect_before_ledger_commit(state, tmp_path):
    msg, decision = inputs(state)
    path = str(tmp_path / "backend.sqlite")
    DurableBackend(path)
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(target=_process_dispatch, args=(state["home"], path, msg, decision, None, None, "crash"))
    process.start()
    process.join(15)
    assert process.exitcode == 17
    key = idempotency_key(msg["canonical_hash"], decision["target_session_id"])
    state["ledger"].close()
    state["ledger"] = DeliveryLedger(os.path.join(state["home"], "ledger.sqlite"))
    assert state["ledger"].get(key)["delivery_state"] == "attempting"
    assert run(state, msg, decision, DurableBackend(path))["delivery_state"] == "unknown"
    assert recover(state, msg, decision, key, DurableBackend(path))["result"] == "duplicate-ack"
    assert DurableBackend(path).count() == 1


def test_identical_process_attempts_serialize(state, tmp_path):
    msg, decision = inputs(state)
    path = str(tmp_path / "backend.sqlite")
    NonIdempotentBackend(path)
    ctx = multiprocessing.get_context("spawn")
    start, queue = ctx.Event(), ctx.Queue()
    workers = [ctx.Process(target=_process_dispatch, args=(state["home"], path, msg, decision, start, queue, None, True)) for _ in range(2)]
    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        worker.join(15)
        assert worker.exitcode == 0
    assert sorted(queue.get(timeout=2) for _ in workers) == ["delivered", "duplicate-ack"]
    assert NonIdempotentBackend(path).count() == 1


def test_shared_connection_thread_attempts_serialize(state):
    msg, decision = inputs(state)
    with ThreadPoolExecutor(max_workers=4) as pool:
        outs = list(pool.map(lambda _: run(state, msg, decision), range(4)))
    assert [o["result"] for o in outs].count("delivered") == 1
    assert len(state["backend"].appended) == 1


def test_legacy_escalated_is_unknown_not_retryable(state):
    msg, decision = inputs(state)
    key = idempotency_key(msg["canonical_hash"], decision["target_session_id"])
    record = dict(idempotency_key=key, canonical_hash=msg["canonical_hash"],
                  message_id=msg["message_id"], target_session_id=decision["target_session_id"], result="escalated")
    # Actual pre-migration shape, bypass new record() defaults.
    state["ledger"]._conn.execute("INSERT INTO deliveries VALUES (?,?,?)", (key, json.dumps(record), "2026-01-01"))
    state["ledger"]._conn.commit()
    assert run(state, msg, decision)["delivery_state"] == "unknown"
    assert recover(state, msg, decision, key)["delivery_state"] == "unknown"
    assert not state["backend"].appended
    assert recover(state, msg, decision, key, resolution="rejected", evidence_id="audited-no-write")["result"] == "delivered"
    assert len(state["backend"].appended) == 1


def test_recovery_validates_original_key_hash_target_and_approval(state):
    msg, decision = inputs(state)
    state["backend"].fail_next = "reject"
    first = run(state, msg, decision)
    key = first["idempotency_key"]
    with pytest.raises(ValueError, match="original"):
        recover(state, dict(msg, canonical_hash="wrong"), decision, key)
    with pytest.raises(ValueError, match="original"):
        recover(state, msg, dict(decision, target_session_id="wrong"), key)
    with pytest.raises(ValueError, match="original"):
        recover(state, msg, decision, "../not-a-key")
    gated = recover(state, dict(msg, text="publish this"), decision, key)
    assert gated["result"] == "held-for-approval"
    assert state["ledger"].get(key)["delivery_state"] == "rejected"
    assert not state["backend"].appended


def test_mirror_failure_does_not_change_delivered_sqlite_state(state, tmp_path):
    msg, decision = inputs(state)
    state["ledger"].jsonl_path = str(tmp_path)  # opening a directory fails
    assert run(state, msg, decision)["result"] == "delivered"
    assert run(state, msg, decision)["result"] == "duplicate-ack"
    assert len(state["backend"].appended) == 1


def test_malformed_durable_evidence_fails_closed(state):
    msg, decision = inputs(state)
    class InvalidBackend:
        durable_delivery_keys = True
        def append_delivery(self, *args):
            return {"outcome": "delivered", "receipt_id": "not-the-contract"}
        def lookup_delivery(self, *args):
            return DeliveryEvidence("rejected")  # no proof
    assert run(state, msg, decision, InvalidBackend())["delivery_state"] == "unknown"


def test_redacted_recovery_preserves_canonical_identity_and_checks_payload(state):
    msg, decision = inputs(state, "harmless key sk-ABCDEFGHIJKLMNOPQRST")
    assert "sk-" not in msg["text"]
    state["backend"].fail_next = "reject"
    first = run(state, msg, decision)
    with pytest.raises(ValueError, match="original"):
        recover(state, dict(msg, text="different harmless payload"), decision, first["idempotency_key"])
    assert recover(state, msg, decision, first["idempotency_key"])["result"] == "delivered"


def test_ledger_path_aliases_share_the_delivery_lock(state, tmp_path):
    msg, decision = inputs(state)
    path = os.path.join(state["home"], "ledger.sqlite")
    alias = tmp_path / "alias.sqlite"
    alias.symlink_to(path)
    other = DeliveryLedger(str(alias))
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(dispatch, msg, decision, state["catalog"], led, state["backend"])
                       for led in (state["ledger"], other)]
            results = [f.result()["result"] for f in futures]
        assert sorted(results) == ["delivered", "duplicate-ack"]
        assert len(state["backend"].appended) == 1
    finally:
        other.close()


def test_recovery_rechecks_paused_topic_and_foreign_seat(state):
    msg, decision = inputs(state)
    state["backend"].fail_next = "reject"
    first = run(state, msg, decision)
    state["catalog"].set_paused("synthetic", True)
    assert recover(state, msg, decision, first["idempotency_key"])["result"] == "parked-clarification"
    state["catalog"].set_paused("synthetic", False)
    state["catalog"].steal_seat("synthetic", "other")
    assert recover(state, msg, decision, first["idempotency_key"])["result"] in ("parked-busy", "escalated")
    assert state["ledger"].get(first["idempotency_key"])["delivery_state"] == "rejected"
    assert not state["backend"].appended


def test_durable_rejection_evidence_then_safe_retry(state, tmp_path):
    msg, decision = inputs(state)
    class RejectedOnce(DurableBackend):
        rejected = False
        def append_delivery(self, target, message, key):
            if not self.rejected:
                self.rejected = True
                return DeliveryEvidence("rejected", evidence_id="authoritative-no-write")
            return super().append_delivery(target, message, key)
    backend = RejectedOnce(str(tmp_path / "durable.sqlite"))
    first = run(state, msg, decision, backend)
    assert first["delivery_state"] == "rejected"
    assert backend.count() == 0
    assert recover(state, msg, decision, first["idempotency_key"], backend)["result"] == "delivered"
    assert backend.count() == 1
