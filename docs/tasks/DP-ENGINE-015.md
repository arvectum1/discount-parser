# DP-ENGINE-015 — durable multi-record result/review persistence

## Status

Implemented on top of DP-ENGINE-014 in the canonical `arvectum1/discount-parser` product line.

## Goal

Make every automatically discovered record/offer independently durable and independently reviewable after process restart, without reacquiring the source URL and without collapsing sibling records back into the DP-008 single-result revision model.

## Contract

A multi-record source item is persisted as:

1. one immutable record-set manifest containing parent source identity/provenance, ordered `record_id` membership, and record-provider errors/warnings;
2. one durable result entity per `(job_id, item_id, record_id)` containing the record boundary, field decisions, candidates and evidence;
3. one optimistic revision per durable record;
4. one independent DP-009 lease key per durable record.

Sibling records from the same page therefore do not share a result revision or review lease.

## Persistence

`RecordResultRepository` reuses the proven DP-008 `ResultStore` backends (`InMemoryResultStore`, `JsonResultStore`, `SQLiteResultStore`) through a reserved reversible storage-item namespace. No migration of the existing result-store schema is required and ordinary single-result rows remain untouched.

`RecordResultCodec` is versioned by `MULTI_RECORD_RESULT_SCHEMA_VERSION = 1` and persists:

- `record_id`;
- boundary provider/source reference/order/confidence/evidence/metadata;
- boundary review status and reason;
- field decisions with existing candidate IDs and evidence;
- provider errors;
- optional raw content only when explicitly enabled, matching the DP-008 raw-content policy.

The default remains **raw HTML/text omitted**.

`persist_set()` is restart-safe for the normal crash window: an already-created exact manifest or record is idempotently reused, while a changed payload requires an explicit update/reset rather than blind overwrite.

## Record-scoped review

`DurableRecordReviewCoordinator` continues review from persisted candidates/evidence only. It never reacquires the URL.

Supported review operations:

- confirm/reject an engine-proposed record boundary;
- confirm/reject review-required fields using existing candidate IDs only.

There is no manual value insertion and no manual record creation path.

Each successful review update increments only that record's revision. A stale `expected_revision` fails with `ResultConflictError`.

## Governed queue

`GovernedRecordReviewQueue` reuses DP-009 queue backends (`InMemoryReviewQueueStore`, `JsonReviewQueueStore`, `SQLiteReviewQueueStore`) using the same reserved record identity as the durable result.

Consequences:

- two reviewers can claim two sibling records from one page concurrently;
- two reviewers cannot simultaneously claim the same record;
- leases survive JSON/SQLite restart;
- submission audit stores candidate IDs / record decision, never business values;
- a lease is released as completed when the reviewed record becomes `ready`, `incomplete`, or `rejected`;
- if more review remains inside the same record, its lease stays active.

## Status mapping

Record status is authoritative in the versioned multi-record payload:

- `ready` → underlying DP-008 `READY`;
- `needs_confirmation` → underlying `REVIEW_REQUIRED`;
- `incomplete` / `rejected` → underlying non-review `INCOMPLETE` storage bucket.

This mapping deliberately avoids changing the DP-008 schema/enum contract while preserving the richer record status on decode.

## Compatibility

DP-008/009 single-result APIs, stores, queue models and database tables are not changed.

DP-013 production source runtime also remains unchanged in this task. DP-015 establishes the durable/reviewer substrate needed before migrating source-specific `parse(html)` output to the generic DP-014 record extraction path.

## Non-goals

- changing the five production source adapters;
- DB migrations for legacy product tables;
- record deduplication across different source pages;
- supervisor UI or authentication/RBAC;
- manual record/value entry;
- overloading DP-007 page checkpoint fields with record-level semantics.

## Acceptance

Regression coverage verifies:

- round-trip record-set persistence;
- exact idempotence and changed-payload conflict;
- independent sibling revisions;
- stale revision rejection;
- durable boundary accept/reject;
- record-scoped pending review;
- JSON and SQLite result restart;
- reversible storage identity and single-result isolation;
- independent sibling leases;
- lease completion after terminal review;
- JSON queue restart;
- SQLite same-record exclusion with sibling concurrency;
- audit contains candidate IDs/record identity but not business values.
