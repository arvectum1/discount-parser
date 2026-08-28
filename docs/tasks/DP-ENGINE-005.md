# DP-ENGINE-005 — Learned site-profile / confirmation-learning layer

**Status:** implemented

## Goal

Reduce repeated human review on the same site without introducing manual CSS/XPath configuration or memorizing previously confirmed business values.

`DP-ENGINE-005` learns only structural evidence preferences from explicit reviewer actions. A confirmation teaches the engine which evidence pattern was trustworthy for a field on a specific site; future candidates with the same structural pattern receive a bounded confidence adjustment.

## Core rule: learn structure, never values

The profile store does **not** persist candidate values.

For the built-in `AutoDiscoveryProvider`, learning fingerprints are derived from:

- evidence kind (`jsonld`, `html_meta`, `html_label_value`, `text_label_value`, etc.);
- normalized evidence source reference;
- matched semantic field terms/aliases.

Examples:

- `script[12].offers[0].price` → `script[*].offers[*].price`;
- `line:143` → `line:*`;
- `dt/dd:Цена` remains the semantic label/value structure.

A confirmed price of `199` therefore teaches “this site/field trusts this structural signal”, not “the price is 199”.

## In scope

- exact-site profile keys based on normalized host (and non-default port when present);
- `EvidenceFingerprint` and deterministic source-reference normalization;
- `ProfileSignalStats`;
- bounded `LearningPolicy`;
- `SiteProfileStore` protocol;
- `InMemorySiteProfileStore`;
- atomic `JsonSiteProfileStore`;
- `ProfileAwareProvider` wrapper for candidate confidence adjustment;
- `ConfirmationLearner`;
- positive learning for the selected candidate;
- negative learning for competing/rejected candidates;
- overlap protection so the same structural fingerprint is not both rewarded and penalized in one review;
- `LearningEvent` audit records containing candidate ids/fingerprints but no values;
- default integration into `URLExtractionPipeline`;
- non-strict learning-failure isolation by default;
- optional strict learning mode;
- opt-out through `learning_enabled=False`.

## Site boundary

Learning is intentionally conservative.

Profiles are keyed by the final asset host:

- subdomains are not collapsed together;
- default ports are normalized away;
- non-default ports remain part of the key;
- a profile learned for `shop.example.com` does not affect `other.example.com`.

This avoids accidental trust transfer between unrelated applications hosted under the same parent domain.

## Confidence policy

Default adjustment policy:

- each confirmation: `+0.06`;
- each rejection/competing loss: `-0.08`;
- positive adjustment capped at `+0.18`;
- negative adjustment capped at `-0.24`;
- final candidate confidence capped at `0.99`.

The adjustment is applied only when the current candidate exposes a structural fingerprint already present in the site profile.

This makes learning useful but bounded:

- a previously ambiguous pair of strong structured signals can usually become automatic after an explicit confirmation on the same site;
- a weak plain-text fallback (`~0.70`) normally remains review-required after one confirmation and only crosses the default `0.80` threshold after repeated consistent confirmation;
- stale or repeatedly rejected patterns lose confidence instead of becoming permanent rules.

## Confirmation semantics

Learning occurs only after the existing `ExtractionEngine.confirm()` accepts the review action.

For a selected candidate:

1. its structural fingerprints are recorded as positive;
2. competing candidate fingerprints are recorded as negative;
3. overlapping fingerprints are removed from the negative set.

For an explicit rejection (`candidate_id=None`), all proposed structural fingerprints are recorded as negative.

Auto-selected fields do not self-train. This prevents the engine from amplifying its own guesses without human evidence.

## Orchestration integration

`URLExtractionPipeline()` now enables in-memory learning by default.

The normal flow is:

1. acquire URL;
2. discover candidates through a `ProfileAwareProvider`;
3. resolve normally;
4. if review is required, reviewer confirms/rejects an existing candidate;
5. record structural learning;
6. future runs on the same site apply the learned confidence adjustment.

`URLExtractionResult.learning_events` exposes successful learning actions.

`URLExtractionResult.learning_warnings` records non-fatal profile-store failures.

For persistent production learning, pass a JSON store:

```python
from arvectum_data import JsonSiteProfileStore, URLExtractionPipeline

pipeline = URLExtractionPipeline(
    profile_store=JsonSiteProfileStore("state/site-profiles.json"),
)
```

The JSON file is rewritten atomically.

## Failure behavior

Profile learning is an optimization layer, not the authoritative extraction decision.

By default, a profile-store write failure does not invalidate a successfully confirmed extraction. The returned result contains a `profile_learning_failed:...` warning.

Set `strict_learning=True` when infrastructure policy requires persistence failures to raise.

## Human participation rule

No new normal human configuration is introduced.

The reviewer still only:

- confirms one engine-proposed candidate; or
- rejects the candidates.

The reviewer does **not**:

- write CSS selectors;
- write XPath;
- identify DOM line numbers;
- enter replacement values;
- choose a browser mode;
- edit a site profile manually.

The confirmation itself becomes the training signal.

## Explicit non-goals

`DP-ENGINE-005` does **not** implement:

- memorization of business values;
- free-form manual corrections;
- CSS/XPath selector learning;
- cross-domain/global trust transfer;
- ML/LLM model training;
- semantic embeddings;
- automatic learning from auto-selected output;
- profile TTL/decay scheduling;
- database-backed multi-worker synchronization;
- authentication/cookie learning;
- CAPTCHA/anti-bot adaptation;
- OCR/VLM learning;
- publication/output persistence;
- Arvectum OS scheduling.

Database-backed profile persistence, lifecycle/decay policy and distributed synchronization can be added behind the `SiteProfileStore` contract later.

## Acceptance evidence

The `DP-ENGINE-005` regression set covers:

- dynamic JSON-LD/list indices normalize into stable fingerprints;
- line-number evidence normalizes without preserving unstable line positions;
- explicit confirmation rewards the selected structure and penalizes competitors;
- candidate values are absent from profile persistence;
- changed values on the same site reuse learned structure;
- another host receives no learned adjustment;
- repeated weak-signal confirmation is bounded and gradual;
- rejecting all candidates records negative evidence only;
- JSON profile persistence survives reload;
- learning events contain no candidate values;
- learning failures are non-fatal by default;
- strict learning mode can surface persistence failures;
- `learning_enabled=False` preserves the pre-005 behavior;
- existing custom extraction integrations remain valid.

Local targeted `DP-ENGINE-005` harness: **14 tests passed** before repository integration.
