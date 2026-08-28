from __future__ import annotations

from collections.abc import Mapping, Sequence

from .models import Candidate, Evidence, FieldSpec, RawAsset


class AttributeProvider:
    """Generic provider for already-structured source attributes.

    It is deliberately schema-agnostic: a domain adapter supplies the mapping from
    canonical field keys to attribute names.
    """

    def __init__(
        self,
        mapping: Mapping[str, str],
        *,
        confidence: float = 0.95,
        name: str = "attributes",
    ) -> None:
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        self.name = name
        self._mapping = dict(mapping)
        self._confidence = confidence

    def candidates(
        self,
        asset: RawAsset,
        fields: Sequence[FieldSpec],
    ) -> Sequence[Candidate]:
        requested = {field.key for field in fields}
        result: list[Candidate] = []
        for field_key, attribute_name in self._mapping.items():
            if field_key not in requested or attribute_name not in asset.attributes:
                continue
            value = asset.attributes[attribute_name]
            if value is None or value == "":
                continue
            result.append(
                Candidate(
                    field_key=field_key,
                    value=value,
                    confidence=self._confidence,
                    provider=self.name,
                    evidence=(
                        Evidence(
                            kind="structured_attribute",
                            source_ref=attribute_name,
                            excerpt=str(value)[:500],
                        ),
                    ),
                )
            )
        return result
