"""Merge destinations must retain the result without overwriting a third topic."""
from pathlib import Path

import pytest

from courier.catalog import ConflictError, SeatError


@pytest.mark.parametrize("destination", ["source-a", "source-b", "merged"])
def test_merge_retains_result_for_source_or_new_destination(state, destination):
    catalog = state["catalog"]
    catalog.create_session("source-a", "Summary A")
    catalog.create_session("source-b", "Summary B")
    unrelated = catalog.create_session("unrelated", "Keep this record")

    merged = catalog.merge("source-a", "source-b", destination)

    assert catalog.get(destination) == merged
    assert catalog.get(merged["session_id"]) == merged
    assert merged["summary"] == "Summary A | Summary B"
    assert merged["merged_from"] == ["source-a", "source-b"]
    assert catalog.get("unrelated") == unrelated
    assert {s["slug"] for s in catalog.list_sessions()} == {
        destination, "unrelated"}
    event = catalog.events()[-1]
    assert event["type"] == "sessions-merged"
    assert event["session_id"] == merged["session_id"]


def test_merge_rejects_unrelated_existing_destination_without_mutation(state):
    catalog = state["catalog"]
    for slug in ("source-a", "source-b", "unrelated"):
        catalog.create_session(slug, "Summary " + slug)
    before = Path(catalog.path).read_bytes()

    with pytest.raises(ConflictError, match="target slug exists"):
        catalog.merge("source-a", "source-b", "unrelated")

    assert Path(catalog.path).read_bytes() == before


def test_merge_rejects_foreign_seat_without_mutation(state):
    catalog = state["catalog"]
    for slug in ("source-a", "source-b"):
        catalog.create_session(slug)
        catalog.acquire_seat(slug, "other")
    before = Path(catalog.path).read_bytes()

    with pytest.raises(SeatError):
        catalog.merge("source-a", "source-b", "source-a", holder="courier")

    assert Path(catalog.path).read_bytes() == before
