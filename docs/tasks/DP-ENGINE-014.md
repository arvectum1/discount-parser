# DP-ENGINE-014 — Generic multi-record / offer-set extraction contract

**Status:** implemented

## Goal

Remove the single-record limitation identified during `DP-ENGINE-013` without breaking the shipped Discount Parser production path.

Before this task, `ExtractionEngine` resolved one `FieldDecision` per semantic field across an entire `RawAsset`. On a merchant page containing several offers, values from different offers therefore became competing candidates for the same field.

`DP-ENGINE-014` introduces an explicit record-boundary layer before field resolution:

```text
RawAsset (page / feed / structured source)
  ↓
RecordProvider(s)
  ↓
RecordBoundary[]
  ↓
ExtractionEngine independently per boundary
  ↓
RecordExtractionResult[]
  ↓
RecordSetResult
```

The existing single-record `ExtractionEngine` remains unchanged and is reused inside each record.

## Core contract

New module:

`arvectum_data/engine/records.py`

### `RecordProvider`

A record provider proposes structural record slices only. It does not select business field values.

```python
class RecordProvider(Protocol):
    name: str

    def records(
        self,
        asset: RawAsset,
        fields: Sequence[FieldSpec],
    ) -> RecordProviderResult: ...
```

This keeps record segmentation independent from candidate discovery and field resolution.

### `RecordBoundary`

Each proposed record contains:

- deterministic `record_id`;
- isolated child `RawAsset`;
- provider name;
- structural `source_ref`;
- source order (`ordinal`);
- boundary confidence;
- boundary evidence;
- bounded metadata.

`record_id` is derived from structural provenance:

`parent_asset_id + provider + source_ref`

It deliberately does not hash business values. A change to a promo-code value does not by itself create a different record identity when the structural location is unchanged.

### `RecordProviderResult`

Record providers return:

- `records`;
- `warnings`.

This allows bounded providers to surface truncation or malformed structured blocks without turning an otherwise usable page into a total failure.

### `MultiRecordExtractionEngine`

The engine:

1. runs record providers with provider-level failure isolation;
2. validates provider ownership and unique record IDs;
3. orders boundaries deterministically;
4. runs the existing `ExtractionEngine` independently for each record asset;
5. returns one `RecordExtractionResult` per record;
6. aggregates them into `RecordSetResult`.

Candidate-provider failures remain isolated inside the existing per-record `ExtractionResult`.

## Record governance

A record boundary is itself an engine proposal.

### `RecordBoundaryStatus`

- `auto_selected`
- `needs_confirmation`
- `confirmed`
- `rejected`

`MultiRecordExtractionEngine` uses `min_boundary_confidence` (default `0.80`).

A boundary below the threshold is retained with all extracted candidate/evidence context but is not exported as accepted data until a reviewer confirms it.

Review can only:

- confirm an existing engine-proposed boundary;
- reject an existing engine-proposed boundary.

There is no core API to manually insert a record.

Field review is similarly delegated to the existing `ExtractionEngine.confirm()` contract: a reviewer can select only an existing candidate ID or reject the field. There is still no manual-value path.

### `RecordStatus`

Each record exposes an operational status derived from boundary and field state:

- `ready`
- `needs_confirmation`
- `incomplete`
- `rejected`

Priority is:

1. rejected boundary → `rejected`;
2. boundary or field requires review → `needs_confirmation`;
3. unresolved/rejected required field → `incomplete`;
4. otherwise → `ready`.

This lets future persistence/review workers queue records independently without duplicating field-level truth.

## `RecordSetResult`

Provides:

- `records`;
- `record_provider_errors`;
- `record_provider_warnings`;
- `requires_confirmation`;
- `review_record_ids`;
- `ready_record_ids`;
- `incomplete_record_ids`;
- `rejected_record_ids`;
- `record(record_id)` lookup;
- record-scoped `values()`.

Rejected boundaries are retained as evidence but omitted from exported values.

Low-confidence boundaries are also omitted from normal values until confirmed; `include_unconfirmed=True` is an explicit diagnostic/review view only.

## Built-in generic record providers

### `AttributeRecordProvider`

Supports already-structured sources where `RawAsset.attributes` contains an explicit sequence such as:

