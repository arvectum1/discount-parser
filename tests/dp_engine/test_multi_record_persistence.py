from __future__ import annotations

from dataclasses import replace

import pytest

from arvectum_data import (
    Candidate,
    DurableRecordReviewCoordinator,
    Evidence,
    ExtractionResult,
    FieldDecision,
    FieldSpec,
    FieldStatus,
    GovernedRecordReviewQueue,
    InMemoryResultStore,
    InMemoryReviewQueueStore,
    JsonResultStore,
    JsonReviewQueueStore,
    RawAsset,
    RecordBoundary,
    RecordBoundaryStatus,
    RecordExtractionResult,
    RecordResultRepository,
    RecordSetResult,
    RecordStatus,
    ResultConflictError,
    SQLiteResultStore,
    SQLiteReviewQueueStore,
    StoredResultRecord,
    StoredResultStatus,
    parse_record_storage_item_id,
    payload_hash if False else record_storage_item_id,
)


def _record(record_id: str, value: str, *, boundary_review: bool = False, field_review: bool = True):
    field = FieldSpec("code", required=True, min_confidence=0.8, min_margin=0.1)
    asset = RawAsset(
        asset_id=f"asset-{record_id}",
        source_url="https://example.test/offers",
        html=f"<article>{value}</article>",
        metadata={"record": record_id},
    )
    candidate = Candidate(
        field_key="code",
        value=value,
        confidence=0.91,
        provider="fixture",
        evidence=(Evidence(kind="fixture", source_ref=record_id, excerpt=value),),
    )
    decision = FieldDecision(
        field=field,
        status=FieldStatus.NEEDS_CONFIRMATION if field_review else FieldStatus.AUTO_SELECTED,
        selected=candidate,
        candidates=(candidate,),
        reason="fixture",
    )
    boundary = RecordBoundary(
        record_id=record_id,
        asset=asset,
        provider="fixture_records",
        source_ref=f"records/{record_id}",
        ordinal=0,
        confidence=0.7 if boundary_review else 0.99,
        evidence=(Evidence(kind="record", source_ref=record_id),),
        metadata={"fixture": True},
    )
    return RecordExtractionResult(
        boundary=boundary,
        boundary_status=(
            RecordBoundaryStatus.NEEDS_CONFIRMATION
            if boundary_review
            else RecordBoundaryStatus.AUTO_SELECTED
        ),
        extraction=ExtractionResult(asset=asset, decisions={"code": decision}),
        boundary_reason="fixture boundary",
    )


def _set(*records):
    return RecordSetResult(
        asset=RawAsset(
            asset_id="parent",
            source_url="https://example.test/offers",
            html="<main>parent</main>",
        ),
        records=tuple(records),
        record_provider_warnings={"fixture_records": ("bounded",)},
    )


def test_persist_set_round_trips_records_without_raw_content():
    repo = RecordResultRepository(InMemoryResultStore())
    original = _set(_record("r1", "SAVE10"), _record("r2", "SAVE20"))
    manifest, stored = repo.persist_set(
        job_id="job", item_id="page", definition_hash="def", result=original
    )
    assert manifest.record_ids == ("r1", "r2")
    assert [item.revision for item in stored] == [1, 1]
    loaded = repo.load_set("job", "page")
    assert loaded is not None
    assert [item.record_id for item in loaded.records] == ["r1", "r2"]
    assert loaded.records[0].boundary.asset.html is None
    assert loaded.records[0].extraction.decisions["code"].selected.value == "SAVE10"


def test_persist_set_is_idempotent_for_exact_payload():
    repo = RecordResultRepository(InMemoryResultStore())
    result = _set(_record("r1", "SAVE10"))
    first = repo.persist_set(job_id="job", item_id="page", definition_hash="def", result=result)
    second = repo.persist_set(job_id="job", item_id="page", definition_hash="def", result=result)
    assert first[0].revision == second[0].revision == 1
    assert first[1][0].revision == second[1][0].revision == 1


def test_persist_set_rejects_changed_existing_record():
    repo = RecordResultRepository(InMemoryResultStore())
    repo.persist_set(job_id="job", item_id="page", definition_hash="def", result=_set(_record("r1", "A")))
    with pytest.raises(ResultConflictError):
        repo.persist_record(
            job_id="job", item_id="page", definition_hash="def", result=_record("r1", "B")
        )


def test_record_review_updates_only_one_sibling_revision():
    store = InMemoryResultStore()
    repo = RecordResultRepository(store)
    first = _record("r1", "A")
    second = _record("r2", "B")
    repo.persist_set(job_id="job", item_id="page", definition_hash="def", result=_set(first, second))
    review = DurableRecordReviewCoordinator(store, repository=repo)
    candidate_id = first.extraction.decisions["code"].selected.candidate_id
    update = review.confirm_fields(
        "job", "page", "r1", {"code": candidate_id}, expected_revision=1
    )
    assert update.record.revision == 2
    assert update.record.status is RecordStatus.READY
    sibling = repo.load_record("job", "page", "r2")
    assert sibling is not None and sibling.revision == 1
    assert sibling.status is RecordStatus.NEEDS_CONFIRMATION


