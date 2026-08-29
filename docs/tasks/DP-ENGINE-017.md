# DP-ENGINE-017 — live source parity telemetry & controlled legacy retirement

## Goal

Convert DP-ENGINE-016 from fixture-proven migration into an observable live production migration without exposing any new customer/operator tuning.

The five shipped Discount Parser sources continue to use the generic discovery/relevance/acquisition path and generic DP-014 multi-record decoder. The legacy source adapter remains a safety mechanism until live evidence proves that it can be reduced.

## Runtime states

### `observing`

Default and safe state.

For every selected page that reaches decoding:

1. generic DP-014 multi-record decode runs;
2. legacy `parse(html)` runs against the exact same already-acquired HTML;
3. DP-016 safe-superset parity decides which output is returned.

No second network request is introduced.

### `generic_primary`

First controlled retirement stage.

Promotion requires, by default:

- at least 30 consecutive live parity-pass pages;
- at least 3 consecutive clean source runs containing parity observations.

After promotion:

- generic usable records are returned directly on most pages;
- a rotating approximately 1/10 sample still runs the legacy oracle;
- stable URL sets rotate through different sample slots between runs;
- generic not-ready/error always invokes the legacy adapter as an emergency fallback;
- any sampled mismatch, generic not-ready/error, or missing safety oracle records failure evidence and returns the source to `observing`.

DP-017 intentionally does **not** implement a legacy-free state.

## Durable telemetry

Alembic `0010` adds bounded per-source aggregate state in `source_parity_state`:

- current retirement mode;
- total parity-observed/pass/failure pages;
- current consecutive pass window;
- consecutive clean runs;
- direct generic pages after promotion;
- emergency fallback pages;
- last failure timestamp and bounded reason.

The table is aggregate-only; it does not grow one row per page and does not persist raw HTML, offer bodies, credentials, or candidate values.

Pre-0010 databases fail safe to `observing`; telemetry persistence failure must never stop collection.

## Operator/developer visibility

`discount-parser parse` now reports per-run engine counters including:

- generic/legacy pages;
- parity observed/pass/failure pages;
- direct generic pages;
- emergency fallback pages;
- retirement mode before/after the run.

`discount-parser parity-report` returns the durable aggregate state for all observed sources.

These are diagnostic interfaces. The customer is not asked to select a mode, threshold, page, DOM element, CSS selector, XPath, browser toggle, or candidate value.

## Safety invariants

1. Direct generic pages never count as parity proof because no oracle ran.
2. Any live parity failure immediately revokes `generic_primary`.
3. Promotion requires a fresh consecutive evidence window after every failure.
4. `generic_primary` retains both rotating legacy sampling and emergency fallback.
5. Sampling and fallback reuse already-acquired HTML; no duplicate network fetch is introduced.
6. Existing DP-016 safe-superset rules remain the parity authority.
7. Legacy/non-hybrid sources are unaffected.

## Non-goals

- complete deletion of source adapters;
- live network calls from CI;
- customer-visible retirement controls;
- per-site selectors or thresholds;
- distributed telemetry or remote analytics;
- replacing the existing offer persistence/review model.

## Acceptance

- promotion/demotion state-machine regressions;
- direct generic pages cannot self-prove parity;
- rotating sampled oracle in generic-primary mode;
- emergency fallback on generic not-ready;
- sampled mismatch returns legacy for that page and revokes mode;
- Alembic 0010 persistence and `parity-report` regression;
- full repository CI on Ubuntu, Windows, macOS ARM and macOS Intel;
- delivery packaging and existing Windows acceptance gates remain green.
