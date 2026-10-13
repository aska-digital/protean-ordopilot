"""2. new-topic: novel message -> NEW with fresh session_id, min context."""
from tests.conftest import redacted_msg, seed, sessions_of

import pytest

from courier import router
from courier.catalog import SessionCatalog
from courier.cli import State


class TestNewTopic:
    def test_novel_message_is_new(self, state):
        seed(state["catalog"], state["backend"], [
            ("refund-policy", "refund policy returns warranty claims process"),
            ("billing", "billing invoices payments charges statements"),
        ])
        msg = redacted_msg("quantum knitting patterns for llamas under "
                           "moonlight, needles clicking")
        decision = router.route(msg, sessions_of(state["catalog"]))
        assert decision["kind"] == "NEW"
        assert decision["target_session_id"]
        assert decision["required_context"] == []

    def test_new_materializes_session(self, state):
        from courier import dispatcher
        seed(state["catalog"], state["backend"], [
            ("refund-policy", "refund policy returns warranty claims process"),
        ])
        msg = redacted_msg("quantum knitting patterns for llamas under "
                           "moonlight, needles clicking")
        decision = router.route(msg, sessions_of(state["catalog"]))
        assert decision["kind"] == "NEW"
        result = dispatcher.dispatch(msg, decision, state["catalog"],
                                     state["ledger"], state["backend"])
        assert result["result"] == "delivered"
        assert state["catalog"].get(result["target_session_id"]) is not None

    @pytest.mark.parametrize("kind", ["CONTINUE", "DIRECT", "HANDOFF", "NEW"])
    def test_new_session_can_receive_later_message(self, state, kind):
        from courier import dispatcher
        first = redacted_msg("novel synthetic topic", message_id="new-first")
        decision = {"kind": "NEW", "target_session_id": "routing-id",
                    "target_slug": "novel-topic"}
        result = dispatcher.dispatch(first, decision, state["catalog"],
                                     state["ledger"], state["backend"])
        assert result["result"] == "delivered"
        backend_ref = state["backend"].get_session("novel-topic")
        assert backend_ref["session_id"] != "routing-id"

        # Reopening the projection must preserve the delivery binding.
        catalog = SessionCatalog(state["catalog"].path)
        followup = redacted_msg("a different synthetic followup",
                                message_id="new-followup")
        continued = dispatcher.dispatch(
            followup, {"kind": kind, "target_session_id": "routing-id"},
            catalog, state["ledger"], state["backend"])
        assert continued["result"] == "delivered"
        assert continued["target_session_id"] == "routing-id"
        assert state["backend"].appended == [
            (backend_ref["session_id"], "new-first"),
            (backend_ref["session_id"], "new-followup")]
        assert state["ledger"].count() == 2

        duplicate = dispatcher.dispatch(
            followup, {"kind": kind, "target_session_id": "routing-id"},
            catalog, state["ledger"], state["backend"])
        assert duplicate["result"] == "duplicate-ack"
        assert len(state["backend"].appended) == 2

    def test_cli_reconstructed_backend_preserves_binding(self, tmp_path):
        from courier import dispatcher
        state = State(str(tmp_path / "cli"))
        ledger = state.ledger()
        try:
            initial = dispatcher.dispatch(
                redacted_msg("synthetic fresh topic", message_id="cli-first"),
                {"kind": "NEW", "target_session_id": "routing-cli",
                 "target_slug": "fresh"},
                state.catalog(), ledger, state.backend())
            assert initial["result"] == "delivered"

            backend = State(state.home).backend()
            followup = dispatcher.dispatch(
                redacted_msg("synthetic followup", message_id="cli-followup"),
                {"kind": "CONTINUE", "target_session_id": "routing-cli"},
                state.catalog(), ledger, backend)
            assert followup["result"] == "delivered"
            assert len(backend.appended) == 1
        finally:
            ledger.close()
