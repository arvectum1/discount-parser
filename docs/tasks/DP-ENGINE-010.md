# DP-ENGINE-010 — Quality-driven rendered retry / semantic acquisition recovery

**Status:** implemented

## Goal

Close the gap between transport-level page usefulness and semantic extraction usefulness.

`DP-ENGINE-003` can already render pages when HTTP fails or when static HTML looks like an obvious JavaScript shell. A different class of page can look perfectly usable to acquisition heuristics while still hiding required business fields until JavaScript executes.

`DP-ENGINE-010` adds a second, domain-neutral recovery gate at orchestration level:

1. acquire normally;
2. extract normally;
3. inspect governed field outcomes;
4. when `AUTO` static extraction leaves required fields unresolved, try one rendered acquisition;
5. extract the rendered page;
6. deterministically keep the better governed result.

The customer/operator does not choose static vs browser per site.

## Trigger boundary

Semantic recovery is attempted only when all of the following are true:

- `SemanticRecoveryPolicy.enabled` is true;
- request `render_mode` is `AUTO`;
- the first acquisition has not already used a renderer;
- at least one required field is `unresolved` or `rejected`.

This means:

- a ready static result does not render;
- optional unresolved fields alone do not trigger a browser;
- `NEVER` remains a hard prohibition on browser recovery;
- `ALWAYS` renders once and is never double-rendered;
- an `AUTO` acquisition that already fell back to browser is not rendered again.

## Layer responsibility

The split between acquisition and orchestration remains deliberate.

### Acquisition layer

`AcquisitionEngine` decides whether rendering is needed based on transport/page-shape evidence:

- HTTP failure;
- JavaScript-required message;
- obvious client-rendered shell;
- empty HTML.

### Orchestration layer

`URLExtractionPipeline` decides whether a second rendered attempt is useful based on semantic contract evidence:

- required `FieldSpec` outcomes remain unresolved.

No site selector or site-specific transport rule is introduced.

## SemanticRecoveryPolicy

`SemanticRecoveryPolicy` is enabled by default and can be injected into `URLExtractionPipeline`.

The policy exposes:

- `should_retry(...)` — whether a rendered semantic recovery is permitted/needed;
- `quality(...)` — a domain-neutral `ExtractionQuality` summary;
- `prefer_rendered(...)` — deterministic comparison of static vs rendered governed outcomes.

Applications can disable the feature globally with:

```python
URLExtractionPipeline(
    semantic_recovery_policy=SemanticRecoveryPolicy(enabled=False),
)
```

This is a deployment/testing control, not normal per-site customer configuration.

## ExtractionQuality

Rendered and static results are compared without looking at business values.

Quality priority is lexicographic:

1. fewer unresolved/rejected **required** fields;
2. fewer required fields needing confirmation;
3. fewer unresolved/rejected fields overall;
4. fewer fields needing confirmation overall;
5. more auto-selected/confirmed fields.

The comparison intentionally does **not** use candidate confidence as a final tie-break.

If governed statuses/coverage are equal, the static result remains authoritative. This avoids paying browser cost and switching provenance merely because another extraction path produced a slightly different confidence score while failing to improve semantic completeness.

## Recovery result selection

### Rendered result is better

If rendered extraction has a strictly better `ExtractionQuality`, the returned `URLExtractionResult` uses:

- rendered asset;
- rendered extraction decisions;
- combined static + rendered acquisition attempts;
- combined acquisition warnings;
- `semantic_render_recovery_selected:rendered` marker.

A rendered result that converts a required field from `unresolved` to `needs_confirmation` is considered an improvement even though human review is still required.

### Rendered result is not better

If quality is equal or worse, the static asset/extraction remains authoritative while the returned acquisition trace still records that rendered recovery was attempted.

The result receives:

`semantic_render_recovery_selected:static`

This preserves auditability without silently discarding the original successful extraction.

## Recovery failure

Browser recovery is best-effort after a successful static extraction.

If the rendered acquisition or rendered extraction raises:

- the successful static extraction is retained;
- the pipeline does not convert the item into an acquisition failure;
- a synthetic failed rendered `AcquisitionAttempt` is appended;
- warnings record only the exception type, not the exception text/URL.

This avoids leaking URL/query details through durable warning text.

The failure marker is:

`semantic_render_recovery_failed:<ExceptionType>`

## Audit trail

A recovery attempt adds a trigger marker listing only required field keys:

`semantic_render_recovery_triggered:price,title`

Candidate values are never copied into the recovery marker.

When rendered acquisition succeeds, both the original and recovery attempts remain in `AcquisitionResult.attempts` regardless of which extraction is selected.

This trace is already compatible with `DP-ENGINE-008` durable result persistence because acquisition attempts/warnings are part of the existing result codec.

No result schema version change is required.

## Batch/job integration

No `DP-ENGINE-007` API changes are required.

`JobExecutor` already calls `URLExtractionPipeline.extract()` with each item's acquisition request. Therefore batch jobs automatically gain semantic recovery under the default `AUTO` mode.

Execution retry policy remains independent:

- transient HTTP/browser acquisition failure of the primary path is still execution retry territory;
- failure of the optional semantic rendered recovery after successful static extraction is not a job failure.

## Durable review integration

If recovery produces a rendered `needs_confirmation` result:

- `DP-ENGINE-008` persists those rendered candidates/evidence normally;
- `DP-ENGINE-009` queues the review normally;
- reviewer confirmation still selects only existing candidate ids;
- no page reacquisition is needed during review.

## Human participation rule

The operator/customer does not:

- inspect DOM/CSS/XPath;
- decide whether a specific site needs Playwright;
- retry a page manually because required values were absent from static HTML;
- compare static/rendered candidates manually;
- configure browser mode per site in the normal workflow.

The only human path remains governed candidate confirmation when automation cannot safely resolve ambiguity.

## Explicit non-goals

`DP-ENGINE-010` does **not** implement:

- anti-bot bypass;
- CAPTCHA solving;
- browser pools/concurrency scaling;
- site-specific render rules;
- OCR/VLM recovery;
- network interception/API discovery;
- multiple browser retries;
- arbitrary confidence-based browser escalation;
- manual value correction;
- distributed browser workers.

## Acceptance evidence

Repository regressions cover 15 scenarios:

- required unresolved static -> rendered ready result;
- ready static -> no render;
- optional unresolved only -> no render;
- `NEVER` blocks semantic recovery;
- `ALWAYS` does not double-render;
- already-rendered `AUTO` result does not render again;
- browser failure retains static and records safe failure evidence;
- rendered review beats required unresolved;
- equal governed quality retains static;
- optional semantic improvement can select rendered when required state ties;
- disabled policy preserves pre-010 behavior;
- quality ranks required review above required unresolved;
- confidence-only difference does not select rendered;
- warnings from both acquisition paths are preserved;
- trigger warnings contain field keys, not business values.

Container checkout of GitHub remains unavailable because `github.com` DNS does not resolve in the execution environment, so the repository pytest suite was not run directly in this pass.

A reconstructed targeted policy harness covering trigger and quality semantics passed **12/12 assertions**. Source/test modules were also syntax-compiled before repository write.
