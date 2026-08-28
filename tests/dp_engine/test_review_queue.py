from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from arvectum_data import (
    AcquisitionResult,
    Candidate,
    Evidence,
    ExtractionResult,
    FieldDecision,
    FieldSpec,
    FieldStatus,
    GovernedReviewQueue,
    InMemoryResultStore,
    InMemoryReviewQueueStore,
    JsonReviewQueueStore,
    RawAsset,
    ResultConflictError,
    ResultRepository,
    ReviewAction,
    ReviewLeaseConflictError,
    ReviewLeaseExpiredError,
    ReviewerIdentity,
    ReviewerMismatchError,
    SQLiteReviewQueueStore,
    StoredResultStatus,
    URLExtractionResult,
)


def make_review_result(*, two_fields: bool = False) -> URLExtractionResult:
    asset = RawAsset("asset", source_url="https://shop.example/item")
    price = FieldSpec("price", required=True)
    p1 = Candidate("price", "199", 0.90, "test", (Evidence("meta", "price"),))
    p2 = Candidate("price", "201", 0.89, "test", (Evidence("jsonld", "price"),))
    decisions = {
        "price": FieldDecision(price, FieldStatus.NEEDS_CONFIRMATION, p1, (p1, p2), "ambiguous")
    }
    if two_fields:
        title = FieldSpec("title")
        t1 = Candidate("title", "A", 0.90, "test", (Evidence("meta", "title"),))
        t2 = Candidate("title", "B", 0.89, "test", (Evidence("jsonld", "title"),))
        decisions["title"] = FieldDecision(title, FieldStatus.NEEDS_CONFIRMATION, t1, (t1, t2), "ambiguous")
    extraction = ExtractionResult(asset, decisions)
    return URLExtractionResult(AcquisitionResult(asset, ()), extraction)


def queue_fixture(*, two_fields: bool = False):
    now = [100.0]
    store = InMemoryResultStore()
    repo = ResultRepository(store, clock=lambda: now[0])
    record = repo.persist_initial(
        job_id="job",
        item_id="item",
        definition_hash="definition",
        result=make_review_result(two_fields=two_fields),
    )
    tokens = iter(["lease-1", "lease-2", "lease-3", "lease-4"])
    events = iter([f"event-{i}" for i in range(1, 40)])
    queue = GovernedReviewQueue(
        store,
        queue_store=InMemoryReviewQueueStore(),
        clock=lambda: now[0],
        token_factory=lambda: next(tokens),
        event_id_factory=lambda: next(events),
        default_lease_s=10,
        max_lease_s=100,
    )
    return now, store, record, queue


def test_reviewer_identity_rejects_blank_id():
    with pytest.raises(ValueError, match="reviewer_id"):
        ReviewerIdentity("  ")


def test_pending_lists_only_review_required_records():
    now, store, record, queue = queue_fixture()
    ready = make_review_result()
    decision = ready.extraction.decisions["price"]
    ready = URLExtractionResult(
        ready.acquisition,
        ExtractionResult(
            ready.asset,
            {"price": replace(decision, status=FieldStatus.AUTO_SELECTED)},
        ),
    )
    ResultRepository(store, clock=lambda: now[0]).persist_initial(
        job_id="job", item_id="ready", definition_hash="definition", result=ready
    )

    assert [item.item_id for item in queue.pending()] == ["item"]


def test_claim_hides_item_and_same_reviewer_claim_is_idempotent():
    _, _, record, queue = queue_fixture()
    first = queue.claim("job", "item", "alice", expected_result_revision=record.revision)
    second = queue.claim("job", "item", "alice", expected_result_revision=record.revision)

    assert queue.pending() == ()
    assert first.lease.lease_token == second.lease.lease_token
    assert [event.action for event in queue.history()] == [ReviewAction.CLAIMED]


def test_second_reviewer_cannot_claim_active_lease():
    _, _, record, queue = queue_fixture()
    queue.claim("job", "item", "alice", expected_result_revision=record.revision)

    with pytest.raises(ReviewLeaseConflictError):
        queue.claim("job", "item", "bob")


def test_claim_next_skips_item_claimed_by_someone_else():
    now = [100.0]
    store = InMemoryResultStore()
    repo = ResultRepository(store, clock=lambda: now[0])
    for item_id in ("a", "b"):
        repo.persist_initial(job_id="job", item_id=item_id, definition_hash="d", result=make_review_result())
    events = iter([f"e{i}" for i in range(20)])
    tokens = iter([f"t{i}" for i in range(20)])
    queue = GovernedReviewQueue(
        store,
        queue_store=InMemoryReviewQueueStore(),
        clock=lambda: now[0],
        token_factory=lambda: next(tokens),
        event_id_factory=lambda: next(events),
        default_lease_s=10,
        max_lease_s=100,
    )
    queue.claim("job", "a", "alice")

    claim = queue.claim_next("bob")
    assert claim is not None
    assert claim.item.item_id == "b"


def test_expired_lease_can_be_taken_over():
    now, _, record, queue = queue_fixture()
    queue.claim("job", "item", "alice", expected_result_revision=record.revision)
    now[0] = 111.0

    claim = queue.claim("job", "item", "bob", expected_result_revision=record.revision)

    assert claim.lease.reviewer_id == "bob"
    assert queue.history()[-1].action is ReviewAction.TAKEN_OVER
    assert queue.history()[-1].metadata["previous_reviewer_id"] == "alice"


