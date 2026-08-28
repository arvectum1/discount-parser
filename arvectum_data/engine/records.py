from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from html.parser import HTMLParser
from typing import Any

from .engine import ExtractionEngine
from .models import Evidence, ExtractionResult, FieldSpec, RawAsset
from .protocols import CandidateProvider, RecordProvider


class RecordBoundaryStatus(StrEnum):
    AUTO_SELECTED = "auto_selected"
    NEEDS_CONFIRMATION = "needs_confirmation"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class RecordStatus(StrEnum):
    READY = "ready"
    NEEDS_CONFIRMATION = "needs_confirmation"
    INCOMPLETE = "incomplete"
    REJECTED = "rejected"


def make_record_id(parent_asset_id: str, provider: str, source_ref: str) -> str:
    """Build a deterministic record identifier from structural provenance only."""
    if not parent_asset_id.strip():
        raise ValueError("parent_asset_id must not be blank")
    if not provider.strip():
        raise ValueError("provider must not be blank")
    if not source_ref.strip():
        raise ValueError("source_ref must not be blank")
    payload = json.dumps(
        {
            "parent_asset_id": parent_asset_id,
            "provider": provider,
            "source_ref": source_ref,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "rec_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class RecordBoundary:
    """One automatically discovered record-sized slice of a larger source asset."""

    record_id: str
    asset: RawAsset
    provider: str
    source_ref: str
    ordinal: int
    confidence: float
    evidence: tuple[Evidence, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.record_id.strip():
            raise ValueError("RecordBoundary.record_id must not be blank")
        if not self.provider.strip():
            raise ValueError("RecordBoundary.provider must not be blank")
        if not self.source_ref.strip():
            raise ValueError("RecordBoundary.source_ref must not be blank")
        if self.ordinal < 0:
            raise ValueError("RecordBoundary.ordinal must be non-negative")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("RecordBoundary.confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class RecordProviderResult:
    records: tuple[RecordBoundary, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecordExtractionResult:
    boundary: RecordBoundary
    boundary_status: RecordBoundaryStatus
    extraction: ExtractionResult
    boundary_reason: str | None = None

    def __post_init__(self) -> None:
        if self.extraction.asset.asset_id != self.boundary.asset.asset_id:
            raise ValueError("Record extraction asset must match its boundary asset")

    @property
    def record_id(self) -> str:
        return self.boundary.record_id

    @property
    def status(self) -> RecordStatus:
        if self.boundary_status is RecordBoundaryStatus.REJECTED:
            return RecordStatus.REJECTED
        if self.boundary_status is RecordBoundaryStatus.NEEDS_CONFIRMATION:
            return RecordStatus.NEEDS_CONFIRMATION
        if self.extraction.requires_confirmation:
            return RecordStatus.NEEDS_CONFIRMATION
        if self.extraction.unresolved_required_fields:
            return RecordStatus.INCOMPLETE
        return RecordStatus.READY

    @property
    def requires_confirmation(self) -> bool:
        return self.status is RecordStatus.NEEDS_CONFIRMATION

    @property
    def unresolved_required_fields(self) -> tuple[str, ...]:
        return self.extraction.unresolved_required_fields

    def values(self, *, include_unconfirmed: bool = False) -> dict[str, Any]:
        if self.boundary_status is RecordBoundaryStatus.REJECTED:
            return {}
        if (
            self.boundary_status is RecordBoundaryStatus.NEEDS_CONFIRMATION
            and not include_unconfirmed
        ):
            return {}
        return self.extraction.values(include_unconfirmed=include_unconfirmed)


@dataclass(frozen=True, slots=True)
class RecordSetResult:
    """Independent field decisions for every record discovered in one source asset."""

    asset: RawAsset
    records: tuple[RecordExtractionResult, ...]
    record_provider_errors: Mapping[str, str] = field(default_factory=dict)
    record_provider_warnings: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ids = [record.record_id for record in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError("RecordSetResult record_ids must be unique")

    @property
    def requires_confirmation(self) -> bool:
        return any(record.requires_confirmation for record in self.records)

    @property
    def review_record_ids(self) -> tuple[str, ...]:
        return tuple(
            record.record_id
            for record in self.records
            if record.status is RecordStatus.NEEDS_CONFIRMATION
        )

    @property
    def ready_record_ids(self) -> tuple[str, ...]:
        return tuple(
            record.record_id
            for record in self.records
            if record.status is RecordStatus.READY
        )

    @property
    def incomplete_record_ids(self) -> tuple[str, ...]:
        return tuple(
            record.record_id
            for record in self.records
            if record.status is RecordStatus.INCOMPLETE
        )

    @property
    def rejected_record_ids(self) -> tuple[str, ...]:
        return tuple(
            record.record_id
            for record in self.records
            if record.status is RecordStatus.REJECTED
        )

    def record(self, record_id: str) -> RecordExtractionResult:
        for record in self.records:
            if record.record_id == record_id:
                return record
        raise KeyError(record_id)

    def values(
        self,
        *,
        include_unconfirmed: bool = False,
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for record in self.records:
            values = record.values(include_unconfirmed=include_unconfirmed)
            if values or (
                record.boundary_status is not RecordBoundaryStatus.REJECTED
                and (
                    record.boundary_status is not RecordBoundaryStatus.NEEDS_CONFIRMATION
                    or include_unconfirmed
                )
            ):
                result[record.record_id] = values
        return result


class MultiRecordExtractionEngine:
    """Discover record boundaries, then reuse the single-record field engine per slice.

    Record providers are responsible only for structural segmentation. Candidate
    providers remain responsible only for field candidates. Human review can accept
    or reject an engine-proposed record boundary and can select only existing field
    candidate IDs; there is no API for manually inserting records or values.
    """

    def __init__(
        self,
        record_providers: Sequence[RecordProvider],
        candidate_providers: Sequence[CandidateProvider],
        *,
        min_boundary_confidence: float = 0.80,
    ) -> None:
        record_names = [provider.name for provider in record_providers]
        if len(record_names) != len(set(record_names)):
            raise ValueError("Record provider names must be unique")
        if not 0.0 <= min_boundary_confidence <= 1.0:
            raise ValueError("min_boundary_confidence must be between 0 and 1")
        self._record_providers = tuple(record_providers)
        self._field_engine = ExtractionEngine(candidate_providers)
        self._min_boundary_confidence = min_boundary_confidence

    def extract(
        self,
        asset: RawAsset,
        fields: Sequence[FieldSpec],
    ) -> RecordSetResult:
        field_keys = [field.key for field in fields]
        if len(field_keys) != len(set(field_keys)):
            raise ValueError("FieldSpec keys must be unique")

        boundaries: list[RecordBoundary] = []
        seen_ids: set[str] = set()
        errors: dict[str, str] = {}
        warnings: dict[str, tuple[str, ...]] = {}

        for provider in self._record_providers:
            try:
                produced = provider.records(asset, fields)
                if not isinstance(produced, RecordProviderResult):
                    raise TypeError("RecordProvider.records() must return RecordProviderResult")
                staged: list[RecordBoundary] = []
                local_ids: set[str] = set()
                for boundary in produced.records:
                    if boundary.provider != provider.name:
                        raise ValueError(
                            f"Record boundary provider {boundary.provider!r} does not match "
                            f"producer {provider.name!r}"
                        )
                    if boundary.record_id in seen_ids or boundary.record_id in local_ids:
                        raise ValueError(
                            f"Duplicate record_id {boundary.record_id!r} from {provider.name!r}"
                        )
                    local_ids.add(boundary.record_id)
                    staged.append(boundary)
                boundaries.extend(staged)
                seen_ids.update(local_ids)
                if produced.warnings:
                    warnings[provider.name] = tuple(produced.warnings)
            except Exception as exc:  # record-provider isolation is deliberate
                errors[provider.name] = f"{type(exc).__name__}: {exc}"

        boundaries.sort(
            key=lambda boundary: (
                boundary.ordinal,
                boundary.provider,
                boundary.record_id,
            )
        )
        records: list[RecordExtractionResult] = []
        for boundary in boundaries:
            extraction = self._field_engine.extract(boundary.asset, fields)
            auto_selected = boundary.confidence >= self._min_boundary_confidence
            records.append(
                RecordExtractionResult(
                    boundary=boundary,
                    boundary_status=(
                        RecordBoundaryStatus.AUTO_SELECTED
                        if auto_selected
                        else RecordBoundaryStatus.NEEDS_CONFIRMATION
                    ),
                    extraction=extraction,
                    boundary_reason=(
                        "Record boundary met automatic confidence threshold."
                        if auto_selected
                        else "Record boundary requires reviewer confirmation."
                    ),
                )
            )

        return RecordSetResult(
            asset=asset,
            records=tuple(records),
            record_provider_errors=errors,
            record_provider_warnings=warnings,
        )

    def confirm_boundary(
        self,
        result: RecordSetResult,
        record_id: str,
        *,
        accept: bool,
    ) -> RecordSetResult:
        target = result.record(record_id)
        if target.boundary_status is not RecordBoundaryStatus.NEEDS_CONFIRMATION:
            raise ValueError(
                f"Record {record_id!r} boundary is {target.boundary_status.value}; "
                "only review-required boundaries may be confirmed or rejected."
            )
        replacement = replace(
            target,
            boundary_status=(
                RecordBoundaryStatus.CONFIRMED
                if accept
                else RecordBoundaryStatus.REJECTED
            ),
            boundary_reason=(
                "Reviewer confirmed an engine-proposed record boundary."
                if accept
                else "Reviewer rejected an engine-proposed record boundary."
            ),
        )
        return self._replace_record(result, replacement)

    def confirm_fields(
        self,
        result: RecordSetResult,
        record_id: str,
        selections: Mapping[str, str | None],
    ) -> RecordSetResult:
        target = result.record(record_id)
        if target.boundary_status is RecordBoundaryStatus.REJECTED:
            raise ValueError(f"Record {record_id!r} was rejected and cannot be field-reviewed")
        replacement = replace(
            target,
            extraction=self._field_engine.confirm(target.extraction, selections),
        )
        return self._replace_record(result, replacement)

    @staticmethod
    def _replace_record(
        result: RecordSetResult,
        replacement: RecordExtractionResult,
    ) -> RecordSetResult:
        records = tuple(
            replacement if record.record_id == replacement.record_id else record
            for record in result.records
        )
        return replace(result, records=records)


def _semantic_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _field_semantics(fields: Sequence[FieldSpec]) -> dict[str, set[str]]:
    semantics: dict[str, set[str]] = {}
    for field in fields:
        terms = {_semantic_key(field.key)}
        terms.update(_semantic_key(alias) for alias in field.aliases)
        semantics[field.key] = {term for term in terms if term}
    return semantics


def _direct_matching_fields(
    value: Mapping[str, Any],
    fields: Sequence[FieldSpec],
) -> tuple[str, ...]:
    semantics = _field_semantics(fields)
    matches: set[str] = set()
    for raw_key, child in value.items():
        if child is None or child == "":
            continue
        if not isinstance(child, (str, int, float, bool)) and not (
            isinstance(child, list)
            and child
            and all(isinstance(item, (str, int, float, bool)) for item in child)
        ):
            continue
        key = _semantic_key(str(raw_key))
        for field_key, terms in semantics.items():
            if key in terms:
                matches.add(field_key)
    return tuple(sorted(matches))


class AttributeRecordProvider:
    """Treat an explicitly structured sequence in RawAsset.attributes as records."""

    def __init__(
        self,
        *,
        attribute_key: str = "records",
        max_records: int = 200,
        confidence: float = 0.99,
        name: str = "attribute_records",
    ) -> None:
        if not attribute_key.strip():
            raise ValueError("attribute_key must not be blank")
        if max_records < 1:
            raise ValueError("max_records must be positive")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        self.name = name
        self.attribute_key = attribute_key
        self.max_records = max_records
        self.confidence = confidence

    def records(
        self,
        asset: RawAsset,
        fields: Sequence[FieldSpec],
    ) -> RecordProviderResult:
        raw = asset.attributes.get(self.attribute_key)
        if raw is None:
            return RecordProviderResult()
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
            raise ValueError(f"asset.attributes[{self.attribute_key!r}] must be a sequence")

        warnings: list[str] = []
        source_records = list(raw)
        if len(source_records) > self.max_records:
            warnings.append(f"max_records:{self.max_records}")
            source_records = source_records[: self.max_records]

        boundaries: list[RecordBoundary] = []
        for index, item in enumerate(source_records):
            if not isinstance(item, Mapping):
                raise ValueError(
                    f"asset.attributes[{self.attribute_key!r}][{index}] must be a mapping"
                )
            source_ref = f"attributes.{self.attribute_key}[{index}]"
            child_asset = RawAsset(
                asset_id=f"{asset.asset_id}#{make_record_id(asset.asset_id, self.name, source_ref)}",
                source_url=asset.source_url,
                attributes=dict(item),
                metadata={
                    "record_parent_asset_id": asset.asset_id,
                    "record_provider": self.name,
                    "record_source_ref": source_ref,
                },
            )
            boundaries.append(
                RecordBoundary(
                    record_id=make_record_id(asset.asset_id, self.name, source_ref),
                    asset=child_asset,
                    provider=self.name,
                    source_ref=source_ref,
                    ordinal=index,
                    confidence=self.confidence,
                    evidence=(
                        Evidence(
                            kind="structured_record_boundary",
                            source_ref=source_ref,
                            metadata={"attribute_key": self.attribute_key},
                        ),
                    ),
                )
            )
        return RecordProviderResult(records=tuple(boundaries), warnings=tuple(warnings))


class _JSONLDParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []
        self._capturing = False
        self._parts: list[str] = []
        self._index = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "script" or self._capturing:
            return
        attributes = {str(key).casefold(): str(value or "") for key, value in attrs}
        content_type = attributes.get("type", "").casefold().split(";", 1)[0].strip()
        if content_type == "application/ld+json":
            self._capturing = True
            self._parts = []
            self._index += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._capturing:
            self.blocks.append((f"script[{self._index}]", "".join(self._parts)))
            self._parts = []
            self._capturing = False

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._parts.append(data)


def _walk_json_objects(value: Any, path: str):
    if isinstance(value, Mapping):
        yield path, value
        for key, child in value.items():
            yield from _walk_json_objects(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_json_objects(child, f"{path}[{index}]")


class JSONLDRecordProvider:
    """Discover repeated record objects in JSON-LD without CSS/XPath selectors."""

    def __init__(
        self,
        *,
        min_matched_fields: int = 2,
        max_records: int = 200,
        name: str = "jsonld_records",
    ) -> None:
        if min_matched_fields < 1:
            raise ValueError("min_matched_fields must be positive")
        if max_records < 1:
            raise ValueError("max_records must be positive")
        self.name = name
        self.min_matched_fields = min_matched_fields
        self.max_records = max_records

    def records(
        self,
        asset: RawAsset,
        fields: Sequence[FieldSpec],
    ) -> RecordProviderResult:
        if not asset.html or not fields:
            return RecordProviderResult()

        parser = _JSONLDParser()
        parser.feed(asset.html)
        parser.close()
        warnings: list[str] = []
        discovered: list[tuple[str, Mapping[str, Any], tuple[str, ...]]] = []
        effective_min = min(self.min_matched_fields, len(fields))

        for block_ref, block in parser.blocks:
            try:
                payload = json.loads(block)
            except (json.JSONDecodeError, TypeError, ValueError):
                warnings.append(f"malformed_jsonld:{block_ref}")
                continue
            for source_ref, value in _walk_json_objects(payload, block_ref):
                matched_fields = _direct_matching_fields(value, fields)
                if len(matched_fields) >= effective_min:
                    discovered.append((source_ref, value, matched_fields))

        if len(discovered) > self.max_records:
            warnings.append(f"max_records:{self.max_records}")
            discovered = discovered[: self.max_records]

        boundaries: list[RecordBoundary] = []
        for ordinal, (source_ref, value, matched_fields) in enumerate(discovered):
            serialized = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            safe_json = serialized.replace("</", "<\\/")
            record_id = make_record_id(asset.asset_id, self.name, source_ref)
            raw_type = value.get("@type")
            child_asset = RawAsset(
                asset_id=f"{asset.asset_id}#{record_id}",
                source_url=asset.source_url,
                html=f'<script type="application/ld+json">{safe_json}</script>',
                metadata={
                    "record_parent_asset_id": asset.asset_id,
                    "record_provider": self.name,
                    "record_source_ref": source_ref,
                    "jsonld_type": raw_type,
                    "matched_fields": matched_fields,
                },
            )
            confidence = min(0.99, 0.92 + 0.02 * min(len(matched_fields), 3))
            boundaries.append(
                RecordBoundary(
                    record_id=record_id,
                    asset=child_asset,
                    provider=self.name,
                    source_ref=source_ref,
                    ordinal=ordinal,
                    confidence=confidence,
                    evidence=(
                        Evidence(
                            kind="jsonld_record_boundary",
                            source_ref=source_ref,
                            excerpt=serialized[:500],
                            metadata={
                                "matched_fields": matched_fields,
                                "jsonld_type": raw_type,
                            },
                        ),
                    ),
                    metadata={
                        "matched_fields": matched_fields,
                        "jsonld_type": raw_type,
                    },
                )
            )

        return RecordProviderResult(records=tuple(boundaries), warnings=tuple(warnings))
