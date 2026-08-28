# DP-ENGINE-012 — Discovered URL relevance / target-page classification

**Status:** implemented

## Goal

Turn the bounded URL set produced by `DP-ENGINE-011` into a smaller, ranked set of pages that are actually useful to Discount Parser.

For the current product, a **target page** is not necessarily a single-product or single-offer detail page. The important case is a page that contains useful discount business content: promo codes, discounts, coupons, cashback, campaigns or offer blocks. A merchant/brand page that contains many offers is therefore a valid target.

Examples include pages such as:

- merchant promo pages (`/o/<merchant>` on Promokood-like layouts);
- store/brand promo pages;
- aggregator/category/root pages when they actually contain active offer blocks;
- ordinary slugs whose page content clearly contains discount offers even when the URL itself has no promo marker.

The classifier deliberately avoids an e-commerce-only "product detail page" model because that would reject real Discount Parser source pages.

## Repository ownership

`DP-ENGINE-012` is implemented in the current product-development canonical repository:

`arvectum1/discount-parser`

It builds on the reconciled `DP-ENGINE-001..011` baseline imported from the earlier Data Platform development line. Reusable parts can be promoted/reconciled back to `data-platform` later; this task is optimized first for shipping Discount Parser.

## Input / output boundary

Input:

- `CrawlDiscoveryResult` from `DP-ENGINE-011`;
- optional request headers;
- a bounded `TargetPagePolicy`.

Output:

- `TargetPageDiscoveryResult`;
- one `TargetPageAssessment` per unique seed/discovered URL;
- deterministic ranking;
- direct `to_job()` handoff into the existing `ExtractionJob` runtime.

The classifier does not modify the existing extraction, execution, durable result or review layers.

## Classification states

`TargetPageStatus` has four states:

- `target` — strong evidence that the page contains useful Discount Parser offer content;
- `candidate` — plausible target that should remain in the automatic processing set to reduce false negatives;
- `non_target` — explicit hard-negative or insufficient relevance after successful probing;
- `unprobed` — URL was not content-probed because a configured bound was reached or probing was deliberately disabled.

This separation is intentional. A network/probe limitation must not be confused with a semantic negative decision.

## Why candidates are included by default

`TargetPageDiscoveryResult.to_job()` includes `target + candidate` by default.

This follows the current customer-delivery priority:

- false negatives are expensive because a real discount page can disappear from the pipeline completely;
- an extra candidate page is comparatively cheap because the existing governed extraction/job/review stack can reject or leave it incomplete later.

A strict caller can use `include_candidates=False` when only high-confidence target pages should enter the extraction job.

## Evidence model

Every assessment carries additive `RelevanceEvidence` records with:

- evidence source;
- signal name;
- bounded numeric weight;
- small structural/semantic detail.

The score is explainable and deterministic. There is no opaque ML model in this baseline.

Signals are intentionally split between cheap link evidence and bounded page-content evidence.

## Cheap URL/link signals

Before any additional page probe, the classifier inspects:

- canonical URL path segments;
- query keys;
- discovery anchor text;
- first-discovery parent/depth provenance inherited from the crawl result.

### Strong discount path markers

Generic discount markers such as:

- `promo`, `promocode`, `promokod`;
- `coupon`;
- `discount`;
- `deal`;
- `offer`;
- `sale`;
- `akcii` / `aktsii`;
- `skidki`;

add positive evidence.

### Merchant/store markers

Segments such as:

- `o`;
- `shop`;
- `store`;
- `merchant`;
- `brand`;

are weaker positive signals. They are not sufficient by themselves to assert `target`; they mainly prioritize probing.

The `o` marker is useful for the real Promokood merchant-page pattern without introducing a full site-specific selector adapter.

### Hard-negative paths

Clearly non-business-content destinations are rejected without spending a probe request, including common account/legal/utility segments such as:

- login/auth/register/account/profile;
- cart/basket/checkout;
- privacy/policy/terms/agreement;
- contacts/about;
- blog/news/search;
- faq/help/support;
- sitemap/api.

The match is based on path segments, not arbitrary substring matching, so unrelated slugs are not rejected merely because they contain a similar character sequence.

### Navigation/pagination penalty

Directory/navigation markers such as category/catalog/shops/brands/page and pagination/view query keys lower the score but are not hard rejects.

This is important for Discount Parser because some category/root pages still contain real active offers and must be allowed to recover through content evidence.

## Content probing

By default the classifier performs a bounded page probe using the existing `AcquisitionEngine`.

It does not create a second HTTP/browser implementation.

Default probe controls:

- `max_probe_pages = 100`;
- `max_selected_urls = 100`;
- `render_mode = AUTO`;
- `timeout_s = 15`;
- `max_bytes = 1_000_000` per page.

These limits are separate from the crawl bounds in `DP-ENGINE-011`.

## Page content signals

The probe extracts only lightweight relevance signals from the acquired `RawAsset`:

- `<title>`;
- first/combined `<h1>` content;
- description / `og:description` meta;
- bounded visible text.

`script`, `style`, `noscript` and `svg` content is ignored for visible-text scoring so embedded JavaScript/CSS vocabulary cannot make a page look like a discount page.

When the asset is text-only, the same bounded semantic vocabulary is applied to `RawAsset.text`.

## Discount vocabulary

The default current-product vocabulary includes Russian and English promo concepts such as:

- промокод;
- скидка;
- купон;
- акция;
- предложение;
- кэшбэк / кешбэк;
- promo code / promocode;
- coupon;
- discount;
- deal;
- cashback.

