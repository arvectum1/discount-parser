# DP-ENGINE-009 — Governed review queue / reviewer workflow

**Status:** implemented

## Goal

Add a governed multi-reviewer workflow above `DP-ENGINE-008` durable review results so pending confirmation work can be claimed, renewed, completed and audited without two reviewers racing on the same item.

The queue does not copy extraction payloads. `StoredResultRecord` remains the source of truth for candidate/evidence data; the queue stores only reviewer coordination state and an append-only action audit.

## Core boundary

The layers now have distinct responsibilities:

- **execution checkpoint** — worker progress/retry control;
- **durable result store** — source identity, candidate values and extraction/review evidence;
- **review queue store** — reviewer lease coordination + action audit.

The queue never becomes a second result database.

## Reviewer identity

`ReviewerIdentity` carries:

- stable `reviewer_id`;
- optional display name;
- optional caller-supplied metadata.

It represents a principal already authenticated by the surrounding application/runtime.

Authentication, password handling, SSO, directory lookup and organization-wide RBAC are intentionally not implemented inside the parser engine. A production UI/API must derive `ReviewerIdentity` from its authenticated session rather than trusting arbitrary end-user text.

## Pending queue

`GovernedReviewQueue.pending()` derives work directly from durable results whose status is `review_required`.

Each `ReviewQueueItem` contains only:

- job id;
- item id;
- durable result revision;
- definition hash;
- current lease state when present;
- availability flag.

Ready/incomplete results are not queue work.

An active claim is hidden from the default pending view. `include_claimed=True` is available for administrative/runtime inspection.

## Claim / lease model

Review work is protected through an expiring `ReviewLease`:

- `reviewer_id`;
- opaque `lease_token`;
- `claimed_at`;
- `expires_at`;
- `updated_at`;
- monotonic lease revision.

Default lease duration: **15 minutes**.

Default maximum requested lease: **24 hours**.

The lease token is generated with `secrets.token_urlsafe(24)` in the default workflow and acts as a bearer capability. It must be treated as a protected runtime credential and must not be logged or exposed to unrelated clients.

## Claim semantics

`claim(job_id, item_id, reviewer, ...)`:

1. verifies the durable result still requires review;
2. optionally verifies the expected result revision;
3. atomically attempts to acquire the queue lease;
4. returns the lease to the reviewer.

While an unexpired lease is active:

- another reviewer gets `ReviewLeaseConflictError`;
- the same reviewer can repeat claim idempotently and receives the existing lease rather than a second active lease.

After lease expiry another reviewer can take over the item. The takeover increments lease revision and writes a `taken_over` audit event carrying only the previous reviewer id as coordination metadata.

## `claim_next()`

`claim_next(reviewer)` provides a worker/UI-friendly dequeue primitive.

It scans current review-required results and:

- skips items actively owned by another reviewer;
- tolerates claim races by continuing to the next candidate;
- can return the reviewer's already-active claim idempotently;
- returns `None` when no claimable work remains.

Queue ordering follows the deterministic ordering exposed by the durable result store.

## Renew / release

`renew()` requires:

- matching reviewer id;
- matching lease token;
- an unexpired lease.

It extends expiry and increments lease revision.

`release()` voluntarily gives the item back to the available queue and emits an audit event.

An expired lease cannot be renewed or used for review submission. It must be reclaimed/taken over.

## Governed submit

`submit()` requires all of:

- job id + item id;
- reviewer identity;
- active lease token;
- non-empty candidate selections;
- **expected durable result revision**.

The revision is mandatory in the governed path. This protects against a stale browser tab or a second process submitting after the result changed.

Submission delegates to the existing `DurableReviewCoordinator.confirm()` and therefore ultimately to `URLExtractionPipeline.confirm()`.

The reviewer can still only:

- select an existing candidate id; or
- reject a review-required field with `None`.

There is no manual replacement-value path.

## Double-review protection

Two independent guards are used:

1. **lease ownership** prevents different reviewers from simultaneously owning the same review item;
2. **durable result optimistic revision** prevents stale submissions even if two processes somehow share the same lease/reviewer context.

For SQLite queue storage, lease acquisition is executed under `BEGIN IMMEDIATE`, so independent worker processes on one runtime node cannot both successfully claim the same unexpired item.

## Partial review

A reviewer does not have to complete every review-required field in one submission.

If a submission leaves other fields in `needs_confirmation`:

- the durable result remains `review_required`;
- the lease remains active;
- the same reviewer can continue working with the new result revision.

