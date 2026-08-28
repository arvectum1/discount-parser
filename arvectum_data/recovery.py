from __future__ import annotations

from dataclasses import dataclass

from .acquisition import AcquisitionRequest, AcquisitionResult, RenderMode
from .engine import ExtractionResult, FieldStatus


@dataclass(frozen=True, slots=True)
class ExtractionQuality:
    """Domain-neutral quality summary used to compare static and rendered extraction."""

    unresolved_required: int
    review_required: int
    unresolved_total: int
    review_total: int
    accepted_total: int

    @classmethod
    def from_result(cls, result: ExtractionResult) -> "ExtractionQuality":
        decisions = tuple(result.decisions.values())
        unresolved_statuses = {FieldStatus.UNRESOLVED, FieldStatus.REJECTED}
        accepted_statuses = {FieldStatus.AUTO_SELECTED, FieldStatus.CONFIRMED}
        return cls(
            unresolved_required=sum(
                decision.field.required and decision.status in unresolved_statuses
                for decision in decisions
            ),
            review_required=sum(
                decision.field.required and decision.status is FieldStatus.NEEDS_CONFIRMATION
                for decision in decisions
            ),
            unresolved_total=sum(
                decision.status in unresolved_statuses for decision in decisions
            ),
            review_total=sum(
                decision.status is FieldStatus.NEEDS_CONFIRMATION
                for decision in decisions
            ),
            accepted_total=sum(
                decision.status in accepted_statuses for decision in decisions
            ),
        )

    @property
    def rank(self) -> tuple[int, int, int, int, int]:
        """Higher rank means a better governed result."""

        return (
            -self.unresolved_required,
            -self.review_required,
            -self.unresolved_total,
            -self.review_total,
            self.accepted_total,
        )


@dataclass(frozen=True, slots=True)
class SemanticRecoveryPolicy:
    """Trigger browser recovery only when AUTO static extraction misses required fields."""

    enabled: bool = True

    def should_retry(
        self,
        request: AcquisitionRequest,
        acquisition: AcquisitionResult,
        extraction: ExtractionResult,
    ) -> bool:
        if not self.enabled:
            return False
        if request.render_mode is not RenderMode.AUTO:
            return False
        if acquisition.used_renderer:
            return False
        return bool(extraction.unresolved_required_fields)

    def quality(self, extraction: ExtractionResult) -> ExtractionQuality:
        return ExtractionQuality.from_result(extraction)

    def prefer_rendered(
        self,
        static: ExtractionResult,
        rendered: ExtractionResult,
    ) -> bool:
        return self.quality(rendered).rank > self.quality(static).rank
