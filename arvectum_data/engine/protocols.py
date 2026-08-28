from __future__ import annotations

from typing import Protocol, Sequence

from .models import Candidate, FieldSpec, RawAsset


class CandidateProvider(Protocol):
    """Produces evidence-backed candidates without deciding final field values."""

    name: str

    def candidates(
        self,
        asset: RawAsset,
        fields: Sequence[FieldSpec],
    ) -> Sequence[Candidate]: ...