If the result becomes:

- `ready`; or
- `incomplete` after required-field rejection/unresolution,

then the workflow emits completion audit and releases the lease automatically.

## Reject convenience path

`reject_fields(...)` is a convenience wrapper around governed submit using `None` selections.

It does not introduce a new decision mechanism and still uses the same engine confirmation contract.

## Audit trail

`ReviewAuditEvent` is append-only at the queue API level.

Actions:

- `claimed`;
- `taken_over`;
- `renewed`;
- `released`;
- `submitted`;
- `completed`.

Events can include:

- reviewer id;
- job/item identity;
- timestamp;
- lease revision;
- durable result revision before/after submit;
- field -> candidate id / `None` selections;
- small coordination metadata such as checkpoint synchronization result.

Audit events intentionally do **not** duplicate:

- candidate values;
- evidence excerpts;
- raw page content;
- lease token.

Those remain in the appropriate protected stores.

## Queue stores

`ReviewQueueStore` defines:

- load lease;
- atomic claim;
- renew;
- release/complete;
- append audit event;
- list audit history.

### `InMemoryReviewQueueStore`

Process-local/test backend.

### `JsonReviewQueueStore`

- atomic temp-file + `os.replace` snapshot persistence;
- lease + audit survive process restart;
- intended for a simple single-writer local deployment.

### `SQLiteReviewQueueStore`

- Python stdlib `sqlite3` only;
- WAL mode;
- configurable busy timeout;
- transactional `BEGIN IMMEDIATE` claim/renew/release;
- unique `(job_id,item_id)` active lease;
- append-only audit table with unique event ids;
- shared coordination for multiple review processes on one runtime node.

This remains a single-node backend. Cross-node distributed leasing belongs to a later infrastructure adapter.

## Failure ordering

Review result update and queue audit/completion are separate stores and therefore not one distributed transaction.

Submission order is:

1. verify active lease;
2. verify expected durable result revision;
3. persist governed result update;
4. append `submitted` audit;
5. if terminal, release lease with `completed` audit.

The durable result remains authoritative. If a later audit/lease write fails after the result update, the result revision prevents the old decision from being submitted again. A later reconciliation/administrative process can repair stale queue coordination state.

## Checkpoint synchronization

The underlying `DurableReviewCoordinator` still performs `DP-ENGINE-008` checkpoint synchronization after persisted review decisions when a checkpoint store is supplied.

The queue records whether checkpoint synchronization succeeded in submit audit metadata but does not duplicate checkpoint state.

## Human participation rule

Customer/reviewer participation remains minimal:

1. open/receive one automatically queued review item;
2. inspect engine-proposed candidates;
3. confirm a candidate or reject proposals.

The reviewer does not:

- inspect CSS/XPath/DOM nodes;
- decide browser/static acquisition;
- re-fetch pages;
- manually enter replacement values;
- manage job checkpoints;
- resolve reviewer races manually;
- edit leases or audit files.

## Explicit non-goals

`DP-ENGINE-009` does **not** implement:

- user authentication/login;
- SSO/OIDC/LDAP integration;
- organization-level RBAC policy engine;
- review web UI;
- cross-node/distributed leases;
- queue priorities/SLA/escalation;
- notifications;
- supervisor reassignment UI;
- cryptographic signing of review decisions;
- encryption-at-rest/key management;
- manual value corrections;
- CSS/XPath learning.

The stable reviewer/lease/audit contracts are intended for those outer layers to consume later.

## Acceptance evidence

Targeted queue/store workflow coverage includes:

- reviewer id validation;
- pending queue filters only `review_required` results;
- same-reviewer claim is idempotent;
- another reviewer cannot claim an active lease;
- `claim_next()` skips another reviewer's active work;
- expired lease takeover;
- takeover audit identifies previous reviewer without copying result data;
- renew requires lease owner and token;
- release makes work available again;
- successful candidate submit completes and releases lease;
- required-field rejection produces incomplete result and releases lease;
- partial review keeps lease active;
- expired lease cannot submit;
- stale durable result revision cannot submit;
- maximum lease duration is enforced;
- audit records candidate ids but not candidate values/lease token;
- JSON queue state survives reload;
- two SQLite connections cannot double-claim an active item and can take over after TTL.

Local targeted queue/store workflow harness: **16 tests passed**.

The repository regression file additionally exercises the real `DP-ENGINE-008` durable result/coordinator integration path.
