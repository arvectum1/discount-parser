from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from .engine.models import Candidate, RawAsset
from .engine.protocols import CandidateProvider


_INDEX_RE = re.compile(r"\[\d+\]")
_LINE_RE = re.compile(r"(?i)\bline:\d+\b")


def site_key_from_url(url: str | None) -> str | None:
    """Return a conservative site key without collapsing unrelated subdomains."""
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if not host:
            return None
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            pass
        host = host.casefold().rstrip(".")
        port = parsed.port
    except (TypeError, ValueError):
        return None

    default_port = 443 if parsed.scheme.casefold() == "https" else 80
    if port is None or port == default_port:
        return host
    return f"{host}:{port}"


def _normalize_source_ref(source_ref: str) -> str:
    value = " ".join(source_ref.split())
    value = _INDEX_RE.sub("[*]", value)
    value = _LINE_RE.sub("line:*", value)
    return value


def _semantic_terms(candidate: Candidate) -> tuple[str, ...]:
    raw = candidate.metadata.get("matched_terms", ())
    if isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, Sequence):
        return ()
    normalized = {
        " ".join(str(term).split()).casefold()
        for term in raw
        if str(term).strip()
    }
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class EvidenceFingerprint:
    kind: str
    source_ref: str
    semantic_terms: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return json.dumps(
            [self.kind, self.source_ref, list(self.semantic_terms)],
            ensure_ascii=False,
            separators=(",", ":"),
        )


def candidate_fingerprints(candidate: Candidate) -> tuple[EvidenceFingerprint, ...]:
    terms = _semantic_terms(candidate)
    fingerprints = {
        EvidenceFingerprint(
            kind=evidence.kind.casefold().strip(),
            source_ref=_normalize_source_ref(evidence.source_ref),
            semantic_terms=terms,
        )
        for evidence in candidate.evidence
        if evidence.kind.strip() and evidence.source_ref.strip()
    }
    return tuple(sorted(fingerprints, key=lambda fingerprint: fingerprint.key))


@dataclass(frozen=True, slots=True)
class ProfileSignalStats:
    confirmations: int = 0
    rejections: int = 0

    def __post_init__(self) -> None:
        if self.confirmations < 0 or self.rejections < 0:
            raise ValueError("Profile signal counters must be non-negative")


@dataclass(frozen=True, slots=True)
class LearningPolicy:
    positive_step: float = 0.06
    negative_step: float = 0.08
    max_positive: float = 0.18
    max_negative: float = 0.24
    max_candidate_confidence: float = 0.99

    def __post_init__(self) -> None:
        for name in (
            "positive_step",
            "negative_step",
            "max_positive",
            "max_negative",
            "max_candidate_confidence",
        ):
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.max_candidate_confidence > 1.0:
            raise ValueError("max_candidate_confidence must not exceed 1.0")

    def adjustment(self, stats: ProfileSignalStats) -> float:
        positive = min(self.max_positive, stats.confirmations * self.positive_step)
        negative = min(self.max_negative, stats.rejections * self.negative_step)
        return positive - negative


class SiteProfileStore(Protocol):
    def get_stats(
        self,
        site_key: str,
        field_key: str,
        fingerprint: EvidenceFingerprint,
    ) -> ProfileSignalStats: ...

    def record(
        self,
        site_key: str,
        field_key: str,
        *,
        positive: Sequence[EvidenceFingerprint] = (),
        negative: Sequence[EvidenceFingerprint] = (),
    ) -> None: ...

    def snapshot(self) -> Mapping[str, Any]: ...


class InMemorySiteProfileStore:
    """Value-free structural learning store."""

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        self._sites: dict[str, dict[str, dict[str, dict[str, int]]]] = {}
        if data:
            self._load_data(data)

    def _load_data(self, data: Mapping[str, Any]) -> None:
        sites = data.get("sites", data)
        if not isinstance(sites, Mapping):
            raise ValueError("Profile store data must contain a mapping of sites")
        for site_key, fields in sites.items():
            if not isinstance(fields, Mapping):
                continue
            for field_key, patterns in fields.items():
                if not isinstance(patterns, Mapping):
                    continue
                for pattern_key, raw_stats in patterns.items():
                    if not isinstance(raw_stats, Mapping):
                        continue
                    confirmations = int(raw_stats.get("confirmations", 0))
                    rejections = int(raw_stats.get("rejections", 0))
                    if confirmations < 0 or rejections < 0:
                        raise ValueError("Profile counters must be non-negative")
                    self._sites.setdefault(str(site_key), {}).setdefault(
                        str(field_key), {}
                    )[str(pattern_key)] = {
                        "confirmations": confirmations,
                        "rejections": rejections,
                    }

    def get_stats(
        self,
        site_key: str,
        field_key: str,
        fingerprint: EvidenceFingerprint,
    ) -> ProfileSignalStats:
        raw = (
            self._sites.get(site_key, {})
            .get(field_key, {})
            .get(fingerprint.key, {})
        )
        return ProfileSignalStats(
            confirmations=int(raw.get("confirmations", 0)),
            rejections=int(raw.get("rejections", 0)),
        )

    def record(
        self,
        site_key: str,
        field_key: str,
        *,
        positive: Sequence[EvidenceFingerprint] = (),
        negative: Sequence[EvidenceFingerprint] = (),
    ) -> None:
        positive_by_key = {fingerprint.key: fingerprint for fingerprint in positive}
        negative_by_key = {
            fingerprint.key: fingerprint
            for fingerprint in negative
            if fingerprint.key not in positive_by_key
        }
        field = self._sites.setdefault(site_key, {}).setdefault(field_key, {})
        for key in positive_by_key:
            stats = field.setdefault(key, {"confirmations": 0, "rejections": 0})
            stats["confirmations"] += 1
        for key in negative_by_key:
            stats = field.setdefault(key, {"confirmations": 0, "rejections": 0})
            stats["rejections"] += 1

    def snapshot(self) -> Mapping[str, Any]:
        return json.loads(
            json.dumps(
                {"version": 1, "sites": self._sites},
                ensure_ascii=False,
                sort_keys=True,
            )
        )


