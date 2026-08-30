# DP-CUST-019 — release provenance integrity regression coverage

Status: `Selected for implementation`
Selected: `2026-08-30`
Baseline: `d5eee92bae854b27648e548bebb03879c1cb0bf9`
Pilot context: `Arvectum Company AC-605 / AI-ENG-001 first real supervised product task`

## Goal

Add deterministic regression coverage for the existing `scripts/release_provenance.py` release-artifact provenance generator/verifier so future customer-delivery archives cannot silently pass with missing, extra or tampered release artifacts.

This is a real Discount Parser maintenance/release-safety task. It is intentionally selected as the first AI-ENG-001 supervised product task because it has objective tests, reversible git output and no required customer or production external effect.

## Current product context

Discount Parser `0.1.16` is the current customer-delivery release baseline. DP-CUST-018 completed release preparation after DP-ENGINE-019. The repository already contains `scripts/release_provenance.py`; this task adds regression evidence around that existing contract rather than redesigning release infrastructure.

## In scope

Primary expected implementation:

- add `tests/test_release_provenance.py`;
- exercise `collect_artifacts`, `build_manifest` and/or `verify_manifest` through stable public behavior;
- prove deterministic artifact discovery/order for `discount-parser-*.zip` files;
- prove a generated manifest records filename, SHA-256 and size for every release archive;
- prove verification PASS for an untampered artifact set;
- prove verification fails closed when a declared archive is modified;
- prove verification fails closed when a declared archive is missing;
- prove verification fails closed when an extra matching release archive exists outside the manifest;
- prove unsupported provenance schema fails closed.

A minimal change to `scripts/release_provenance.py` is allowed only if the new tests expose a real defect in the existing contract. Any such behavior change must be narrowly justified in the final report.

## Out of scope

- no parser/extraction/runtime behavior changes;
- no source adapter changes;
- no database/schema migration;
- no UI change;
- no version bump;
- no release/tag creation;
- no modification of customer delivery artifacts;
- no network calls;
- no new package/runtime dependency;
- no credentials/secrets;
- no automatic commit/push/merge/release/deploy/customer delivery;
- no Company/Product/Arvectum OS authority-boundary change.

## Acceptance criteria

1. New release-provenance regressions run deterministically on macOS/Linux without external services.
2. Valid manifest/artifact verification passes.
3. Tampered artifact verification fails closed.
4. Missing artifact verification fails closed.
5. Extra release archive verification fails closed.
6. Unsupported schema verification fails closed.
7. Existing release provenance behavior remains backward compatible unless a concrete defect is demonstrated and minimally corrected.
8. Targeted tests pass.
9. Full existing Discount Parser test suite is run if practical within the pilot timeout; any unrelated pre-existing failure is reported rather than silently fixed.
10. AI-ENG-001 stops at `READY_FOR_OWNER`; no promotion is performed autonomously.

## Preferred change boundary

Expected allowed paths:

- `tests/test_release_provenance.py`
- `scripts/release_provenance.py` only if strictly necessary for a defect revealed by the tests

No other path should change without escalation.

## Suggested validation

```bash
python3 -m pytest -q tests/test_release_provenance.py
python3 -m pytest -q
```

The second command may be expensive but is preferred if it completes inside the bounded pilot window.

## Pilot evidence required

AI-ENG-001 must preserve:

- baseline SHA;
- isolated worktree path;
- executor prompt/stdout/stderr;
- changed paths and diff;
- targeted/full test results;
- attempts/rework if any;
- final state;
- Owner intervention count/time when observable.

Technical PASS does not by itself approve promotion or imply customer readiness/profitability.
