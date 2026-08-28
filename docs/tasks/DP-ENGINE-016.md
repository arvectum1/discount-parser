# DP-ENGINE-016 — Source parity migration / generic multi-record production adoption

## Goal

Move Discount Parser production decoding from site adapter `parse(html)` implementations toward the generic DP-014 multi-record engine without reducing customer data quality or adding manual site configuration.

## Production migration contract

For every already-acquired selected page in hybrid runtime:

1. discover generic record boundaries with a domain-neutral semantic HTML provider;
2. interpret each record with Discount Parser domain field semantics;
3. parse the same in-memory HTML through the existing adapter as a temporary migration oracle;
4. compare exact record identities and all legacy-populated business fields;
5. return generic records only when the generic result is a proven safe superset;
6. otherwise return the legacy records automatically and emit bounded diagnostic provenance.

The parity oracle does **not** perform another network request.

## Safe-superset definition

Generic output is safe to adopt only when:

- generic record count equals legacy record count;
- record `external_id` sets are identical and contain no duplicates;
- for every shared record, every business field populated by legacy is equal after deterministic normalization;
- generic records are all `READY` (no boundary/field confirmation and no unresolved required field).

A generic value may enrich a field that legacy left null. This is allowed because the migration gate is lossless rather than artificially information-poor.

## Domain-neutral HTML record discovery

`SemanticHTMLRecordProvider` may use only generic HTML structure and promotion-like semantic signals. It contains:

- no host/domain names;
- no CSS selectors supplied by a source;
- no XPath;
- no source-specific class names;
- no operator configuration.

It recognizes bounded article/list/card-like containers and offer-like links, suppresses nested action duplicates, creates structural deterministic record IDs, and exposes generic text/link/image/data attributes to candidate providers.

## Discount Parser domain bridge

`DiscountOfferCandidateProvider` converts a generic record slice into existing `RawOffer` semantics using promotion-domain rules (title, merchant, link, offer ID, promo code, discount/cashback, image, validity, conditions). It is allowed to know the Discount Parser business schema but not individual source hosts or selectors.

## Production provenance

Each returned offer records:

- `dp_engine.runtime = hybrid`;
- `dp_engine.decoder = generic_multi_record` or `legacy_adapter`;
- target-page relevance provenance;
- page-level parity counts when comparison was performed;
- generic `record_id`/provider/source-ref when the generic decoder was adopted.

`SourceCollectionResult` also exposes aggregate `generic_pages`, `legacy_pages`, and `parity_failures` counters.

## Safety behavior

Automatic legacy fallback is required when:

- generic decoding raises;
- no generic records are found;
- any generic record is review-required or incomplete;
- parity changes record identity/count;
- any legacy-populated business field differs.

No customer/operator switch is added.

## Regression gate

The versioned five-adapter fixture corpus must prove safe-superset parity for every adapter fixture before merge. Additional regressions cover structural boundary discovery, stable record IDs, bounded record counts, generic production adoption, parity mismatch fallback, and single-fetch behavior.

Full repository CI and delivery builds remain mandatory before merge.
