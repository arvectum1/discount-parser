# DP-ENGINE-013 — Production source/runtime integration

**Status:** implemented

## Goal

Connect the reconciled DP Engine to the actual Discount Parser source runtime without replacing proven customer-facing offer persistence or forcing a premature universal multi-record extractor.

`DP-ENGINE-013` moves the five shipped promo-code sources from the old direct `adapter.collect()` entry path to a bounded hybrid path:

```text
SourceConfig
  ↓
Discount Parser network router
  ↓
DP-ENGINE-011 bounded crawl
  ↓
DP-ENGINE-012 target relevance
  ↓
DP acquisition/cache
  ↓
existing adapter.parse(html) multi-offer decoder
  ↓
existing RawOffer → normalize/dedup/classify/persist pipeline
```

The old adapter `collect()` method remains an automatic safety fallback only.

## Why the integration is hybrid

The generic DP extraction contract implemented in `DP-ENGINE-001..010` resolves one semantic decision per `FieldSpec` for one acquired asset.

A Discount Parser merchant page commonly contains many independent offers. For example, one `/o/<merchant>` page may contain several promo codes and promotions that must become separate `RawOffer` rows.

Forcing the current single-value generic extractor to replace the existing multi-offer adapters would lose records and make the product worse. Therefore `DP-ENGINE-013` deliberately keeps the tested `parse(html)` methods as bounded record decoders while DP Engine takes ownership of discovery, relevance and acquisition.

A future task may add a first-class generic multi-record extraction/review contract and then retire these decoders incrementally.

## Runtime ownership

New module:

`src/sources/engine_runtime.py`

Main components:

- `DiscountParserHTTPTransport`
- `ProductionSourcePolicy`
- `SourceCollectionResult`
- `ProductionSourceRuntime`
- `collect_source_offers()`

`src/sources/runner.py` now calls `collect_source_offers()` before the existing persistence path.

No Offer DB schema or persistence contract changes are required.

## Governed product HTTP transport

Generic `arvectum_data` defaults to its own urllib transport. That is not sufficient for the customer product because Discount Parser already owns network routing for:

- `auto`;
- `direct`;
- configured proxy;
- system/Windows WinINet proxy;
- route fallback under the existing network router.

`DiscountParserHTTPTransport` adapts the existing `network_router` to the DP Engine `HTTPTransport` protocol.

It preserves:

- source `network_policy`;
- browser-like request headers;
- timeout bounds;
- response-size bounds;
- redirects;
- 403/451 route retry signals in `auto` mode.

This means DP Engine does not create a second incompatible networking stack inside the installed product.

## Per-run acquisition cache

Crawl, target probing and decoding can request the same page at different stages.

The production transport therefore caches successful page snapshots only for the lifetime of one source run.

The cache:

- is process/run local;
- is not persisted;
- does not outlive the source run;
- does not store credentials separately;
- prevents repeated downloads of the same URL/header combination.

## Conservative production bounds

`ProductionSourcePolicy` defaults:

- crawl pages: 16;
- crawl depth: 1;
- discovered URLs: 300;
- links parsed per page: 250;
- relevance probes: 40;
- selected target/candidate URLs: 40;
- timeout: 20 seconds;
- response size: 5 MB.

The runtime remains bounded even when a source has a very large navigation graph.

## Existing Discount Parser discovery reuse

The pre-existing `source_registry.known_site_crawl` implementation contains a useful Promokood mechanism that discovers `/o/...` pages even when the live UI exposes them through JS/data attributes instead of ordinary anchors.

`DP-ENGINE-013` reuses this mechanism as a supplemental same-host discovery source.

It does not import or require manual CSS selectors.

The existing CSS extraction-profile subsystem remains available for legacy compatibility but is not introduced into the new normal customer path.

## Multi-offer decoding

For every selected `target` or `candidate` page:

1. DP acquisition obtains the page through the shared production transport/cache;
2. a normal shipped source adapter is instantiated with the selected page as its effective `base_url`;
3. the adapter's existing `parse(html)` method decodes zero or more `RawOffer` values;
4. one page decoder failure is isolated from other selected pages;
5. duplicate `(source_key, external_id)` records produced by multiple pages are removed before persistence.

