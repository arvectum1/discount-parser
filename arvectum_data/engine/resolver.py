from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence

from .models import Candidate, FieldDecision, FieldSpec, FieldStatus


def _rank(candidates: Iterable[Candidate]) -> tuple[Candidate, ...]:
    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                -candidate.confidence,
                candidate.provider,
                candidate.candidate_id,
            ),
        )
    )


def resolve_fields(
    fields: Sequence[FieldSpec],
    candidates: Iterable[Candidate],
) -> dict[str, FieldDecision]:
    specs = {field.key: field for field in fields}
    grouped: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        if candidate.field_key in specs:
            grouped[candidate.field_key].append(candidate)

    decisions: dict[str, FieldDecision] = {}
    for field in fields:
        ranked = _rank(grouped.get(field.key, ()))
        if not ranked:
            decisions[field.key] = FieldDecision(
                field=field,
                status=FieldStatus.UNRESOLVED,
                selected=None,
                candidates=(),
                reason="No provider produced a candidate.",
            )
            continue

        top = ranked[0]
        if top.confidence < field.min_confidence:
            decisions[field.key] = FieldDecision(
                field=field,
                status=FieldStatus.NEEDS_CONFIRMATION,
                selected=top,
                candidates=ranked,
                reason=(
                    f"Top confidence {top.confidence:.3f} is below required "
                    f"{field.min_confidence:.3f}."
                ),
            )
            continue

        if len(ranked) > 1:
            margin = top.confidence - ranked[1].confidence
            if margin < field.min_margin:
                decisions[field.key] = FieldDecision(
                    field=field,
                    status=FieldStatus.NEEDS_CONFIRMATION,
                    selected=top,
                    candidates=ranked,
                    reason=(
                        f"Top-candidate margin {margin:.3f} is below required "
                        f"{field.min_margin:.3f}."
                    ),
                )
                continue

        decisions[field.key] = FieldDecision(
            field=field,
            status=FieldStatus.AUTO_SELECTED,
            selected=top,
            candidates=ranked,
        )

    return decisions
