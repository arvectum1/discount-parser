# DP-ENGINE-002 — Candidate discovery/provider layer

**Status:** implemented

## Goal

Turn the `DP-ENGINE-001` provider contract into an automatic candidate-discovery layer that can inspect already-fetched HTML/text and propose evidence-backed field values without per-site CSS/XPath configuration.

The operator remains outside discovery. Domain configuration supplies semantic field names and optional aliases once; runtime discovery finds values automatically and the existing engine decides whether they are safe to auto-select or require confirmation.

## In scope

- backward-compatible HTML support on `RawAsset`;
- semantic aliases on `FieldSpec`;
- `AutoDiscoveryProvider` implementing the existing `CandidateProvider` contract;
- recursive JSON-LD discovery;
- HTML `meta` / `itemprop` discovery;
- DOM semantic element discovery from `itemprop`, `data-field`, `data-label`;
- common label/value structures (`dt/dd`, `th/td`);
- document-title signal;
- plain-text `label: value` / dash-separated fallback;
- deterministic semantic-name normalization across case, punctuation, snake_case and camelCase-style names;
- evidence provenance for every signal;
- same-value corroboration and confidence strengthening;
- conflict preservation so competing values continue through the `DP-ENGINE-001` margin/review gate;
- malformed JSON-LD isolation;
- regression coverage with the entire `DP-ENGINE-001` suite.

## Discovery priority / confidence

The provider deliberately treats portable machine-readable structures as stronger evidence than presentation text:

1. JSON-LD scalar property: `0.96`;
2. HTML meta: `0.93`;
3. `itemprop` attribute value: `0.90`;
4. semantic DOM element: `0.88`;
5. DOM label/value pair: `0.84`;
6. document `<title>`: `0.74`;
7. plain-text label/value fallback: `0.72`.

Matching through a configured semantic alias applies a small confidence penalty (`0.02`). With the default `FieldSpec.min_confidence=0.80`, plain-text fallback therefore goes to human confirmation rather than being published automatically.

## Corroboration rule

Signals are grouped by `(field, canonical value)` before they are converted into engine candidates.

- Repeated evidence for the **same value** becomes one candidate, not competing candidates.
- Independent evidence kinds add a bounded confidence bonus (`+0.03` per additional kind, maximum `+0.09`, total capped at `0.99`).
- Evidence entries and matched semantic terms remain attached to the candidate.
- Different values are never merged. They remain separate candidates, allowing the existing confidence-margin rule to require confirmation.

This is important because JSON-LD and meta tags frequently repeat the same source value; repetition is corroboration, not ambiguity.

## No-selector rule

`AutoDiscoveryProvider` does not inspect or accept CSS selectors, XPath expressions, DOM line numbers, element ids, or CSS classes as field configuration.

`FieldSpec.aliases` are semantic vocabulary only. Example: a canonical field `price` may have aliases such as `Цена` or `Стоимость`. The runtime provider searches machine-readable names and visible label/value structures; it does not require the customer to identify where those labels occur in a page.

## Human participation rule

Unchanged from `DP-ENGINE-001`:

- high-confidence unambiguous candidates may be auto-selected;
- weak/conflicting candidates are surfaced as `needs_confirmation`;
- a reviewer may confirm an engine-proposed candidate or reject the candidates;
- manual value entry is not introduced by this task.

## Explicit non-goals

`DP-ENGINE-002` does **not** implement:

- HTTP requests, browser automation or JavaScript rendering;
- anti-bot handling or credentials;
- OCR/VLM/image candidate discovery;
- fuzzy/LLM semantic inference beyond explicit field names/aliases;
- domain-specific schemas for discounts, doors, procurement or catalogs;
- persistence of learned reviewer decisions;
- per-site selector learning;
- job scheduling / Arvectum OS deployment.

Transport and rendered-page acquisition should remain a separate layer feeding `RawAsset.html`/`RawAsset.text`.

## Acceptance evidence

New discovery tests verify:

- JSON-LD exact-key discovery;
- same-value JSON-LD + meta corroboration merges into one candidate;
- conflicting structured values require confirmation;
- semantic alias discovery from `dt/dd` without selectors;
- semantic alias discovery from `th/td`;
- low-confidence plain-text fallback goes to confirmation;
- script/style text is excluded from visible-text discovery;
- generic document title discovery;
- malformed JSON-LD does not block healthy discovery sources;
- `itemprop` discovery;
- CSS classes are not treated as semantic field configuration.

Combined local acceptance run: **19 tests passed** (`11` DP-ENGINE-002 tests + `8` DP-ENGINE-001 regression tests).
