from __future__ import annotations

import base64
import math
from collections.abc import Mapping, Sequence
from typing import Any

from ..acquisition import AcquisitionAttempt, AcquisitionResult
from ..engine import (
    Candidate,
    Evidence,
    ExtractionResult,
    FieldDecision,
    FieldSpec,
    FieldStatus,
    RawAsset,
)
from ..orchestration import URLExtractionResult
from ..profiles import LearningEvent
from .models import ResultIntegrityError, ResultSerializationError


class ResultCodec:
    """Strict, type-preserving codec for durable URL extraction results.

    Raw page text/html/attributes are omitted by default because reviewer continuation
    only needs source identity, candidates/evidence and acquisition/extraction state.
    Set include_raw_content=True when a governed deployment explicitly needs a full
    raw-page snapshot in the durable result store.
    """

    def __init__(self, *, include_raw_content: bool = False) -> None:
        self.include_raw_content = include_raw_content

    def encode(self, result: URLExtractionResult) -> dict[str, Any]:
        asset = result.asset
        return {
            "raw_content_persisted": self.include_raw_content,
            "asset": self._encode_asset(asset),
            "acquisition": {
                "attempts": [self._encode_attempt(item) for item in result.acquisition.attempts],
                "warnings": list(result.acquisition.warnings),
            },
            "extraction": {
                "decisions": {
                    key: self._encode_decision(decision)
                    for key, decision in result.extraction.decisions.items()
                },
                "provider_errors": dict(result.extraction.provider_errors),
            },
            "learning_events": [self._encode_learning_event(item) for item in result.learning_events],
            "learning_warnings": list(result.learning_warnings),
        }

    def decode(self, payload: Mapping[str, Any]) -> URLExtractionResult:
        try:
            raw_flag = payload.get("raw_content_persisted", False)
            if not isinstance(raw_flag, bool):
                raise ResultIntegrityError("raw_content_persisted must be boolean")
            asset = self._decode_asset(
                self._mapping(payload, "asset"),
                raw_content_persisted=raw_flag,
            )
            acquisition_payload = self._mapping(payload, "acquisition")
            extraction_payload = self._mapping(payload, "extraction")
            attempts_raw = acquisition_payload.get("attempts", ())
            if not isinstance(attempts_raw, Sequence) or isinstance(attempts_raw, (str, bytes)):
                raise ResultIntegrityError("acquisition.attempts must be a sequence")
            attempts = tuple(self._decode_attempt(item) for item in attempts_raw)
            warnings = self._string_tuple(acquisition_payload.get("warnings", ()), "acquisition.warnings")

            decisions_raw = extraction_payload.get("decisions", {})
            if not isinstance(decisions_raw, Mapping):
                raise ResultIntegrityError("extraction.decisions must be a mapping")
            decisions = {
                str(key): self._decode_decision(raw)
                for key, raw in decisions_raw.items()
            }
            provider_errors_raw = extraction_payload.get("provider_errors", {})
            if not isinstance(provider_errors_raw, Mapping):
                raise ResultIntegrityError("provider_errors must be a mapping")
            provider_errors = {str(key): str(value) for key, value in provider_errors_raw.items()}

            learning_raw = payload.get("learning_events", ())
            if not isinstance(learning_raw, Sequence) or isinstance(learning_raw, (str, bytes)):
                raise ResultIntegrityError("learning_events must be a sequence")
            learning_events = tuple(self._decode_learning_event(item) for item in learning_raw)
            learning_warnings = self._string_tuple(payload.get("learning_warnings", ()), "learning_warnings")
        except ResultIntegrityError:
            raise
        except Exception as exc:
            raise ResultIntegrityError(f"Invalid durable result payload: {type(exc).__name__}: {exc}") from exc

        acquisition = AcquisitionResult(asset=asset, attempts=attempts, warnings=warnings)
        extraction = ExtractionResult(
            asset=asset,
            decisions=decisions,
            provider_errors=provider_errors,
        )
        return URLExtractionResult(
            acquisition=acquisition,
            extraction=extraction,
            learning_events=learning_events,
            learning_warnings=learning_warnings,
        )

    def _encode_asset(self, asset: RawAsset) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "asset_id": asset.asset_id,
            "source_url": asset.source_url,
            "metadata": self._encode_value(dict(asset.metadata)),
        }
        if self.include_raw_content:
            payload.update(
                {
                    "text": asset.text,
                    "html": asset.html,
                    "attributes": self._encode_value(dict(asset.attributes)),
                }
            )
        return payload

    def _decode_asset(
        self,
        payload: Mapping[str, Any],
        *,
        raw_content_persisted: bool,
    ) -> RawAsset:
        metadata = self._decode_value(payload.get("metadata", ["dict", []]))
        if not isinstance(metadata, Mapping):
            raise ResultIntegrityError("asset.metadata must decode to a mapping")
        raw_keys = {"text", "html", "attributes"}
        present_raw_keys = raw_keys.intersection(payload)
        if raw_content_persisted and present_raw_keys != raw_keys:
            raise ResultIntegrityError("full raw-content payload is incomplete")
        if not raw_content_persisted and present_raw_keys:
            raise ResultIntegrityError("raw content present while policy marker is false")
        attributes: Mapping[str, Any] = {}
        if raw_content_persisted:
            decoded_attributes = self._decode_value(payload.get("attributes", ["dict", []]))
            if not isinstance(decoded_attributes, Mapping):
                raise ResultIntegrityError("asset.attributes must decode to a mapping")
            attributes = decoded_attributes
        return RawAsset(
            asset_id=str(payload["asset_id"]),
            source_url=None if payload.get("source_url") is None else str(payload.get("source_url")),
            text=None if not raw_content_persisted or payload.get("text") is None else str(payload.get("text")),
            attributes=dict(attributes),
            html=None if not raw_content_persisted or payload.get("html") is None else str(payload.get("html")),
            metadata=dict(metadata),
        )

    @staticmethod
    def _encode_attempt(attempt: AcquisitionAttempt) -> dict[str, Any]:
        return {
            "method": attempt.method,
            "success": attempt.success,
            "reason": attempt.reason,
            "status_code": attempt.status_code,
            "final_url": attempt.final_url,
            "rendered": attempt.rendered,
        }

    @staticmethod
    def _decode_attempt(payload: Any) -> AcquisitionAttempt:
        if not isinstance(payload, Mapping):
            raise ResultIntegrityError("Acquisition attempt must be a mapping")
        status_code = payload.get("status_code")
        return AcquisitionAttempt(
            method=str(payload["method"]),
            success=bool(payload["success"]),
            reason=str(payload["reason"]),
            status_code=None if status_code is None else int(status_code),
            final_url=None if payload.get("final_url") is None else str(payload.get("final_url")),
            rendered=bool(payload.get("rendered", False)),
        )

    def _encode_decision(self, decision: FieldDecision) -> dict[str, Any]:
        return {
            "field": self._encode_field(decision.field),
            "status": decision.status.value,
            "selected_candidate_id": None if decision.selected is None else decision.selected.candidate_id,
            "candidates": [self._encode_candidate(item) for item in decision.candidates],
            "reason": decision.reason,
        }

    def _decode_decision(self, payload: Any) -> FieldDecision:
        if not isinstance(payload, Mapping):
            raise ResultIntegrityError("Field decision must be a mapping")
        candidates_raw = payload.get("candidates", ())
        if not isinstance(candidates_raw, Sequence) or isinstance(candidates_raw, (str, bytes)):
            raise ResultIntegrityError("decision.candidates must be a sequence")
        candidates = tuple(self._decode_candidate(item) for item in candidates_raw)
        by_id = {candidate.candidate_id: candidate for candidate in candidates}
        selected_id = payload.get("selected_candidate_id")
        selected = None
        if selected_id is not None:
            selected = by_id.get(str(selected_id))
            if selected is None:
                raise ResultIntegrityError("selected candidate id is absent from candidates")
        return FieldDecision(
            field=self._decode_field(self._mapping(payload, "field")),
            status=FieldStatus(str(payload["status"])),
            selected=selected,
            candidates=candidates,
            reason=None if payload.get("reason") is None else str(payload.get("reason")),
        )

    @staticmethod
    def _encode_field(field: FieldSpec) -> dict[str, Any]:
        return {
            "key": field.key,
            "required": field.required,
            "min_confidence": field.min_confidence,
            "min_margin": field.min_margin,
            "aliases": list(field.aliases),
        }

    @staticmethod
    def _decode_field(payload: Mapping[str, Any]) -> FieldSpec:
        aliases = payload.get("aliases", ())
        if not isinstance(aliases, Sequence) or isinstance(aliases, (str, bytes)):
            raise ResultIntegrityError("field.aliases must be a sequence")
        return FieldSpec(
            key=str(payload["key"]),
            required=bool(payload.get("required", False)),
            min_confidence=float(payload.get("min_confidence", 0.80)),
            min_margin=float(payload.get("min_margin", 0.10)),
            aliases=tuple(str(item) for item in aliases),
        )

    def _encode_candidate(self, candidate: Candidate) -> dict[str, Any]:
        return {
            "field_key": candidate.field_key,
            "value": self._encode_value(candidate.value),
            "confidence": candidate.confidence,
            "provider": candidate.provider,
            "evidence": [self._encode_evidence(item) for item in candidate.evidence],
            "metadata": self._encode_value(dict(candidate.metadata)),
            "candidate_id": candidate.candidate_id,
        }

    def _decode_candidate(self, payload: Any) -> Candidate:
        if not isinstance(payload, Mapping):
            raise ResultIntegrityError("Candidate must be a mapping")
        evidence_raw = payload.get("evidence", ())
        if not isinstance(evidence_raw, Sequence) or isinstance(evidence_raw, (str, bytes)):
            raise ResultIntegrityError("candidate.evidence must be a sequence")
        metadata = self._decode_value(payload.get("metadata", ["dict", []]))
        if not isinstance(metadata, Mapping):
            raise ResultIntegrityError("candidate.metadata must decode to a mapping")
        return Candidate(
            field_key=str(payload["field_key"]),
            value=self._decode_value(payload["value"]),
            confidence=float(payload["confidence"]),
            provider=str(payload["provider"]),
            evidence=tuple(self._decode_evidence(item) for item in evidence_raw),
            metadata=dict(metadata),
            candidate_id=str(payload["candidate_id"]),
        )

    def _encode_evidence(self, evidence: Evidence) -> dict[str, Any]:
        return {
            "kind": evidence.kind,
            "source_ref": evidence.source_ref,
            "excerpt": evidence.excerpt,
            "metadata": self._encode_value(dict(evidence.metadata)),
        }

    def _decode_evidence(self, payload: Any) -> Evidence:
        if not isinstance(payload, Mapping):
            raise ResultIntegrityError("Evidence must be a mapping")
        metadata = self._decode_value(payload.get("metadata", ["dict", []]))
        if not isinstance(metadata, Mapping):
            raise ResultIntegrityError("evidence.metadata must decode to a mapping")
        return Evidence(
            kind=str(payload["kind"]),
            source_ref=str(payload["source_ref"]),
            excerpt=None if payload.get("excerpt") is None else str(payload.get("excerpt")),
            metadata=dict(metadata),
        )

    @staticmethod
    def _encode_learning_event(event: LearningEvent) -> dict[str, Any]:
        return {
            "site_key": event.site_key,
            "field_key": event.field_key,
            "selected_candidate_id": event.selected_candidate_id,
            "positive_fingerprints": list(event.positive_fingerprints),
            "negative_fingerprints": list(event.negative_fingerprints),
        }

    @staticmethod
    def _decode_learning_event(payload: Any) -> LearningEvent:
        if not isinstance(payload, Mapping):
            raise ResultIntegrityError("Learning event must be a mapping")
        return LearningEvent(
            site_key=str(payload["site_key"]),
            field_key=str(payload["field_key"]),
            selected_candidate_id=(
                None
                if payload.get("selected_candidate_id") is None
                else str(payload.get("selected_candidate_id"))
            ),
            positive_fingerprints=tuple(str(item) for item in payload.get("positive_fingerprints", ())),
            negative_fingerprints=tuple(str(item) for item in payload.get("negative_fingerprints", ())),
        )

    def _encode_value(self, value: Any) -> list[Any]:
        if value is None:
            return ["null"]
        if isinstance(value, bool):
            return ["bool", value]
        if isinstance(value, int) and not isinstance(value, bool):
            return ["int", value]
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ResultSerializationError("Non-finite floats are not supported in durable results")
            return ["float", repr(value)]
        if isinstance(value, str):
            return ["str", value]
        if isinstance(value, bytes):
            return ["bytes", base64.b64encode(value).decode("ascii")]
        if isinstance(value, list):
            return ["list", [self._encode_value(item) for item in value]]
        if isinstance(value, tuple):
            return ["tuple", [self._encode_value(item) for item in value]]
        if isinstance(value, Mapping):
            pairs: list[list[Any]] = []
            for key in sorted(value, key=lambda item: str(item)):
                if not isinstance(key, str):
                    raise ResultSerializationError("Durable result mappings require string keys")
                pairs.append([key, self._encode_value(value[key])])
            return ["dict", pairs]
        raise ResultSerializationError(
            f"Unsupported durable result value type: {type(value).__name__}"
        )

    def _decode_value(self, encoded: Any) -> Any:
        if not isinstance(encoded, list) or not encoded:
            raise ResultIntegrityError("Typed durable value must be a non-empty list")
        tag = encoded[0]
        if tag == "null" and len(encoded) == 1:
            return None
        if len(encoded) != 2:
            raise ResultIntegrityError(f"Invalid typed durable value for tag {tag!r}")
        payload = encoded[1]
        if tag == "bool":
            return bool(payload)
        if tag == "int":
            return int(payload)
        if tag == "float":
            value = float(payload)
            if not math.isfinite(value):
                raise ResultIntegrityError("Non-finite durable float")
            return value
        if tag == "str":
            return str(payload)
        if tag == "bytes":
            try:
                return base64.b64decode(str(payload).encode("ascii"), validate=True)
            except Exception as exc:
                raise ResultIntegrityError("Invalid durable bytes encoding") from exc
        if tag in {"list", "tuple"}:
            if not isinstance(payload, list):
                raise ResultIntegrityError(f"{tag} payload must be a list")
            values = [self._decode_value(item) for item in payload]
            return values if tag == "list" else tuple(values)
        if tag == "dict":
            if not isinstance(payload, list):
                raise ResultIntegrityError("dict payload must be a list")
            result: dict[str, Any] = {}
            for pair in payload:
                if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str):
                    raise ResultIntegrityError("dict entry must be [string-key, typed-value]")
                if pair[0] in result:
                    raise ResultIntegrityError("duplicate durable mapping key")
                result[pair[0]] = self._decode_value(pair[1])
            return result
        raise ResultIntegrityError(f"Unknown durable value tag {tag!r}")

    @staticmethod
    def _mapping(container: Mapping[str, Any], key: str) -> Mapping[str, Any]:
        value = container.get(key)
        if not isinstance(value, Mapping):
            raise ResultIntegrityError(f"{key} must be a mapping")
        return value

    @staticmethod
    def _string_tuple(value: Any, name: str) -> tuple[str, ...]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ResultIntegrityError(f"{name} must be a sequence")
        return tuple(str(item) for item in value)