def test_stale_record_revision_is_rejected():
    store = InMemoryResultStore()
    repo = RecordResultRepository(store)
    record = _record("r1", "A")
    repo.persist_set(job_id="job", item_id="page", definition_hash="def", result=_set(record))
    review = DurableRecordReviewCoordinator(store, repository=repo)
    candidate_id = record.extraction.decisions["code"].selected.candidate_id
    review.confirm_fields("job", "page", "r1", {"code": candidate_id}, expected_revision=1)
    with pytest.raises(ResultConflictError):
        review.confirm_fields("job", "page", "r1", {"code": candidate_id}, expected_revision=1)


def test_boundary_accept_then_field_review_can_continue():
    store = InMemoryResultStore()
    repo = RecordResultRepository(store)
    record = _record("r1", "A", boundary_review=True, field_review=True)
    repo.persist_set(job_id="job", item_id="page", definition_hash="def", result=_set(record))
    review = DurableRecordReviewCoordinator(store, repository=repo)
    accepted = review.confirm_boundary("job", "page", "r1", accept=True, expected_revision=1)
    assert accepted.record.revision == 2
    assert accepted.record.status is RecordStatus.NEEDS_CONFIRMATION
    candidate_id = accepted.result.extraction.decisions["code"].selected.candidate_id
    completed = review.confirm_fields(
        "job", "page", "r1", {"code": candidate_id}, expected_revision=2
    )
    assert completed.record.status is RecordStatus.READY
    assert completed.record.revision == 3


def test_boundary_reject_is_durable_and_values_are_hidden():
    store = InMemoryResultStore()
    repo = RecordResultRepository(store)
    record = _record("r1", "A", boundary_review=True, field_review=False)
    repo.persist_set(job_id="job", item_id="page", definition_hash="def", result=_set(record))
    update = DurableRecordReviewCoordinator(store, repository=repo).confirm_boundary(
        "job", "page", "r1", accept=False, expected_revision=1
    )
    assert update.record.status is RecordStatus.REJECTED
    assert update.result.values() == {}
    loaded = repo.load_result("job", "page", "r1")
    assert loaded is not None and loaded[1].status is RecordStatus.REJECTED


def test_pending_reviews_are_record_scoped():
    repo = RecordResultRepository(InMemoryResultStore())
    repo.persist_set(
        job_id="job",
        item_id="page",
        definition_hash="def",
        result=_set(_record("r1", "A"), _record("r2", "B", field_review=False)),
    )
    pending = repo.pending_reviews(job_id="job", item_id="page")
    assert [(item.item_id, item.record_id) for item in pending] == [("page", "r1")]


def test_json_result_store_survives_restart(tmp_path):
    path = tmp_path / "results"
    repo = RecordResultRepository(JsonResultStore(path))
    repo.persist_set(job_id="job", item_id="page", definition_hash="def", result=_set(_record("r1", "A")))
    reopened = RecordResultRepository(JsonResultStore(path))
    loaded = reopened.load_result("job", "page", "r1")
    assert loaded is not None
    assert loaded[0].revision == 1
    assert loaded[1].extraction.decisions["code"].selected.value == "A"


def test_sqlite_result_store_survives_restart(tmp_path):
    path = tmp_path / "results.sqlite3"
    with SQLiteResultStore(path) as store:
        RecordResultRepository(store).persist_set(
            job_id="job", item_id="page", definition_hash="def", result=_set(_record("r1", "A"))
        )
    with SQLiteResultStore(path) as store:
        loaded = RecordResultRepository(store).load_result("job", "page", "r1")
        assert loaded is not None and loaded[0].revision == 1


def test_record_storage_identity_is_reversible():
    encoded = record_storage_item_id("page/сложный", "rec:1")
    assert parse_record_storage_item_id(encoded) == ("page/сложный", "rec:1")


def test_clear_item_does_not_delete_single_result_row():
    store = InMemoryResultStore()
    repo = RecordResultRepository(store)
    repo.persist_set(job_id="job", item_id="page", definition_hash="def", result=_set(_record("r1", "A")))
    payload = {"legacy": True}
    from arvectum_data.results.models import payload_hash
    legacy = store.create(
        StoredResultRecord(
            job_id="job", item_id="page", definition_hash="def",
            status=StoredResultStatus.READY, payload=payload, payload_hash=payload_hash(payload),
        )
    )
    repo.clear_item("job", "page")
    assert store.load("job", "page") == legacy
    assert repo.load_manifest("job", "page") is None


