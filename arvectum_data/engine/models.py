from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class FieldStatus(StrEnum):
    AUTO_SELECTED = "auto_selected"
    NEEDS_CONFIRMATION = "needs_confirmation"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: str
    source_ref: str
    excerpt: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RawAsset:
    asset_id: str
    source_url: str | None = None
    text: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    html: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FieldSpec:
    key: str
    required: bool = False
    min_confidence: float = 0.80
    min_margin: float = 0.10
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("FieldSpec.key must not be blank")
        if not 0.0 <= self.min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        if not 0.0 <= self.min_margin <= 1.0:
            raise ValueError("min_margin must be between 0 and 1")
        cleaned = tuple(alias.strip() for alias in self.aliases if alias.strip())
        if len(cleaned) != len(set(alias.casefold() for alias in cleaned)):
            raise ValueError("FieldSpec aliases must be unique")
        object.__setattr__(self, "aliases", cleaned)


@dataclass(frozen=True, slots=True)
class Candidate:
    field_key: str
    value: Any
    confidence: float
    provider: str
    evidence: tuple[Evidence, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    candidate_id: str = ""

    def __post_init__(self) -> None:
        if not self.field_key.strip():
            raise ValueError("Candidate.field_key must not be blank")
        if not self.provider.strip():
            raise ValueError("Candidate.provider must not be blank")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Candidate.confidence must be between 0 and 1")
        if not self.candidate_id:
            payload = json.dumps(
                {
                    "field_key": self.field_key,
                    "provider": self.provider,
                    "value": self.value,
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            )
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
            object.__setattr__(self, "candidate_id", digest)


@dataclass(frozen=True, slots=True)
class FieldDecision:
    field: FieldSpec
    status: FieldStatus
    selected: Candidate | None
    candidates: tuple[Candidate, ...] = ()
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    asset: RawAsset
    decisions: Mapping[str, FieldDecision]
    provider_errors: Mapping[str, str] = field(default_factory=dict)

    @property
    def requires_confirmation(self) -> bool:
        return any(
            decision.status is FieldStatus.NEEDS_CONFIRMATION
            for decision in self.decisions.values()
        )

    @property
    def unresolved_required_fields(self) -> tuple[str, ...]:
        return tuple(
            key
            for key, decision in self.decisions.items()
            if decision.field.required
            and decision.status in {FieldStatus.UNRESOLVED, FieldStatus.REJECTED}
        )

    def values(self, *, include_unconfirmed: bool = False) -> dict[str, Any]:
        accepted = {FieldStatus.AUTO_SELECTED, FieldStatus.CONFIRMED}
        if include_unconfirmed:
            accepted.add(FieldStatus.NEEDS_CONFIRMATION)
        return {
            key: decision.selected.value
            for key, decision in self.decisions.items()
            if decision.selected is not None and decision.status in accepted
        }
