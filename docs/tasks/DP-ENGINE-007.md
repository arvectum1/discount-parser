# DP-ENGINE-007 — Execution/job layer

**Status:** implemented

## Goal

Add a governed execution layer above `URLExtractionPipeline` so the Data Platform can process batches of URLs as resumable jobs without mixing worker concerns into acquisition, discovery, resolution or site-profile learning.

The layer is intentionally sequential and adapter-friendly. It establishes deterministic job/item state, retry semantics and checkpoints that a future Arvectum OS worker/scheduler can drive in bounded chunks.

## Public execution path

```python
from arvectum_data import ExtractionJob, FieldSpec, JobExecutor

job = ExtractionJob.from_urls(
    "catalog-refresh-2026-08-28",
    ["https://example.test/a", "https://example.test/b"],
    [FieldSpec("title"), FieldSpec("price", required=True)],
)

result = JobExecutor().run(job)
```

For bounded worker slices:

```python
result = executor.run(job, max_items=25)
# run the same job again later; checkpoint state is resumed
```

## Job and item contract

`ExtractionJob` contains:

- a stable `job_id`;
- ordered `JobItem` inputs;
- shared semantic `FieldSpec` definitions.

Each `JobItem` preserves the existing acquisition controls:

- URL;
- optional asset id;
- headers;
- timeout;
- max bytes;
- render mode.

If no `item_id` is supplied, a deterministic URL-derived id is generated. Duplicate URLs therefore need explicit distinct item ids when the caller intentionally wants the same URL twice in one job.

## Definition hash / resume safety

Every job exposes a deterministic `definition_hash` built from:

- item ids and URLs;
- acquisition controls;
- header values;
- field keys/required flags/confidence/margin/aliases.

The checkpoint stores only the resulting SHA-256 digest, not those inputs themselves.

If a checkpoint exists for a `job_id` but the current definition hash or item set differs, execution raises `JobCheckpointMismatchError` before processing anything. A stale checkpoint can therefore not silently resume a materially different job.

## Item statuses

Execution distinguishes five meaningful terminal outcomes:

- `succeeded` — governed extraction is ready;
- `review_required` — one or more fields require candidate confirmation;
- `incomplete` — required fields are unresolved/rejected;
- `failed` — execution raised an unrecoverable error or exhausted retries;
- plus `pending/running` for non-terminal checkpoint state.

`review_required` and `incomplete` are semantic outcomes, not transport failures. They are not retried automatically.

## Job status

`JobRunResult.status` is derived from item state:

- `succeeded` — all items succeeded;
- `needs_attention` — complete, but at least one item requires review or is incomplete;
- `completed_with_failures` — complete, with one or more failed items;
- `partial` — one or more items remain non-terminal (normally after `max_items`).

A failure in one item does not stop later items in the same run.

## Retry policy

Default `RetryPolicy`:

- max attempts: **3**;
- base delay: **0.5 s**;
- multiplier: **2.0**;
- max delay: **10 s**;
- retryable exceptions: `AcquisitionError`, `TimeoutError`, `OSError`.

Backoff is deterministic and capped. No random jitter is added in this baseline so tests and checkpoint semantics remain deterministic.

Unexpected programming/runtime exceptions such as a generic `RuntimeError` fail the item immediately unless the caller explicitly includes that exception type in a custom policy.

## Per-item isolation

Every item is executed through the existing `URLExtractionPipeline.extract()` contract.

The execution layer does not bypass or duplicate:

- static/rendered acquisition;
- candidate discovery;
- resolver confidence/margin policy;
- confirmation learning;
- profile lifecycle.

Provider failures that are already isolated inside `ExtractionResult` continue to use that lower-layer behavior.

## Checkpoint contract

`JobCheckpointStore` exposes only:

- `load(job_id)`;
- `save(checkpoint)`;
- `clear(job_id)`.

Two baseline stores are included:

- `InMemoryJobCheckpointStore` — default/test/runtime-local state;
- `JsonJobCheckpointStore(directory)` — atomic one-file-per-job local persistence.

The JSON store uses temporary-file + `os.replace` writes.

### What a checkpoint stores