CTA evidence includes phrases such as:

- активировать/показать/получить/скопировать промокод;
- открыть акцию;
- получить скидку;
- show/get/copy code.

The vocabulary is exposed through `TargetPagePolicy` and can be extended without CSS/XPath rules.

## Content score behavior

Strong title/H1/meta discount semantics add evidence.

Visible text contributes through both:

- semantic diversity — several distinct discount concepts;
- bounded density — repeated discount language that is typical of pages containing multiple live offer blocks.

A page with one incidental use of the word "скидка" does not automatically become a target.

A merchant page with repeated promo/discount/coupon/CTA content normally crosses the target threshold even if its slug is otherwise generic.

## Generic title handling

Exact generic titles such as "Главная", "Магазины", "Категории" and "Поиск" receive a negative weight.

This is a penalty rather than a hard rejection. If such a page genuinely contains many live offers, strong content evidence can still outweigh the generic title.

That behavior matches real Discount Parser sources, where aggregator/root/category pages may themselves be useful extraction targets.

## Probe failure semantics

One failed relevance probe does not abort the classification run.

For a non-hard-negative URL:

- the existing cheap URL/anchor score is retained;
- the URL remains at least `candidate` instead of being converted to a false `non_target`;
- exception type and bounded error summary are recorded;
- HTTP(S) URLs inside exception text are replaced with `<url>` so query tokens/credentials are not duplicated into logs/evidence.

This is intentionally recall-biased for the current customer-delivery phase.

## Probe bounds

If the crawl produced more probe-eligible URLs than `max_probe_pages`:

- higher preliminary relevance is probed first;
- discovery order is the deterministic tie-breaker;
- remaining pages are marked `unprobed`;
- `limit_reasons` contains `max_probe_pages`.

If more target/candidate URLs exist than `max_selected_urls`:

- assessment statuses are preserved;
- `urls()` / `to_job()` return only the highest-ranked bounded subset;
- `limit_reasons` contains `max_selected_urls`.

The classifier never silently rewrites a proven target to `unprobed` merely to enforce the output cap.

## Ranking

`ranked()` orders pages by:

1. target;
2. candidate;
3. unprobed;
4. non-target;
5. higher score within the same status;
6. original deterministic discovery index as final tie-break.

No candidate business values are used for relevance ranking.

## Seeds

Explicit seeds are classified by default (`include_seeds=True`).

This is required for Discount Parser because configured source roots such as aggregator or merchant pages may already contain offers and should not disappear merely because `DP-ENGINE-011` treats them as crawl origins rather than discovered links.

A URL present both as a seed and a discovered link is assessed/probed once.

## Extraction-job bridge

Example:

```python
from arvectum_data import (
    FieldSpec,
    TargetPageClassifier,
    URLDiscoveryCrawler,
)

crawl = URLDiscoveryCrawler().discover([
    "https://example.test/",
])

targets = TargetPageClassifier().classify(crawl)

job = targets.to_job(
    "discount-targets",
    [
        FieldSpec("title", required=True),
        FieldSpec("promo_code", required=True),
    ],
)
```

The resulting `ExtractionJob` continues through the existing retry/checkpoint/result/review stack. `DP-ENGINE-012` does not introduce another execution path.

## Reuse from Doors Parser

Doors Parser was reviewed before implementation.

Useful ideas there included:

- negative URL families;
- positive catalog/detail URL markers;
- generic-page rejection;
- additive "productish" content evidence.

Those ideas were adapted, not copied as a site-specific architecture. Door-specific vocabulary, adapter selectors, line matching and per-site URL maps were intentionally not imported into Discount Parser.

## Human participation rule

The normal customer/operator flow remains:

1. provide/approve a source seed;
2. system discovers URLs automatically;
3. system ranks/probes target pages automatically;
4. system extracts fields automatically;
5. user only reviews governed ambiguous field candidates when necessary.

The customer does not:

- classify every discovered URL manually;
- maintain skip lists per site;
- write CSS/XPath selectors;
- inspect DOM nodes;
- choose probe/browser mode per page;
- tune page scores in the normal UX.

## Explicit non-goals

`DP-ENGINE-012` does **not** yet implement:

- learned relevance weights from reviewer history;
- per-source supervised page-type models;
- sitemap ingestion;
- durable crawl-frontier resume;
- robots.txt evaluation;
- per-host pacing/rate limiting;
- authentication/login flows;
- anti-bot/CAPTCHA bypass;
- distributed crawling;
- automatic source onboarding UI;
- page-template selector learning;
- promotion/reconciliation back to `data-platform`.

## Acceptance evidence

The committed targeted regression suite contains **20 scenarios**, including:

- threshold validation;
- real Promokood `/o/<merchant>` pattern;
- content-only target with no URL marker;
- aggregator seed classification;
- hard-negative account path without probe;
- navigation/pagination penalties;
- anchor evidence;
- explicit unprobed state when probing is disabled;
- deterministic probe cap;
- probe failure isolation and URL redaction;
- script/style noise suppression;
- meta-description evidence;
- text-only asset classification;
- generic-title penalty;
- deterministic target/candidate/non-target ranking;
- selected URL cap without status corruption;
- default `target + candidate` job bridge;
- strict target-only job bridge;
- seed/discovered dedup;
- propagation of bounded transport controls and request headers.

A reconstructed harness using the same public contracts passed **20/20** before repository PR creation.
