# DP-ENGINE-011 — Bounded URL discovery / crawl frontier

**Status:** implemented

## Goal

Add a domain-neutral discovery layer that can start from one or more listing/seed URLs, find eligible linked HTTP(S) pages, bound crawl expansion deterministically, and hand the resulting URL set to the existing `ExtractionJob` runtime.

This closes the gap between "the caller already knows every page URL" and the existing acquisition/extraction/job layers.

## Architecture boundary

`DP-ENGINE-011` is intentionally a URL-discovery layer, not a second extraction engine.

- `AcquisitionEngine` still fetches/render pages.
- `URLDiscoveryCrawler` owns link discovery and bounded BFS traversal.
- `CrawlDiscoveryResult` stores URL/provenance/frontier outcome.
- `CrawlDiscoveryResult.to_job()` hands discovered URLs to the existing `ExtractionJob` contract.
- field extraction, semantic recovery, durable results and review remain unchanged.

## Default crawl policy

`CrawlPolicy` defaults:

- `max_pages = 50`;
- `max_depth = 1`;
- `max_discovered_urls = 500`;
- `max_links_per_page = 250`;
- `same_origin = True`;
- `respect_nofollow = True`;
- `render_mode = AUTO`;
- `timeout_s = 20`;
- `max_bytes = 2_000_000` per page.

These bounds prevent an ordinary listing-page discovery request from expanding into an unbounded spider.

## Frontier semantics

Traversal is deterministic breadth-first search.

The frontier tracks:

- canonical URL;
- depth;
- whether the URL is already queued/visited;
- first discovery parent;
- anchor text;
- `rel` tokens.

A URL is discovered once. Fragment-only variants and default-port/host-case variants collapse to the same canonical URL.

Query strings are deliberately preserved rather than sorted or stripped, because query ordering/values can be semantically significant for target pages.

## Generic link discovery

The crawler uses standard HTML semantics only:

- `<a href>`;
- anchor text;
- `rel`;
- first `<base href>` when present.

No CSS selectors, XPath, line numbers, per-site rules or customer DOM configuration are introduced.

`<base href>` is resolved according to HTML URL semantics before scope filtering. An external base therefore cannot cause a relative URL to be incorrectly reinterpreted as an internal URL.

## URL canonicalization

`canonicalize_url()`:

- resolves relative links;
- accepts HTTP(S) only;
- rejects embedded URL credentials;
- lowercases scheme/host;
- removes default `:80` / `:443` ports;
- normalizes an empty path to `/`;
- removes fragments;
- preserves path/query spelling otherwise.

## Scope containment

Default behavior is same-origin relative to the supplied seed origins.

For multiple seeds, every explicit seed origin is in scope.

An off-origin redirect is recorded in page provenance but is not expanded under the default policy.

Cross-origin crawl must be explicitly enabled with:

- `same_origin=False`; and
- a non-empty `allowed_hosts` allow-list.

A configuration that disables same-origin containment without an allow-list is rejected.

This prevents a discovered external link from accidentally turning a bounded site crawl into arbitrary internet traversal.

## Link filtering

By default the crawler skips:

- unsupported schemes such as `mailto:`, `javascript:` and `data:`;
- URL-embedded credentials;
- `rel=nofollow` links;
- common static/binary/document suffixes such as images, media, archives, scripts, stylesheets, office documents and PDF/XML/JSON feeds.

The suffix filter is generic and configurable through `CrawlPolicy.blocked_suffixes`.

## Bounds

Three independent growth controls are enforced:

1. `max_pages` — maximum acquisition attempts/pages popped from the frontier;
2. `max_discovered_urls` — maximum new unique linked URLs admitted;
3. `max_links_per_page` — maximum anchor records parsed from one HTML page.

`max_depth` bounds traversal depth.

When a global limit stops work, `CrawlDiscoveryResult.truncated` becomes true and `limit_reasons` records the limiting control.

## Failure isolation

One page acquisition failure does not abort the discovery run.

`CrawlFailure` records:

- requested URL;
- depth;
- exception type;
- bounded error summary.

HTTP(S) URLs embedded inside exception text are redacted to `<url>` so accidental tokens/query strings from lower-level exception messages are not duplicated into error summaries.

## Rendered listing pages

Discovery reuses the normal `AcquisitionEngine` and therefore can consume its existing `AUTO` rendered-page fallback.

`CrawlPageRecord.rendered` records whether browser rendering was actually used.

The crawler does not implement a second browser decision system.

## Extraction-job bridge

Example:

```python
from arvectum_data import CrawlPolicy, FieldSpec, URLDiscoveryCrawler

crawler = URLDiscoveryCrawler(
    policy=CrawlPolicy(max_pages=25, max_depth=1),
)

discovery = crawler.discover([
    "https://example.test/catalog",
])

job = discovery.to_job(
    "catalog-2026-08-28",
    [
        FieldSpec("title", required=True),
        FieldSpec("price", required=True),
    ],
)
```

The resulting job uses the already implemented retry/checkpoint/result/review stack from `DP-ENGINE-007..009`.

## Human participation rule

Normal customer/operator participation remains minimal.

The operator supplies a seed/listing URL and the desired semantic `FieldSpec` contract. The operator does not:

- enumerate every detail URL manually;
- inspect DOM nodes;
- configure CSS/XPath selectors;
- deduplicate fragment variants;
- decide which discovered link should be browser-fetched;
- manage crawl queues by hand.

## Explicit non-goals

`DP-ENGINE-011` does **not** yet implement:

- relevance/ranking classification of discovered URLs as product/offer/detail pages;
- sitemap ingestion;
- durable crawl-frontier persistence/resume;
- per-host request pacing/rate limiting;
- robots.txt policy evaluation;
- distributed/cross-node crawling;
- authentication/session/login flows;
- anti-bot bypass;
- CAPTCHA bypass;
- recursive API/JS bundle reverse engineering;
- per-site selectors.

These belong to later bounded adapters/policy layers.

## Acceptance evidence

The committed regression suite covers 19 scenarios including:

- canonicalization and fragment/default-port dedup;
- non-HTTP and credential rejection;
- anchor/base/rel parsing;
- same-origin containment;
- query variants remaining distinct;
- nofollow/static-asset filtering;
- external `<base>` semantics;
- deterministic BFS/depth control;
- `max_pages`;
- `max_discovered_urls`;
- `max_links_per_page`;
- failure isolation and URL redaction;
- off-origin redirects;
- mandatory cross-origin allow-list;
- explicit allowed-host expansion;
- rendered-page provenance;
- direct `ExtractionJob` bridge;
- multiple explicit seed origins.

A reconstructed targeted harness using the repository contracts passed **19/19** after the external-`<base>` edge case was corrected.

The full repository pytest suite was not run in this pass because the execution container does not have a usable GitHub checkout/network path.