def test_governed_queue_allows_two_sibling_records_to_be_claimed_independently():
    result_store = InMemoryResultStore()
    repo = RecordResultRepository(result_store)
    repo.persist_set(
        job_id="job", item_id="page", definition_hash="def",
        result=_set(_record("r1", "A"), _record("r2", "B")),
    )
    queue = GovernedRecordReviewQueue(
        result_store, queue_store=InMemoryReviewQueueStore(), repository=repo,
        token_factory=iter(("token-1", "token-2")).__next__,
        event_id_factory=iter((f"event-{i}" for i in range(20))).__next__,
    )
    one = queue.claim("job", "page", "r1", "alice", expected_result_revision=1)
    two = queue.claim("job", "page", "r2", "bob", expected_result_revision=1)
    assert one.lease.reviewer_id == "alice"
    assert two.lease.reviewer_id == "bob"
    assert one.lease.item_id != two.lease.item_id


def test_governed_queue_submission_releases_only_completed_record():
    result_store = InMemoryResultStore()
    repo = RecordResultRepository(result_store)
    r1, r2 = _record("r1", "A"), _record("r2", "B")
    repo.persist_set(job_id="job", item_id="page", definition_hash="def", result=_set(r1, r2))
    queue = GovernedRecordReviewQueue(
        result_store, queue_store=InMemoryReviewQueueStore(), repository=repo,
        token_factory=lambda: "token",
    )
    claim = queue.claim("job", "page", "r1", "alice", expected_result_revision=1)
    candidate_id = r1.extraction.decisions["code"].selected.candidate_id
    submitted = queue.submit_fields(
        "job", "page", "r1", "alice", claim.lease.lease_token,
        {"code": candidate_id}, expected_result_revision=1,
    )
    assert submitted.lease_released is True
    assert submitted.record.status is RecordStatus.READY
    assert [(item.record_id) for item in queue.pending(job_id="job")] == ["r2"]


def test_json_queue_lease_survives_restart(tmp_path):
    result_store = InMemoryResultStore()
    repo = RecordResultRepository(result_store)
    repo.persist_set(job_id="job", item_id="page", definition_hash="def", result=_set(_record("r1", "A")))
    queue_path = tmp_path / "queue.json"
    queue = GovernedRecordReviewQueue(
        result_store, queue_store=JsonReviewQueueStore(queue_path), repository=repo,
        token_factory=lambda: "token", event_id_factory=lambda: "claim-event",
    )
    claim = queue.claim("job", "page", "r1", "alice")
    reopened = GovernedRecordReviewQueue(
        result_store, queue_store=JsonReviewQueueStore(queue_path), repository=repo,
    )
    lease, stored, _ = reopened.get_claim(
        "job", "page", "r1", "alice", claim.lease.lease_token
    )
    assert lease.reviewer_id == "alice"
    assert stored.record_id == "r1"


def test_sqlite_queue_coordinates_sibling_and_same_record_claims(tmp_path):
    result_store = InMemoryResultStore()
    repo = RecordResultRepository(result_store)
    repo.persist_set(
        job_id="job", item_id="page", definition_hash="def",
        result=_set(_record("r1", "A"), _record("r2", "B")),
    )
    path = tmp_path / "queue.sqlite3"
    with SQLiteReviewQueueStore(path) as store_a, SQLiteReviewQueueStore(path) as store_b:
        q1 = GovernedRecordReviewQueue(result_store, queue_store=store_a, repository=repo)
        q2 = GovernedRecordReviewQueue(result_store, queue_store=store_b, repository=repo)
        q1.claim("job", "page", "r1", "alice")
        q2.claim("job", "page", "r2", "bob")
        with pytest.raises(Exception):
            q2.claim("job", "page", "r1", "bob")


def test_audit_submission_contains_record_identity_not_business_value():
    result_store = InMemoryResultStore()
    repo = RecordResultRepository(result_store)
    record = _record("r1", "SECRET-CODE")
    repo.persist_set(job_id="job", item_id="page", definition_hash="def", result=_set(record))
    queue = GovernedRecordReviewQueue(result_store, repository=repo, token_factory=lambda: "token")
    claim = queue.claim("job", "page", "r1", "alice")
    candidate_id = record.extraction.decisions["code"].selected.candidate_id
    queue.submit_fields(
        "job", "page", "r1", "alice", claim.lease.lease_token,
        {"code": candidate_id}, expected_result_revision=1,
    )
    submitted = [event for event in queue.history(job_id="job", item_id="page", record_id="r1") if event.action.value == "submitted"]
    assert len(submitted) == 1
    assert submitted[0].metadata["record_id"] == "r1"
    assert submitted[0].selections == {"code": candidate_id}
    assert "SECRET-CODE" not in repr(submitted[0].to_dict())
