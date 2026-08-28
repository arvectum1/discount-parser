# DP-ENGINE-008 — Durable result/review persistence

**Status:** implemented

## Goal

Persist governed extraction results separately from `DP-ENGINE-007` execution checkpoints so `succeeded`, `review_required` and `incomplete` item evidence survives process restarts and review can continue without reacquiring or reparsing the page.

The split is deliberate:

- **checkpoint store** — execution control state only;
- **result store** — durable extraction/review data and evidence.

## Durable record

`StoredResultRecord` is keyed by `job_id` + `item_id` and bound to the job `definition_hash`.

It stores:

- status: `ready`, `review_required` or `incomplete`;
- schema version (`RESULT_SCHEMA_VERSION = 1`);
- optimistic `revision`;
- `created_at` / `updated_at`;
- encoded `URLExtractionResult` payload;
- SHA-256 of that payload.

The payload hash is recomputed during load. Corrupted/tampered data raises `ResultIntegrityError`.

## Persisted evidence

The default payload contains everything required to continue governed review:

- asset id/source URL and asset metadata;
- acquisition attempts/warnings;
- field specs/statuses/reasons;
- candidates, candidate ids and values;
- confidence/provider;
- evidence kind/source reference/excerpt/metadata;
- provider errors;
- learning events/warnings.

The acquisition and extraction objects are reconstructed onto the same `RawAsset` instance.

### Raw page content

`ResultCodec()` defaults to `include_raw_content=False`, so it does **not** persist raw text, HTML or `RawAsset.attributes`.

Those are not required for `ExtractionEngine.confirm()` or confirmation learning. A deployment that explicitly requires a source snapshot may use:

```python
ResultCodec(include_raw_content=True)
```

That policy is persisted in the record and preserved by subsequent review updates, even if the review coordinator itself was constructed with the default codec.

## Typed value codec

Candidate values/metadata are not silently stringified. The baseline codec preserves:

- `None`;
- `bool`;
- `int`;
- finite `float`;
- `str`;
- `bytes`;
- `list`;
- `tuple`;
- mappings with string keys.

Unsupported arbitrary objects raise `ResultSerializationError`.

## Result stores

`ResultStore` defines:

- `load(job_id, item_id)`;
- `create(record)`;
- optimistic `update(record, expected_revision=...)`;
- filtered `list(...)`;
- `delete(job_id, item_id)`;
- `clear_job(job_id)`.

Implementations:

### `InMemoryResultStore`

Process-local/test backend.

### `JsonResultStore`

- atomic temp-file + `os.replace` writes;
- hashed job/item path names;
- one JSON record per item;
- intended for a simple local single-writer deployment.

### `SQLiteResultStore`

- Python stdlib `sqlite3` only;
- WAL mode;
- configurable busy timeout;
- `BEGIN IMMEDIATE` writes;
- optimistic revision checks in the transaction;
- shared state for multiple processes on one runtime node;
- status/job index for pending-review lookup.

## Optimistic review revisions

Created results start at revision `1`; each successful update increments the revision.

`DurableReviewCoordinator.confirm(... expected_revision=N)` rejects stale reviewer submissions through `ResultConflictError` instead of allowing last-write-wins.

## `JobExecutor` integration

`JobExecutor` accepts optional:

```python
JobExecutor(
    checkpoint_store=...,
    result_store=...,
    result_codec=...,
)
```

For a semantic extraction result, write order is:

1. extraction completes;
2. durable result is persisted;
3. terminal checkpoint state is persisted.

The result is intentionally durable **before** the checkpoint says terminal.

### Crash-window recovery

If the process dies after step 2 but before step 3, the checkpoint remains `running` while the durable result exists.

On resume the executor:

1. loads the matching durable result;
2. reconstructs `URLExtractionResult`;
3. derives/repairs the terminal checkpoint state;
4. returns `resumed=True`;
5. does **not** fetch the URL again.

## Rehydrated terminal items

