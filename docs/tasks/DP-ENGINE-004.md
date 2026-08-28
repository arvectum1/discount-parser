# DP-ENGINE-004 — End-to-end URL extraction orchestration

**Status:** implemented

## Goal

Provide one governed execution path from a URL to an `ExtractionResult` without requiring application code or an operator to manually connect the acquisition (`DP-ENGINE-003`) and candidate-discovery/extraction (`DP-ENGINE-001/002`) layers.

The orchestration layer does not duplicate those responsibilities. It composes them while preserving evidence from both sides of the boundary.

## Public execution path

The normal API is:

```python
pipeline = URLExtractionPipeline()
result = pipeline.extract_url(url, fields)
```

The default pipeline wires:

1. `AcquisitionEngine()` — HTTP-first, automatic rendered-page fallback;
2. `AutoDiscoveryProvider()` — selector-free semantic candidate discovery;
3. `ExtractionEngine` — confidence/margin resolution and confirmation-only review.

Infrastructure callers may instead pass an explicit `AcquisitionRequest` through `extract(request, fields)` or inject custom acquisition/extraction implementations/providers.

## Result contract

`URLExtractionResult` retains:

- `acquisition`: the complete `AcquisitionResult`, including attempts/warnings and transport provenance;
- `extraction`: the complete `ExtractionResult`, including candidates, evidence, decisions and provider errors.

Convenience accessors expose:

- `asset`;
- `values()`;
- `requires_confirmation`;
- `unresolved_required_fields`;
- `ready`.

No evidence is flattened or discarded merely to provide the higher-level API.

## Ready gate

`result.ready` is true only when both conditions hold:

1. no field is in `needs_confirmation`;
2. no required field is unresolved or rejected.

Provider warnings/errors do not automatically block `ready` when the governed extraction result itself is complete; the underlying evidence remains available for policy layers that want stricter behavior later.

This is intentionally a narrow readiness signal, not a publication/compliance policy.

## Review continuation

`URLExtractionPipeline.confirm(result, selections)` delegates to the existing `ExtractionEngine.confirm()` contract.

Therefore:

- only engine-proposed candidates may be confirmed;
- rejection remains supported;
- manual value entry is still absent;
- acquisition evidence is preserved unchanged after review;
- `ready` is recalculated from the new extraction state.

The reviewer never needs to reacquire the page or reconstruct transport context merely to confirm an ambiguous field.

## Minimal-human rule

The default end-to-end path introduces no new operator/customer configuration step.

The caller supplies:

- URL;
- semantic `FieldSpec` definitions/aliases.

The platform then automatically:

1. acquires the page;
2. decides static vs rendered acquisition;
3. discovers field candidates;
4. resolves strong/unambiguous values;
5. surfaces only weak/conflicting candidates for confirmation.

The customer is not asked to choose HTTP/browser mode, inspect DOM nodes, write selectors, wire providers, or copy extracted values manually.

## Failure semantics

- Acquisition failure stops the pipeline before extraction and preserves the existing `AcquisitionError` semantics.
- Discovery/provider failures remain isolated inside `ExtractionResult.provider_errors` according to `DP-ENGINE-001`.
- A successful acquisition is never silently replaced with an invented extraction value.
- Confirmation preserves the original acquisition result.

## Extensibility

The orchestration constructor supports three extension points:

- custom `acquisition` engine;
- custom complete `extraction` engine; or
- a provider sequence used to construct the extraction engine.

Passing both a complete `extraction` engine and `providers` is rejected as ambiguous configuration.

This allows future domain adapters, visual/OCR providers, learned site profiles or governed enterprise transports to plug in without changing the URL-to-result contract.

## Explicit non-goals

`DP-ENGINE-004` does **not** implement:

- persistence/database models;
- batch/job queues;
- retry/backoff scheduling;
- learned site profiles from reviewer confirmations;
- login/session orchestration;
- CAPTCHA/anti-bot bypass;
- OCR/VLM/image extraction;
- publication/export sinks;
- Arvectum OS runtime scheduling;
- domain schemas for discounts, doors, procurement or catalogs.

Those capabilities should consume or extend the stable orchestration contract rather than bypassing it.

## Acceptance evidence

The local orchestration harness verifies:

- acquisition output is passed directly to extraction;
- URL convenience input becomes an `AcquisitionRequest` with transport controls preserved;
- any review-required field makes `ready=False`;
- unresolved required fields make `ready=False`;
- confirmation preserves acquisition evidence;
- complete extraction-engine injection and provider injection cannot be combined ambiguously;
- acquisition failure stops before extraction;
- `values(include_unconfirmed=...)` is forwarded to the governed extraction result.

Local `DP-ENGINE-004` orchestration harness: **8 tests passed**.