def test_renew_requires_owner_and_extends_lease():
    now, _, record, queue = queue_fixture()
    claim = queue.claim("job", "item", "alice", expected_result_revision=record.revision)
    now[0] = 105.0

    with pytest.raises(ReviewerMismatchError):
        queue.renew("job", "item", "bob", claim.lease.lease_token)

    renewed = queue.renew("job", "item", "alice", claim.lease.lease_token, lease_s=20)
    assert renewed.expires_at == 125.0
    assert renewed.revision == claim.lease.revision + 1


def test_release_returns_item_to_available_queue():
    _, _, record, queue = queue_fixture()
    claim = queue.claim("job", "item", "alice", expected_result_revision=record.revision)
    queue.release("job", "item", "alice", claim.lease.lease_token)

    assert queue.pending()[0].available
    assert [event.action for event in queue.history()] == [ReviewAction.CLAIMED, ReviewAction.RELEASED]


def test_submit_confirms_candidate_releases_lease_and_audits_candidate_id_only():
    _, _, record, queue = queue_fixture()
    claim = queue.claim("job", "item", "alice", expected_result_revision=record.revision)
    _, loaded_record, result = queue.get_claim("job", "item", "alice", claim.lease.lease_token)
    candidate = result.extraction.decisions["price"].candidates[0]

    submitted = queue.submit(
        "job",
        "item",
        "alice",
        claim.lease.lease_token,
        {"price": candidate.candidate_id},
        expected_result_revision=loaded_record.revision,
    )

    assert submitted.record.status is StoredResultStatus.READY
    assert submitted.lease_released
    actions = [event.action for event in queue.history()]
    assert actions == [ReviewAction.CLAIMED, ReviewAction.SUBMITTED, ReviewAction.COMPLETED]
    serialized = json.dumps(queue.history()[1].to_dict(), ensure_ascii=False)
    assert candidate.candidate_id in serialized
    assert '"199"' not in serialized


def test_reject_required_field_completes_as_incomplete():
    _, _, record, queue = queue_fixture()
    claim = queue.claim("job", "item", "alice", expected_result_revision=record.revision)

    submitted = queue.reject_fields(
        "job",
        "item",
        "alice",
        claim.lease.lease_token,
        ["price"],
        expected_result_revision=record.revision,
    )

    assert submitted.record.status is StoredResultStatus.INCOMPLETE
    assert submitted.lease_released


def test_partial_submit_keeps_lease_for_remaining_review_fields():
    _, _, record, queue = queue_fixture(two_fields=True)
    claim = queue.claim("job", "item", "alice", expected_result_revision=record.revision)
    _, loaded, result = queue.get_claim("job", "item", "alice", claim.lease.lease_token)
    candidate = result.extraction.decisions["price"].candidates[0]

    submitted = queue.submit(
        "job", "item", "alice", claim.lease.lease_token,
        {"price": candidate.candidate_id},
        expected_result_revision=loaded.revision,
    )

    assert submitted.record.status is StoredResultStatus.REVIEW_REQUIRED
    assert not submitted.lease_released
    assert queue.queue_store.load_lease("job", "item") is not None


def test_expired_lease_cannot_submit():
    now, _, record, queue = queue_fixture()
    claim = queue.claim("job", "item", "alice", expected_result_revision=record.revision)
    now[0] = 111.0

    with pytest.raises(ReviewLeaseExpiredError):
        queue.submit(
            "job", "item", "alice", claim.lease.lease_token,
            {"price": "candidate"}, expected_result_revision=record.revision,
        )


def test_stale_result_revision_cannot_submit_even_with_valid_lease():
    _, _, record, queue = queue_fixture()
    claim = queue.claim("job", "item", "alice", expected_result_revision=record.revision)

    with pytest.raises(ResultConflictError):
        queue.submit(
            "job", "item", "alice", claim.lease.lease_token,
            {"price": "candidate"}, expected_result_revision=record.revision + 1,
        )


def test_lease_duration_is_bounded():
    _, _, record, queue = queue_fixture()
    with pytest.raises(ValueError, match="max_lease_s"):
        queue.claim("job", "item", "alice", expected_result_revision=record.revision, lease_s=101)


def test_json_queue_store_survives_reload(tmp_path: Path):
    path = tmp_path / "review-queue.json"
    first = JsonReviewQueueStore(path)
    first.claim("job", "item", "alice", "token", now=1, ttl_s=10, event_id="e1")

    second = JsonReviewQueueStore(path)
    assert second.load_lease("job", "item").reviewer_id == "alice"
    assert second.list_events()[0].action is ReviewAction.CLAIMED


def test_sqlite_queue_store_prevents_double_claim_across_connections(tmp_path: Path):
    path = tmp_path / "review.db"
    first = SQLiteReviewQueueStore(path)
    second = SQLiteReviewQueueStore(path)
    try:
        first.claim("job", "item", "alice", "a", now=1, ttl_s=10, event_id="e1")
        with pytest.raises(ReviewLeaseConflictError):
            second.claim("job", "item", "bob", "b", now=2, ttl_s=10, event_id="e2")

        lease, event = second.claim("job", "item", "bob", "b", now=12, ttl_s=10, event_id="e3")
        assert lease.reviewer_id == "bob"
        assert event.action is ReviewAction.TAKEN_OVER
        assert len(first.list_events()) == 2
    finally:
        first.close()
        second.close()
