from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from .models import (
    Candidate,
    ExtractionResult,
    FieldSpec,
    FieldStatus,
    RawAsset,
)
from .protocols import CandidateProvider
from .resolver import resolve_fields


class ExtractionEngine:
    """Orchestrates candidate discovery, resolution and confirmation.

    Providers are intentionally domain-specific; this engine is not. Human review
    may select only candidates that providers already proposed, or reject a field.
    There is no manual-value overwrite path in the core API.
    """

    def __init__(self, providers: Sequence[CandidateProvider]) -> None:
        names = [provider.name for provider in providers]
        if len(names) != len(set(names)):
            raise ValueError("Candidate provider names must be unique")
        self._providers = tuple(providers)

    def extract(
        self,
        asset: RawAsset,
        fields: Sequence[FieldSpec],
    ) -> ExtractionResult:
        field_keys = [field.key for field in fields]
        if len(field_keys) != len(set(field_keys)):
            raise ValueError("FieldSpec keys must be unique")

        candidates: list[Candidate] = []
        provider_errors: dict[str, str] = {}
        for provider in self._providers:
            try:
                produced = provider.candidates(asset, fields)
                candidates.extend(
                    candidate
                    for candidate in produced
                    if candidate.field_key in field_keys
                )
            except Exception as exc:  # provider isolation is deliberate
                provider_errors[provider.name] = f"{type(exc).__name__}: {exc}"

        return ExtractionResult(
            asset=asset,
            decisions=resolve_fields(fields, candidates),
            provider_errors=provider_errors,
        )

    def confirm(
        self,
        result: ExtractionResult,
        selections: Mapping[str, str | None],
    ) -> ExtractionResult:
        decisions = dict(result.decisions)
        for field_key, candidate_id in selections.items():
            if field_key not in decisions:
                raise KeyError(field_key)

            decision = decisions[field_key]
            if decision.status is not FieldStatus.NEEDS_CONFIRMATION:
                raise ValueError(
                    f"Field {field_key!r} is {decision.status.value}; "
                    "only review-required fields may be confirmed or rejected."
                )

            if candidate_id is None:
                decisions[field_key] = replace(
                    decision,
                    status=FieldStatus.REJECTED,
                    selected=None,
                    reason="Reviewer rejected the proposed candidates.",
                )
                continue

            by_id = {candidate.candidate_id: candidate for candidate in decision.candidates}
            if candidate_id not in by_id:
                raise ValueError(
                    f"Unknown candidate_id {candidate_id!r} for field {field_key!r}; "
                    "manual values are not accepted by the engine."
                )

            decisions[field_key] = replace(
                decision,
                status=FieldStatus.CONFIRMED,
                selected=by_id[candidate_id],
                reason="Reviewer confirmed an engine-proposed candidate.",
            )

        return ExtractionResult(
            asset=result.asset,
            decisions=decisions,
            provider_errors=result.provider_errors,
        )