When a result store is configured, normal resume also reloads persisted evidence into `JobItemResult.result` for terminal items.

Without a result store, `DP-ENGINE-007` behavior is unchanged.

`run(job, resume=False)` clears prior result records for that `job_id` before a fresh run. `clear_results(job_id)` is also available independently from `clear_checkpoint(job_id)`.

## Durable review continuation

`DurableReviewCoordinator` is the post-restart review path:

```python
coordinator = DurableReviewCoordinator(
    result_store,
    pipeline=pipeline,
    checkpoint_store=checkpoint_store,
)

record, result = coordinator.get(job_id, item_id)
candidate_id = result.extraction.decisions["price"].candidates[0].candidate_id

update = coordinator.confirm(
    job_id,
    item_id,
    {"price": candidate_id},
    expected_revision=record.revision,
)
```

Confirmation delegates to the existing `URLExtractionPipeline.confirm()` contract. Therefore the reviewer may only:

- select an existing candidate id; or
- reject a review-required field with `None`.

There is still no manual replacement-value API and no acquisition call during durable review.

## Confirmation learning after restart

Because review uses the normal pipeline `confirm()`, `DP-ENGINE-005` structural learning remains active.

Production review workers should use the same persistent site-profile backend as extraction workers when learning must survive process boundaries.

## Checkpoint synchronization

When a checkpoint store is supplied, the coordinator reconciles the reviewed item:

- durable `ready` -> checkpoint `succeeded`;
- remaining review work -> `review_required`;
- rejected/unresolved required result -> `incomplete`.

Attempt count is preserved.

`reconcile_checkpoint(job_id, item_id)` can repeat synchronization explicitly.

Result update and checkpoint update are separate persistence operations, not a distributed transaction. The reviewed result is written first. If checkpoint update fails, the review decision remains durable and reconciliation can repair control state later.

## Sensitive-data boundary

Unlike the minimal `DP-ENGINE-007` checkpoint, the result store intentionally contains source identity, candidate values and evidence. It is therefore a **protected data/evidence store**.

Raw page text/HTML/attributes remain opt-in, but result-store access, encryption-at-rest and retention policy belong to deployment/security layers rather than the checkpoint contract.

## Failure semantics

- serialization failure prevents terminal result persistence;
- result-store failures are authoritative and are not swallowed;
- same definition + identical payload is idempotent;
- a different existing payload is not silently overwritten;
- job-definition mismatch raises `ResultDefinitionMismatchError`;
- stale revision raises `ResultConflictError`;
- payload corruption raises `ResultIntegrityError`.

## Human participation rule

No new customer configuration is introduced. The reviewer only checks automatically proposed candidates and confirms/rejects them.

The reviewer does not reacquire the page, inspect CSS/XPath/DOM, reconstruct batches, re-enter values, or manage persistence files.

## Explicit non-goals

`DP-ENGINE-008` does **not** implement:

- reviewer identity/RBAC;
- encryption-at-rest/key management;
- review web UI;
- distributed/network database deployment;
- cross-node transactions;
- blob/object storage for large source archives;
- review claim/lease queues;
- SLA/escalation;
- publication/export sinks;
- manual value correction;
- CSS/XPath learning.

## Acceptance evidence

The targeted regressions cover:

- candidate/evidence/value round-trip;
- bytes/tuple metadata preservation;
- minimal raw-content mode;
- explicit full-snapshot mode;
- preservation of full-snapshot policy through later review;
- unsupported value rejection;
- payload tamper detection;
- idempotent initial persistence and conflict protection;
- JSON reload/pending-review listing;
- shared SQLite state and revision conflict;
- post-restart confirmation without reacquisition;
- checkpoint synchronization after confirmation;
- definition-hash protection;
- terminal result rehydration on executor resume;
- crash-window recovery without refetch;
- clean `resume=False` result reset;
- stale review revision rejection.

Local targeted persistence/review harness: **15 tests passed**.
