from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Sequence

from .models import Candidate, FieldSpec, RawAsset

if TYPE_CHECKING:
    from .records import RecordProviderResult


class CandidateProvider(Protocol):
    """Produces evidence-backed candidates without deciding final field values."""

    name: str

    def candidates(
        self,
        asset: RawAsset,
        fields: Sequence[FieldSpec],
    ) -> Sequence[Candidate]: ...


class RecordProvider(Protocol):
    """Proposes bounded record slices without deciding any field values."""

    name: str

    def records(
        self,
        asset: RawAsset,
        fields: Sequence[FieldSpec],
    ) -> RecordProviderResult: ...
