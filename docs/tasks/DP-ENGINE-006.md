# DP-ENGINE-006 — Site-profile lifecycle, versioning and backend persistence

**Status:** implemented

## Goal

Prevent `DP-ENGINE-005` learned site profiles from becoming permanent truth, and provide a persistence backend suitable for a multi-process Arvectum runtime on one node.

The core rule remains unchanged: profiles contain only structural evidence statistics. They never persist confirmed business values, CSS selectors, XPath, DOM line numbers or free-form corrections.

## Lifecycle model

Each structural fingerprint now stores:

- effective confirmation weight;
- effective rejection weight;
- `updated_at` timestamp.

Default `ProfileLifecyclePolicy`:

- half-life: **30 days**;
- hard TTL: **180 days**;
- physical-prune threshold: effective total weight `<= 0.01`.

At read time, counters are decayed exponentially from `updated_at`.

After one half-life, a confirmation weight of `1.0` becomes `0.5`. This immediately reduces the candidate confidence adjustment even if no maintenance job has run.

After the hard TTL the signal returns zero effective weight and therefore no longer influences extraction.

## No stale-history revival

When fresh feedback arrives for an existing fingerprint, the store first materializes the already-decayed old weight at the current timestamp and only then adds the new event.

Example:

1. one confirmation => weight `1.0`;
2. 30 days pass => effective weight `0.5`;
3. another confirmation => stored effective weight becomes `1.5`, not `2.0`.

Old evidence therefore cannot regain full strength merely because a new review occurred.

## Lazy correctness vs physical maintenance

Expiration is enforced during `get_stats()`.

That means an expired signal cannot influence candidate scoring even if it still physically exists in JSON/SQLite.

`prune()` is a storage-maintenance operation. It removes:

- signals past hard TTL;
- signals whose decayed total weight is below the configured prune threshold;
- empty field/site containers.

`ProfilePruneReport` records removed patterns/fields/sites, store revision and maintenance timestamp.

`URLExtractionPipeline.maintain_profiles()` exposes this as a scheduler-friendly hook for a future Arvectum OS maintenance job.

## Store schema versioning

`PROFILE_SCHEMA_VERSION = 2`.

Schema v2 adds:

- `updated_at` for each fingerprint;
- top-level monotonic `revision`;
- lifecycle-aware floating effective counters.

Every successful learning mutation increments `revision`. A pruning operation increments revision only when it actually removes data.

The revision is available through `store.revision` and the persisted/snapshot payload, giving runtime/cache layers a stable change token without exposing candidate values. `DP-ENGINE-006` intentionally does not alter the existing `LearningEvent` or candidate metadata contracts solely to duplicate that token.

## JSON migration

`JsonSiteProfileStore` remains the simple local persistent backend.

Existing v1 files are accepted. Because v1 did not contain timestamps, migration conservatively treats existing counters as fresh at migration time, writes `updated_at`, upgrades the file to schema v2 and preserves all structural counters.

Unsupported future/unknown schema versions fail explicitly.

JSON writes remain atomic through temporary-file + `os.replace`.

JSON is **not** the multi-process backend: two writers can still race at whole-file level.

## SQLite backend

`SQLiteSiteProfileStore` is the production-oriented backend added by this task.

It uses Python stdlib `sqlite3` only and therefore adds no package dependency.

Behavior:

- one row per `(site_key, field_key, fingerprint)`;
- WAL journal mode;
- configurable SQLite busy timeout;
- transactional `BEGIN IMMEDIATE` read/decay/upsert;
- shared monotonic revision in metadata;
- concurrent processes on one runtime node observe the same database state;
- explicit `close()` plus context-manager support;
- the same lifecycle and value-free profile contract as memory/JSON.

This is intentionally a **single-node multi-process** backend. A network/distributed database adapter can later implement the same `SiteProfileStore` protocol without changing extraction/orchestration.

## Backward compatibility

`DP-ENGINE-005` public concepts remain valid:

- `LearningPolicy` still maps confirmation/rejection weights into bounded confidence adjustments;
- `InMemorySiteProfileStore` still works with no arguments;
- `JsonSiteProfileStore(path)` still works with no extra configuration;
- `ConfirmationLearner` still calls the same `record(... positive=..., negative=...)` contract;
- `ProfileAwareProvider` still consumes the same `get_stats(...)` result shape.

`ProfileSignalStats` itself is unchanged. Lifecycle stores may return fractional `confirmations`/`rejections` values at runtime because Python does not enforce the dataclass's integer annotations; fresh events remain exact integer-equivalent values.

## Human participation

No new human action is introduced.

The customer/reviewer still only confirms or rejects engine-proposed candidates when the governed extraction result requires review.

Lifecycle, aging, storage revisioning and pruning are infrastructure behavior. The customer does not:

- schedule per-site TTL;
- edit timestamps;
- tune selectors;
- choose a persistence backend per page;
- manually clean learned profiles.

## Explicit non-goals

`DP-ENGINE-006` does **not** implement:

- network/distributed SQL;
- cross-node consensus;
- Redis/cache coordination;
- automatic Arvectum OS scheduler registration;
- per-site custom lifecycle policies in UI;
- candidate-value persistence;
- CSS/XPath learning;
- authentication/session learning;
- CAPTCHA/anti-bot adaptation;
- OCR/VLM learning;
- publication/output storage.

## Acceptance evidence

The targeted regression harness verifies:

- 30-day half-life decay;
- fresh feedback adds to decayed history instead of reviving it;
- hard TTL makes a signal ineffective before physical pruning;
- `prune()` removes expired patterns/fields/sites;
- revision increments on learning and changed maintenance;
- v1 JSON migrates to schema v2 with timestamps;
- JSON revision survives reload;
- unknown JSON schema fails explicitly;
- two independent SQLite store instances share writes/revision;
- SQLite applies the same hard-TTL pruning behavior;
- SQLite persistence survives reopen;
- learning policy consumes fractional decayed weights;
- existing `ProfileAwareProvider` consumes decayed store stats without modification;
- pipeline maintenance delegates to the configured store.

Focused local lifecycle/compatibility harnesses passed before repository integration.
