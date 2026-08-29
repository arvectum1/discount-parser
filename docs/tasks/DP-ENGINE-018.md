# DP-ENGINE-018 — live engine acceptance & delivery-readiness evidence

## Goal

Turn the DP-ENGINE-017 live parity mechanism into a bounded production acceptance contract for the five shipped Discount Parser sources.

DP-ENGINE-018 answers two separate questions from one ordinary production collection path:

1. **Is the customer data path safe right now?**
2. **Has the generic DP Engine accumulated enough live evidence to reduce its dependency on legacy adapters?**

These questions are intentionally not conflated. A legacy fallback may preserve correct customer output while simultaneously proving that generic-engine retirement is not ready.

## Relationship to existing acceptance

DP-WIN-003 already validates real source reachability, routing, scheduler behavior, database integrity and network privacy. DP-ENGINE-018 does not create a second route-probing or acquisition stack.

DP-ENGINE-018 calls the existing production `src.sources.runner.run_all()` path. Therefore every requested acceptance cycle is an ordinary Discount Parser production collection using the same:

- source configuration;
- discovery/relevance path;
- acquisition/render behavior;
- generic multi-record decoder;
- DP-016 safe-superset parity;
- DP-017 retirement state;
- normalization/deduplication/database persistence.

No extra fetch is introduced merely to generate acceptance evidence.

## CLI

```text
discount-parser engine-acceptance
```

Options:

- `--source <key>` — developer diagnostic for one configured source;
- `--config <path>` — alternate source config for controlled testing;
- `--runs 1|2|3` — number of ordinary production collection cycles, default `1` and hard-capped at `3`;
- `--output <path>` — JSON evidence destination, default `output/dp_engine_acceptance.json`.

The three-cycle cap is deliberate. It permits the DP-017 minimum of three clean runs to be collected without turning an acceptance command into an unbounded crawler.

## Verdicts

### `PASS`

For every evaluated source:

- the customer path returned data without collection errors;
- the source is still in the shipped `hybrid` runtime contract;
- the generic path was exercised on live selected/decoded pages;
- the current acceptance cycle had no runtime-wide generic fallback;
- no parity mismatch was observed;
- no generic emergency fallback was required;
- durable DP-017 state is `generic_primary` and satisfies the configured retirement evidence thresholds.

`PASS` means the current live cycle is healthy **and** controlled legacy reduction has sufficient accumulated evidence.

### `NEEDS_EVIDENCE`

The customer path is healthy and no hard engine discrepancy occurred, but the generic path or retirement window is not yet sufficiently proven. Typical reasons:

- fewer than 30 consecutive parity-pass pages;
- fewer than 3 clean parity-observing runs;
- no parity observation in the current observing-mode cycle;
- the current cycle did not exercise a generic selected/decoded page.

This is an expected non-failing status immediately after DP-ENGINE-017 adoption. The CLI exits with code `0` for both `PASS` and `NEEDS_EVIDENCE`.

### `FAIL`

A real acceptance problem was observed, including:

- a configured shipped source did not return a run result;
- collection errors or an empty production fetch;
- unexpected non-hybrid runtime mode;
- whole-engine runtime fallback;
- parity mismatch;
- generic emergency fallback.

The CLI exits with code `1` for `FAIL`.

## Customer-path safety vs engine readiness

The report exposes `customer_path_safe` separately from the DP Engine verdict.

Example: a sampled generic/legacy mismatch in `generic_primary` causes the DP-017 safety mechanism to return legacy output and demote the source to `observing`. In that case:

- `customer_path_safe` may remain `true` if usable data was returned without persistence errors;
- DP-ENGINE-018 status is `FAIL` because generic retirement evidence was invalidated.

This preserves product truth instead of reporting either a false customer outage or a false generic-engine success.

## Evidence model

The JSON report contains only bounded aggregate evidence:

- source keys;
- verdicts and stable reason codes;
- run counts and fetched/error counters;
- selected/decoded/generic/legacy page counters;
- parity observed/pass/failure counters;
- direct-generic/emergency-fallback counters;
- retirement mode before/after;
- safe aggregate DP-017 state before/after;
- policy thresholds;
- timing and overall verdict.

It intentionally excludes:

- raw HTML;
- offer titles/descriptions/promo codes or other business values;
- source/page URLs;
- runtime warning text;
- exception strings;
- credentials/tokens;
- DP-017 `last_failure_reason`, because diagnostics may contain a page URL.

The report file is written atomically.

## Production baseline

The shipped baseline remains the five enabled hybrid sources in `config/sources.yaml`:

- `promokood`;
- `promokodik`;
- `berikod`;
- `promokodi_net_ru`;
- `promko`.

A regression test locks this baseline for the current customer-delivery phase.

## Safety invariants

1. Acceptance uses the existing production runner instead of a parallel extraction implementation.
2. No extra network fetch exists solely for evidence generation.
3. `NEEDS_EVIDENCE` never masquerades as a product failure.
4. A parity mismatch or emergency generic fallback can never produce `PASS`.
5. Historical parity failures do not permanently poison a source after DP-017 has rebuilt a fresh consecutive window and promoted it again.
6. A source already in `generic_primary` does not need a sampled oracle on every acceptance cycle; rotating sampling semantics from DP-017 remain authoritative.
7. Evidence contains no raw customer/business/source-page content.
8. Customer/operator participation remains zero for normal acceptance; no selector, DOM, XPath, browser or threshold tuning is exposed.

## CI strategy

CI does **not** access live source websites.

Regression tests inject production-shaped `RunResult` and `SourceParityState` evidence to cover:

- healthy observing state -> `NEEDS_EVIDENCE`;
- complete `generic_primary` evidence -> `PASS`;
- sampled parity mismatch -> engine `FAIL` while customer-path truth remains visible;
- emergency fallback -> `FAIL`;
- collection/empty-fetch failure -> customer path unsafe;
- missing source result;
- whole-engine fallback;
- non-hybrid source regression;
- one-to-three-cycle bound and aggregation;
- generic path not exercised;
- privacy/non-leakage;
- atomic evidence output;
- five-source shipped baseline;
- CLI non-failing `NEEDS_EVIDENCE` semantics.

Full repository CI, delivery packaging, Windows reproducibility and installed acceptance remain required before merge.

## Non-goals

- deleting the five legacy adapters;
- changing DP-017 promotion thresholds;
- running live internet tests inside GitHub Actions;
- replacing DP-WIN-003 network acceptance;
- adding customer-facing engine controls;
- creating a universal external telemetry service;
- changing the existing offer normalization/dedup/publication model.