Using the selected page as effective `base_url` is important because existing adapters resolve relative offer/image/action URLs against `self.base_url`.

## Safety fallback

The production runtime does not strand the customer when the new discovery layer encounters an unknown site shape or network failure.

Automatic fallback occurs when:

- the engine path throws before producing offers; or
- the engine path completes but produces zero offers.

Fallback is the existing:

```python
build_adapter(config).collect()
```

No operator action or browser toggle is required.

A successful hybrid run does **not** additionally call legacy `collect()`, avoiding duplicate network work.

## Runtime modes

`SourceConfig` gains:

- `runtime_mode = "legacy"` by default;
- supported values: `legacy`, `hybrid`.

The default remains `legacy` for backward-compatible programmatic/test configs.

The five shipped entries in `config/sources.yaml` explicitly use `hybrid`:

- promokood;
- promokodik;
- berikod;
- promokodi_net_ru;
- promko.

This makes the production product use DP Engine while old tests/custom internal configs do not silently change behavior.

`runtime_mode` is an internal product configuration boundary, not a new normal customer-facing tuning control.

## Existing persistence remains authoritative

After collection, the established product pipeline is unchanged:

- geo filter;
- normalization;
- same-source external-id update;
- cross-source deduplication;
- category/subcategory classification;
- `Offer` persistence;
- `OfferSourceObservation` provenance;
- existing `ready` / `needs_review` behavior;
- ParseRun accounting.

This preserves the already accepted customer delivery behavior.

## DP Engine provenance on offers

Every offer decoded through the hybrid path receives bounded metadata under:

`raw_payload["dp_engine"]`

It records:

- runtime mode;
- selected page URL;
- relevance status;
- relevance score;
- whether the page was probed;
- up to 12 explainable relevance evidence entries.

This metadata flows through the existing `OfferSourceObservation` durable raw payload.

Business values are not duplicated merely for audit purposes.

## Result/review boundary

`DP-ENGINE-008/009` durable result and governed review remain valid for generic one-record extraction jobs.

They are **not falsely claimed to govern multi-offer merchant-page decoding in DP-013** because the current result schema does not represent an arbitrary list of independently reviewable offer records from one page.

For the shipped Discount Parser runtime, the established durable `Offer` / `needs_review` path remains authoritative until a multi-record DP result contract is implemented.

## Observability

`RunResult` now also exposes non-breaking fields with defaults:

- `runtime_mode`;
- `engine_discovered_urls`;
- `engine_selected_urls`;
- `engine_decoded_pages`;
- `engine_fallback_used`;
- `runtime_warnings`.

Runtime warnings do not turn an otherwise successful persisted run into a ParseRun error.

## Failure/privacy behavior

- one target-page decoder failure does not abort other pages;
- engine-wide failure automatically falls back;
- URL-bearing exception diagnostics redact HTTP(S) URLs;
- diagnostics are bounded to 1000 characters;
- raw page bodies are not copied into runtime warnings;
- no selector, XPath or manual field-value path is introduced.

## Explicit non-goals

`DP-ENGINE-013` does not yet implement:

- a universal multi-offer extraction result type;
- DP durable review for each offer emitted from one merchant page;
- removal of all five source decoders;
- browser/Playwright packaging in the customer installer;
- robots/rate-limit policy beyond existing bounded crawl controls;
- authentication/CAPTCHA/anti-bot bypass;
- customer-facing runtime-mode or crawler tuning controls;
- manual CSS/XPath configuration in the normal path.

## Acceptance tests

The committed DP-013 regression covers:

- Discount Parser router adaptation;
- source network-policy preservation;
- per-run acquisition cache;
- legacy-mode compatibility;
- hybrid discovery/relevance/decoding;
- selected-page base URL propagation;
- reuse of Promokood JS/data-attribute discovery;
- multi-page offer collection;
- duplicate external-id suppression;
- per-page decoder failure isolation;
- engine-failure fallback;
- zero-offer fallback;
- hard-negative pages excluded from decoding;
- bounded DP provenance on `RawOffer`;
- safe config fallback for unknown runtime modes;
- all five shipped sources explicitly enabled for hybrid runtime.

Full Discount Parser CI and delivery-build evidence is evaluated on the pull request before merge.