class JsonSiteProfileStore(InMemorySiteProfileStore):
    """Atomic JSON persistence for learned structural fingerprints."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        data: Mapping[str, Any] | None = None
        if self.path.exists():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("version") != 1:
                raise ValueError("Unsupported site profile store version")
            data = payload
        super().__init__(data=data)

    def record(
        self,
        site_key: str,
        field_key: str,
        *,
        positive: Sequence[EvidenceFingerprint] = (),
        negative: Sequence[EvidenceFingerprint] = (),
    ) -> None:
        super().record(
            site_key,
            field_key,
            positive=positive,
            negative=negative,
        )
        self._persist()

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            self.snapshot(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


class ProfileAwareProvider:
    """Apply bounded site-profile adjustments to candidates from another provider."""

    def __init__(
        self,
        provider: CandidateProvider,
        store: SiteProfileStore,
        *,
        policy: LearningPolicy | None = None,
    ) -> None:
        self.provider = provider
        self.store = store
        self.policy = policy or LearningPolicy()
        self.name = f"profile:{provider.name}"

    def candidates(self, asset: RawAsset, fields) -> Sequence[Candidate]:
        produced = tuple(self.provider.candidates(asset, fields))
        site_key = site_key_from_url(asset.source_url)
        if not site_key:
            return produced

        adjusted: list[Candidate] = []
        for candidate in produced:
            matches: list[tuple[EvidenceFingerprint, float, ProfileSignalStats]] = []
            for fingerprint in candidate_fingerprints(candidate):
                stats = self.store.get_stats(site_key, candidate.field_key, fingerprint)
                delta = self.policy.adjustment(stats)
                if delta:
                    matches.append((fingerprint, delta, stats))

            if not matches:
                adjusted.append(candidate)
                continue

            positive = max((delta for _, delta, _ in matches if delta > 0), default=0.0)
            negative = min((delta for _, delta, _ in matches if delta < 0), default=0.0)
            delta = positive + negative
            confidence = max(
                0.0,
                min(
                    self.policy.max_candidate_confidence,
                    candidate.confidence + delta,
                ),
            )
            metadata = dict(candidate.metadata)
            metadata["site_profile"] = {
                "site_key": site_key,
                "adjustment": round(delta, 6),
                "patterns": tuple(
                    {
                        "fingerprint": fingerprint.key,
                        "confirmations": stats.confirmations,
                        "rejections": stats.rejections,
                        "adjustment": round(item_delta, 6),
                    }
                    for fingerprint, item_delta, stats in matches
                ),
            }
            adjusted.append(
                replace(
                    candidate,
                    confidence=confidence,
                    metadata=metadata,
                )
            )
        return tuple(adjusted)


@dataclass(frozen=True, slots=True)
class LearningEvent:
    site_key: str
    field_key: str
    selected_candidate_id: str | None
    positive_fingerprints: tuple[str, ...] = ()
    negative_fingerprints: tuple[str, ...] = ()


class ConfirmationLearner:
    """Learn structural preferences only from explicit review actions."""

    def __init__(self, store: SiteProfileStore) -> None:
        self.store = store

    def learn(
        self,
        extraction_result: Any,
        selections: Mapping[str, str | None],
    ) -> tuple[LearningEvent, ...]:
        asset = getattr(extraction_result, "asset", None)
        site_key = site_key_from_url(getattr(asset, "source_url", None))
        decisions = getattr(extraction_result, "decisions", None)
        if not site_key or not isinstance(decisions, Mapping):
            return ()

        events: list[LearningEvent] = []
        for field_key, selected_candidate_id in selections.items():
            decision = decisions.get(field_key)
            if decision is None:
                continue
            candidates = tuple(getattr(decision, "candidates", ()) or ())
            if not candidates:
                continue

            positive: set[EvidenceFingerprint] = set()
            negative: set[EvidenceFingerprint] = set()
            for candidate in candidates:
                fingerprints = set(candidate_fingerprints(candidate))
                if (
                    selected_candidate_id is not None
                    and candidate.candidate_id == selected_candidate_id
                ):
                    positive.update(fingerprints)
                else:
                    negative.update(fingerprints)

            negative.difference_update(positive)
            if not positive and not negative:
                continue

            self.store.record(
                site_key,
                field_key,
                positive=tuple(sorted(positive, key=lambda item: item.key)),
                negative=tuple(sorted(negative, key=lambda item: item.key)),
            )
            events.append(
                LearningEvent(
                    site_key=site_key,
                    field_key=field_key,
                    selected_candidate_id=selected_candidate_id,
                    positive_fingerprints=tuple(sorted(item.key for item in positive)),
                    negative_fingerprints=tuple(sorted(item.key for item in negative)),
                )
            )
        return tuple(events)