```python
{
    "records": [
        {"title": "Offer A", "code": "A10"},
        {"title": "Offer B", "code": "B20"},
    ]
}
```

The attribute key is configurable at schema/integration level. No page selector is involved.

Properties:

- deterministic record IDs;
- preserved source order;
- configurable `max_records` (default 200);
- explicit truncation warning;
- strict sequence-of-mappings validation.

This path is suitable for APIs, feeds and adapters that already expose structured record arrays.

### `JSONLDRecordProvider`

Discovers independent JSON-LD objects automatically.

It:

- parses `application/ld+json` blocks;
- recursively visits JSON objects in document order;
- qualifies a record using **direct scalar semantic fields** only;
- matches `FieldSpec.key` and aliases;
- does not treat an `ItemList` parent as a record merely because its children contain offer fields;
- produces a record-scoped synthetic JSON-LD child asset;
- preserves the JSON path as structural provenance;
- isolates malformed JSON-LD blocks as warnings;
- bounds output with `max_records` (default 200).

The default `min_matched_fields=2`, reduced automatically when the requested schema contains fewer fields.

No CSS/XPath or site-specific DOM coordinates are used.

## Why direct-field JSON-LD matching matters

A page may contain:

```json
{
  "@type": "ItemList",
  "itemListElement": [
    {"@type": "Offer", "name": "A", "code": "A10"},
    {"@type": "Offer", "name": "B", "code": "B20"}
  ]
}
```

The old whole-page extractor sees two `name` and two `code` values and must treat them as conflicts.

The new record provider emits two boundaries. Each boundary is then resolved independently:

```text
record 0 → name=A, code=A10
record 1 → name=B, code=B20
```

No cross-record candidate competition occurs.

## Provider isolation and integrity

The multi-record engine enforces:

- unique record-provider names;
- `RecordProviderResult` return type;
- boundary provider must match the producer name;
- unique `record_id` across the result;
- deterministic ordering by `(ordinal, provider, record_id)`;
- one broken record provider does not suppress records from another provider;
- duplicate or spoofed boundaries from one provider are isolated as that provider's error.

## Relationship to DP-ENGINE-013

`DP-ENGINE-013` remains intentionally unchanged in this task.

The shipped production path is still:

```text
DP crawl/relevance/acquisition
  ↓
existing adapter.parse(html) multi-offer decoder
  ↓
RawOffer persistence
```

`DP-ENGINE-014` creates the generic contract needed to replace those bounded source decoders incrementally, but it does **not** switch the five customer sources before parity evidence exists.

This preserves the agreed strategy: finish and protect the standalone Discount Parser instead of forcing a universal rewrite into customer production.

## Persistence boundary

This task defines extraction/review state in memory but does not silently change the existing `ResultCodec`, `ResultStore`, `GovernedReviewQueue` or production `Offer` schema.

Current DP-008/009 durable result/review contracts are single-URL/single-extraction contracts. Persisting `RecordSetResult` as independently reviewable durable records requires an explicit schema/versioning task rather than overloading the old payload shape.

That separation is intentional.

## Tests

`tests/dp_engine/test_multi_record_extraction.py` covers:

- independent structured record resolution;
- JSON-LD ItemList segmentation;
- parent-container exclusion;
- malformed JSON-LD isolation;
- bounded JSON-LD and attribute sources;
- deterministic source order and record IDs;
- low-confidence boundary review;
- boundary accept/reject governance;
- rejected record evidence retention;
- per-record incomplete state;
- field review scoped to one record;
- cross-record candidate rejection;
- record-provider failure isolation;
- provider spoof protection;
- duplicate record-ID protection;
- duplicate provider-name rejection;
- duplicate field-key rejection even with no records;
- valid empty record sets;
- structured-input validation;
- boundary/extraction asset integrity.

## Explicit non-goals

`DP-ENGINE-014` does not:

- replace production source adapters yet;
- add CSS/XPath configuration;
- add manual record/value entry;
- implement login/RBAC/UI;
- migrate the Offer database;
- change single-record durable result schema;
- add multi-record durable persistence;
- add record-level lease/queue persistence;
- solve vision-based record segmentation;
- add distributed execution semantics.

Those can be layered on the generic contract without changing its field-resolution core.