A checkpoint stores execution control state only:

- job id;
- job definition hash;
- revision/timestamp;
- item id;
- item status;
- consumed attempt count;
- review-required field keys;
- unresolved required field keys;
- bounded error type/message/retryable flag.

It does **not** persist:

- source URLs;
- request headers;
- extracted business values;
- candidate evidence;
- full `URLExtractionResult` objects.

Persisted exception summaries redact HTTP(S) URL substrings before checkpoint serialization, so acquisition errors do not reintroduce URL/query payloads indirectly.

This keeps execution progress separate from future output/review persistence and avoids turning arbitrary extraction values into an accidental checkpoint schema.

## Resume semantics

Terminal checkpoint items are skipped on resume:

- succeeded items are not reacquired;
- review-required items are not automatically rerun while a human decision may already be pending;
- incomplete items are not pointlessly retried;
- failed items remain terminal after their retry budget is exhausted.

A resumed terminal `JobItemResult` has `resumed=True` and no live `URLExtractionResult`; durable output/review storage is intentionally a separate layer.

`resume=False` starts the same job definition from a fresh checkpoint. `clear_checkpoint(job_id)` also removes durable progress explicitly.

## Crash semantics

The executor writes `running` with the incremented attempt count **before** invoking the URL pipeline.

If the process dies during that attempt:

- resume consumes that attempt from the retry budget rather than risking infinite replay;
- if budget remains, the item is attempted again;
- if the interrupted attempt already consumed the last allowed slot, the item becomes failed without another call.

This is conservative at-least-once read execution with bounded replay. The current parser path is read-oriented; publication/write side effects remain outside this task.

## Bounded worker slices

`JobExecutor.run(..., max_items=N)` processes at most `N` non-terminal items in that invocation and checkpoints after each state transition.

This gives a future Arvectum OS scheduler a cooperative execution primitive without introducing threads, processes or a queue implementation into the engine package.

## Checkpoint failure behavior

Checkpoint persistence is authoritative execution-control state. Store failures are not swallowed.

If checkpoint state cannot be saved, the executor raises rather than continuing and pretending resumability was preserved. This is intentionally stricter than optional site-profile learning persistence.

## Human participation rule

The execution layer adds no customer configuration step.

A customer/reviewer is still only involved when the governed extraction layer returns candidate confirmation work. The customer does not:

- configure retries per site;
- manage checkpoints;
- choose worker slices;
- recover crashed items manually;
- inspect stack traces to continue a batch.

Those are infrastructure/runtime responsibilities.

## Explicit non-goals

`DP-ENGINE-007` does **not** implement:

- parallel/concurrent item execution;
- distributed queues;
- cross-node leases/consensus;
- scheduler registration in Arvectum OS;
- durable extraction-output storage;
- durable review/candidate evidence storage;
- automatic requeue of terminal failed jobs after policy exhaustion;
- publication/export sinks;
- login/session orchestration;
- CAPTCHA/anti-bot adaptation;
- OCR/VLM execution.

Those layers should consume the stable job/checkpoint contracts instead of modifying the extraction engine.

## Acceptance evidence

The targeted `DP-ENGINE-007` harness verifies:

- deterministic item ids and acquisition-control forwarding;
- duplicate derived item ids are rejected;
- definition hash changes with transport/semantic job changes;
- retryable acquisition failure retries then succeeds;
- exponential backoff and cap;
- unexpected runtime errors do not retry by default;
- retryable failures stop at the configured budget;
- item failure is isolated from later items;
- review-required and incomplete states are not execution failures/retries;
- chunked runs resume without reprocessing completed items;
- review-required checkpoints remain terminal on resume;
- definition mismatch blocks unsafe resume;
- `resume=False` restarts cleanly;
- interrupted running attempts resume with remaining budget;
- interrupted attempts at the retry limit fail without reexecution;
- JSON checkpoints survive reload while excluding URL/header payloads;
- clearing a checkpoint permits clean reexecution;
- invalid `max_items` is rejected;
- failed checkpoint error summaries redact HTTP(S) URLs.

Local targeted execution harness: **19 tests passed**.
