# DP-ENGINE-001 — Domain-neutral extraction engine baseline

**Status:** implemented

## Goal

Establish the first reusable extraction-engine contract for Arvectum Data Platform so Discount Parser, Doors Parser and future data products can share one governed decision layer.

Candidate discovery belongs to adapters/providers. Candidate orchestration, confidence/evidence handling and the automatic-vs-human-review decision belong to this engine.

## In scope

- domain-neutral `RawAsset` and `FieldSpec` contracts;
- evidence-backed `Candidate` contract;
- pluggable `CandidateProvider` protocol;
- deterministic confidence/margin resolution;
- explicit `auto_selected`, `needs_confirmation`, `confirmed`, `rejected` and `unresolved` states;
- provider-failure isolation;
- a generic structured-attribute provider;
- required-field visibility;
- human confirmation constrained to engine-proposed candidates;
- regression tests for the above behavior.

## Human participation rule

The engine must not require an operator to locate DOM nodes, CSS/XPath selectors, line numbers or manually retype extracted values.

For review-required fields, the human action is intentionally limited to:

1. confirm one candidate already proposed by the engine; or
2. reject the candidates.

There is no manual value-entry parameter in `ExtractionEngine.confirm()`. A domain UI may present evidence and candidate choices, but it must preserve this contract unless a later governed task explicitly introduces a separate override authority.

## Resolution rule

For each field:

1. collect candidates from all successful providers;
2. sort deterministically by confidence, provider name and candidate id;
3. if no candidate exists → `unresolved`;
4. if top confidence is below `FieldSpec.min_confidence` → `needs_confirmation`;
5. if the top two candidates are separated by less than `FieldSpec.min_margin` → `needs_confirmation`;
6. otherwise → `auto_selected`.

Provider exceptions are recorded in `provider_errors` and do not erase candidates produced by healthy providers.

## Explicit non-goals

`DP-ENGINE-001` does **not** implement:

- HTTP/browser transport;
- DOM, visual or semantic candidate discovery;
- OCR/VLM extraction;
- Discount- or Doors-specific schemas;
- database persistence;
- jobs/queues/scheduling;
- Arvectum OS runtime integration;
- learning/adaptation from reviewer confirmations.

Those capabilities can be layered on top without changing the engine's domain-neutral contract.

## Acceptance evidence

The baseline test suite verifies:

- strong candidates are selected automatically;
- weak or ambiguous candidates are held for confirmation;
- only an existing candidate id can be confirmed;
- rejection is supported without manual replacement values;
- auto-selected fields cannot be silently overwritten through the review API;
- unresolved required fields are surfaced;
- a broken provider does not break successful extraction from another provider;
- the generic attribute provider does not encode a business domain;
- duplicate provider names and field keys are rejected.

Local acceptance run for this implementation: **8 tests passed**.
