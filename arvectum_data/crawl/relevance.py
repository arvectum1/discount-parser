from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from html.parser import HTMLParser
from typing import Mapping, Sequence
from urllib.parse import parse_qsl, unquote, urlsplit

from ..acquisition import AcquisitionEngine, AcquisitionRequest, RenderMode
from ..engine import FieldSpec, RawAsset
from ..execution import ExtractionJob
from .models import CrawlDiscoveryResult

_URL_RE = re.compile(r"(?i)https?://[^\s'\"<>]+")
_WS_RE = re.compile(r"\s+")
_MAX_ERROR_MESSAGE = 1000

_DISCOUNT_TERMS = (
    "промокод",
    "промокоды",
    "скидк",
    "купон",
    "акци",
    "предложен",
    "кэшбэк",
    "кешбэк",
    "promo code",
    "promocode",
    "coupon",
    "discount",
    "deal",
    "cashback",
)

_CTA_TERMS = (
    "активировать промокод",
    "показать промокод",
    "получить промокод",
    "скопировать промокод",
    "открыть акцию",
    "получить скидку",
    "show code",
    "get code",
    "copy code",
)

_STRONG_PATH_SEGMENTS = frozenset(
    {
        "promo",
        "promocode",
        "promocodes",
        "promokod",
        "promokody",
        "coupon",
        "coupons",
        "discount",
        "discounts",
        "deal",
        "deals",
        "offer",
        "offers",
        "sale",
        "sales",
        "akcii",
        "aktsii",
        "skidki",
    }
)

_MERCHANT_PATH_SEGMENTS = frozenset({"o", "shop", "store", "merchant", "brand"})

_HARD_NEGATIVE_SEGMENTS = frozenset(
    {
        "login",
        "auth",
        "register",
        "signup",
        "account",
        "profile",
        "cart",
        "basket",
        "checkout",
        "privacy",
        "policy",
        "terms",
        "agreement",
        "contacts",
        "contact",
        "about",
        "blog",
        "news",
        "search",
        "faq",
        "help",
        "support",
        "sitemap",
        "api",
    }
)

_NAVIGATION_SEGMENTS = frozenset(
    {
        "category",
        "categories",
        "catalog",
        "shops",
        "stores",
        "brands",
        "merchants",
        "page",
        "tag",
        "tags",
    }
)

_PAGINATION_QUERY_KEYS = frozenset(
    {
        "page",
        "p",
        "offset",
        "start",
        "sort",
        "order",
        "filter",
        "view",
    }
)

_GENERIC_TITLE_EXACT = frozenset(
    {
        "главная",
        "главная страница",
        "магазины",
        "все магазины",
        "категории",
        "все категории",
        "бренды",
        "все бренды",
        "поиск",
        "результаты поиска",
    }
)


class TargetPageStatus(StrEnum):
    TARGET = "target"
    CANDIDATE = "candidate"
    NON_TARGET = "non_target"
    UNPROBED = "unprobed"


@dataclass(frozen=True, slots=True)
class RelevanceEvidence:
    source: str
    signal: str
    weight: float
    detail: str = ""


@dataclass(frozen=True, slots=True)
class TargetPageAssessment:
    url: str
    status: TargetPageStatus
    score: float
    evidence: tuple[RelevanceEvidence, ...] = ()
    discovery_index: int = 0
    parent_url: str | None = None
    depth: int = 0
    probed: bool = False
    probe_error_type: str | None = None
    probe_error_message: str | None = None

    @property
    def selectable(self) -> bool:
        return self.status in {TargetPageStatus.TARGET, TargetPageStatus.CANDIDATE}


@dataclass(frozen=True, slots=True)
class TargetPagePolicy:
    target_threshold: float = 5.0
    candidate_threshold: float = 2.0
    max_probe_pages: int = 100
    max_selected_urls: int = 100
    include_seeds: bool = True
    render_mode: RenderMode = RenderMode.AUTO
    timeout_s: float = 15.0
    max_bytes: int = 1_000_000
    strong_path_segments: frozenset[str] = _STRONG_PATH_SEGMENTS
    merchant_path_segments: frozenset[str] = _MERCHANT_PATH_SEGMENTS
    hard_negative_segments: frozenset[str] = _HARD_NEGATIVE_SEGMENTS
    navigation_segments: frozenset[str] = _NAVIGATION_SEGMENTS
    pagination_query_keys: frozenset[str] = _PAGINATION_QUERY_KEYS
    discount_terms: tuple[str, ...] = _DISCOUNT_TERMS
    cta_terms: tuple[str, ...] = _CTA_TERMS
    generic_title_exact: frozenset[str] = _GENERIC_TITLE_EXACT

    def __post_init__(self) -> None:
        if self.target_threshold <= self.candidate_threshold:
            raise ValueError("target_threshold must be greater than candidate_threshold")
        if self.max_probe_pages < 1:
            raise ValueError("max_probe_pages must be >= 1")
        if self.max_selected_urls < 1:
            raise ValueError("max_selected_urls must be >= 1")
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if not isinstance(self.render_mode, RenderMode):
            object.__setattr__(self, "render_mode", RenderMode(self.render_mode))


@dataclass(frozen=True, slots=True)
class TargetPageDiscoveryResult:
    discovery: CrawlDiscoveryResult
    assessments: tuple[TargetPageAssessment, ...]
    max_selected_urls: int = 100
    limit_reasons: tuple[str, ...] = ()

    @property
    def truncated(self) -> bool:
        return bool(self.limit_reasons)

    def ranked(self) -> tuple[TargetPageAssessment, ...]:
        priority = {
            TargetPageStatus.TARGET: 0,
            TargetPageStatus.CANDIDATE: 1,
            TargetPageStatus.UNPROBED: 2,
            TargetPageStatus.NON_TARGET: 3,
        }
        return tuple(
            sorted(
                self.assessments,
                key=lambda item: (
                    priority[item.status],
                    -item.score,
                    item.discovery_index,
                ),
            )
        )

    def urls(
        self,
        *,
        include_candidates: bool = True,
        include_unprobed: bool = False,
    ) -> tuple[str, ...]:
        allowed = {TargetPageStatus.TARGET}
        if include_candidates:
            allowed.add(TargetPageStatus.CANDIDATE)
        if include_unprobed:
            allowed.add(TargetPageStatus.UNPROBED)
        values = [item.url for item in self.ranked() if item.status in allowed]
        return tuple(values[: self.max_selected_urls])

    def to_job(
        self,
        job_id: str,
        fields: Sequence[FieldSpec],
        *,
        include_candidates: bool = True,
        include_unprobed: bool = False,
    ) -> ExtractionJob:
        urls = self.urls(
            include_candidates=include_candidates,
            include_unprobed=include_unprobed,
        )
        if not urls:
            raise ValueError("Target-page classification produced no URLs for ExtractionJob")
        return ExtractionJob.from_urls(job_id, urls, fields)


@dataclass(frozen=True, slots=True)
class _PageSignals:
    title: str = ""
    h1: str = ""
    meta_description: str = ""
    visible_text: str = ""


class _SignalHTMLParser(HTMLParser):
    def __init__(self, *, max_visible_chars: int = 20_000) -> None:
        super().__init__(convert_charrefs=True)
        self.max_visible_chars = max_visible_chars
        self._ignored_depth = 0
        self._title_depth = 0
        self._h1_depth = 0
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self.meta_description = ""
        self.visible_parts: list[str] = []
        self.visible_chars = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        attrs_map = {str(k).casefold(): str(v or "") for k, v in attrs}
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1
            return
        if tag == "title":
            self._title_depth += 1
        elif tag == "h1":
            self._h1_depth += 1
        elif tag == "meta" and not self.meta_description:
            name = attrs_map.get("name", "").casefold()
            prop = attrs_map.get("property", "").casefold()
            if name == "description" or prop == "og:description":
                self.meta_description = attrs_map.get("content", "").strip()

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if tag in {"script", "style", "noscript", "svg"}:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if tag == "title" and self._title_depth:
            self._title_depth -= 1
        elif tag == "h1" and self._h1_depth:
            self._h1_depth -= 1

    def handle_data(self, data):
        if self._ignored_depth:
            return
        cleaned = _WS_RE.sub(" ", data).strip()
        if not cleaned:
            return
        if self._title_depth:
            self.title_parts.append(cleaned)
        if self._h1_depth:
            self.h1_parts.append(cleaned)
        if self.visible_chars < self.max_visible_chars:
            remaining = self.max_visible_chars - self.visible_chars
            chunk = cleaned[:remaining]
            if chunk:
                self.visible_parts.append(chunk)
                self.visible_chars += len(chunk) + 1

    def signals(self) -> _PageSignals:
        return _PageSignals(
            title=_WS_RE.sub(" ", " ".join(self.title_parts)).strip(),
            h1=_WS_RE.sub(" ", " ".join(self.h1_parts)).strip(),
            meta_description=self.meta_description,
            visible_text=_WS_RE.sub(" ", " ".join(self.visible_parts)).strip(),
        )


class TargetPageClassifier:
    """Bounded relevance classifier for Discount Parser crawl results."""

    def __init__(
        self,
        *,
        acquisition: AcquisitionEngine | None = None,
        policy: TargetPagePolicy | None = None,
    ) -> None:
        self.acquisition = acquisition or AcquisitionEngine()
        self.policy = policy or TargetPagePolicy()

    def classify(
        self,
        discovery: CrawlDiscoveryResult,
        *,
        headers: Mapping[str, str] | None = None,
        probe: bool = True,
    ) -> TargetPageDiscoveryResult:
        entries = self._entries(discovery)
        preliminary = [self._assess_link(*entry) for entry in entries]

        if not probe:
            assessments = tuple(
                item
                if item.status is TargetPageStatus.NON_TARGET
                else self._with_status(
                    item,
                    (
                        TargetPageStatus.TARGET
                        if item.score >= self.policy.target_threshold
                        else TargetPageStatus.CANDIDATE
                        if item.score >= self.policy.candidate_threshold
                        else TargetPageStatus.UNPROBED
                    ),
                )
                for item in preliminary
            )
            return TargetPageDiscoveryResult(
                discovery=discovery,
                assessments=assessments,
                max_selected_urls=self.policy.max_selected_urls,
            )

        probe_candidates = [
            item
            for item in preliminary
            if item.status is not TargetPageStatus.NON_TARGET
        ]
        probe_candidates.sort(key=lambda item: (-item.score, item.discovery_index))
        selected_for_probe = probe_candidates[: self.policy.max_probe_pages]
        probe_urls = {item.url for item in selected_for_probe}
        limit_reasons: list[str] = []
        if len(probe_candidates) > self.policy.max_probe_pages:
            limit_reasons.append("max_probe_pages")

        request_headers = {} if headers is None else dict(headers)
        final: list[TargetPageAssessment] = []
        for item in preliminary:
            if item.status is TargetPageStatus.NON_TARGET:
                final.append(item)
                continue
            if item.url not in probe_urls:
                final.append(self._with_status(item, TargetPageStatus.UNPROBED))
                continue
            final.append(self._probe(item, request_headers))

        ranked = TargetPageDiscoveryResult(
            discovery=discovery,
            assessments=tuple(final),
            max_selected_urls=self.policy.max_selected_urls,
            limit_reasons=tuple(limit_reasons),
        ).ranked()
        selectable = [
            item
            for item in ranked
            if item.status in {TargetPageStatus.TARGET, TargetPageStatus.CANDIDATE}
        ]
        if len(selectable) > self.policy.max_selected_urls:
            limit_reasons.append("max_selected_urls")

        return TargetPageDiscoveryResult(
            discovery=discovery,
            assessments=tuple(final),
            max_selected_urls=self.policy.max_selected_urls,
            limit_reasons=tuple(dict.fromkeys(limit_reasons)),
        )

    def _entries(
        self,
        discovery: CrawlDiscoveryResult,
    ) -> list[tuple[str, str | None, int, str, int]]:
        entries: list[tuple[str, str | None, int, str, int]] = []
        seen: set[str] = set()
        index = 0
        if self.policy.include_seeds:
            for url in discovery.seeds:
                if url in seen:
                    continue
                entries.append((url, None, 0, "", index))
                seen.add(url)
                index += 1
        for link in discovery.links:
            if link.url in seen:
                continue
            entries.append(
                (link.url, link.parent_url, link.depth, link.anchor_text, index)
            )
            seen.add(link.url)
            index += 1
        return entries

    def _assess_link(
        self,
        url: str,
        parent_url: str | None,
        depth: int,
        anchor_text: str,
        discovery_index: int,
    ) -> TargetPageAssessment:
        evidence: list[RelevanceEvidence] = []
        parsed = urlsplit(url)
        segments = tuple(
            unquote(segment).casefold()
            for segment in parsed.path.split("/")
            if segment
        )

        hard_negative = next(
            (segment for segment in segments if segment in self.policy.hard_negative_segments),
            None,
        )
        if hard_negative is not None:
            evidence.append(
                RelevanceEvidence(
                    "url_path",
                    "hard_negative_segment",
                    -100.0,
                    hard_negative,
                )
            )
            return TargetPageAssessment(
                url=url,
                status=TargetPageStatus.NON_TARGET,
                score=-100.0,
                evidence=tuple(evidence),
                discovery_index=discovery_index,
                parent_url=parent_url,
                depth=depth,
            )

        score = 0.0
        strong = sorted(set(segments) & self.policy.strong_path_segments)
        if strong:
            weight = min(5.0, 3.5 + 0.5 * (len(strong) - 1))
            score += weight
            evidence.append(
                RelevanceEvidence("url_path", "discount_path", weight, ",".join(strong))
            )

        merchant = sorted(set(segments) & self.policy.merchant_path_segments)
        if merchant:
            weight = 1.25
            score += weight
            evidence.append(
                RelevanceEvidence("url_path", "merchant_path", weight, ",".join(merchant))
            )

        navigation = sorted(set(segments) & self.policy.navigation_segments)
        if navigation:
            weight = -1.5
            score += weight
            evidence.append(
                RelevanceEvidence("url_path", "navigation_path", weight, ",".join(navigation))
            )

        query_keys = {key.casefold() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
        pagination = sorted(query_keys & self.policy.pagination_query_keys)
        if pagination:
            weight = -2.0
            score += weight
            evidence.append(
                RelevanceEvidence("url_query", "pagination_or_view", weight, ",".join(pagination))
            )

        anchor_norm = _normalize(anchor_text)
        anchor_hits = _matched_terms(anchor_norm, self.policy.discount_terms)
        if anchor_hits:
            weight = min(2.0, 0.75 * len(anchor_hits))
            score += weight
            evidence.append(
                RelevanceEvidence("anchor_text", "discount_terms", weight, ",".join(anchor_hits))
            )

        return TargetPageAssessment(
            url=url,
            status=TargetPageStatus.UNPROBED,
            score=round(score, 6),
            evidence=tuple(evidence),
            discovery_index=discovery_index,
            parent_url=parent_url,
            depth=depth,
        )

    def _probe(
        self,
        preliminary: TargetPageAssessment,
        headers: Mapping[str, str],
    ) -> TargetPageAssessment:
        try:
            acquired = self.acquisition.acquire(
                AcquisitionRequest(
                    url=preliminary.url,
                    headers=dict(headers),
                    timeout_s=self.policy.timeout_s,
                    max_bytes=self.policy.max_bytes,
                    render_mode=self.policy.render_mode,
                )
            )
        except Exception as exc:
            message = _URL_RE.sub("<url>", str(exc))
            if len(message) > _MAX_ERROR_MESSAGE:
                message = message[: _MAX_ERROR_MESSAGE - 3] + "..."
            status = self._status_for_score(preliminary.score)
            if status is TargetPageStatus.NON_TARGET:
                status = TargetPageStatus.CANDIDATE
            return TargetPageAssessment(
                url=preliminary.url,
                status=status,
                score=preliminary.score,
                evidence=preliminary.evidence,
                discovery_index=preliminary.discovery_index,
                parent_url=preliminary.parent_url,
                depth=preliminary.depth,
                probed=True,
                probe_error_type=type(exc).__name__,
                probe_error_message=message,
            )

        content_evidence, content_score = self._content_evidence(acquired.asset)
        score = round(preliminary.score + content_score, 6)
        return TargetPageAssessment(
            url=preliminary.url,
            status=self._status_for_score(score),
            score=score,
            evidence=preliminary.evidence + tuple(content_evidence),
            discovery_index=preliminary.discovery_index,
            parent_url=preliminary.parent_url,
            depth=preliminary.depth,
            probed=True,
        )

    def _content_evidence(
        self,
        asset: RawAsset,
    ) -> tuple[list[RelevanceEvidence], float]:
        signals = _signals_from_asset(asset)
        evidence: list[RelevanceEvidence] = []
        score = 0.0

        title_norm = _normalize(signals.title)
        h1_norm = _normalize(signals.h1)
        meta_norm = _normalize(signals.meta_description)
        visible_norm = _normalize(signals.visible_text)

        if title_norm in self.policy.generic_title_exact:
            score -= 2.5
            evidence.append(
                RelevanceEvidence("title", "generic_exact", -2.5, title_norm)
            )

        for source, text, weight in (
            ("title", title_norm, 2.0),
            ("h1", h1_norm, 2.0),
            ("meta", meta_norm, 2.0),
        ):
            hits = _matched_terms(text, self.policy.discount_terms)
            if hits:
                score += weight
                evidence.append(
                    RelevanceEvidence(source, "discount_terms", weight, ",".join(hits))
                )

        visible_hits = _matched_terms(visible_norm, self.policy.discount_terms)
        if visible_hits:
            weight = min(3.0, 0.5 * len(visible_hits))
            score += weight
            evidence.append(
                RelevanceEvidence(
                    "visible_text",
                    "discount_term_diversity",
                    weight,
                    ",".join(visible_hits),
                )
            )

        occurrence_count = sum(
            visible_norm.count(term) for term in self.policy.discount_terms if term
        )
        if occurrence_count >= 8:
            weight = 2.0
            score += weight
            evidence.append(
                RelevanceEvidence(
                    "visible_text",
                    "discount_term_density",
                    weight,
                    "occurrences>=8",
                )
            )
        elif occurrence_count >= 3:
            weight = 1.0
            score += weight
            evidence.append(
                RelevanceEvidence(
                    "visible_text",
                    "discount_term_density",
                    weight,
                    "occurrences>=3",
                )
            )

        cta_hits = _matched_terms(visible_norm, self.policy.cta_terms)
        if cta_hits:
            weight = min(1.5, 0.75 * len(cta_hits))
            score += weight
            evidence.append(
                RelevanceEvidence("visible_text", "offer_cta", weight, ",".join(cta_hits))
            )

        return evidence, round(score, 6)

    def _status_for_score(self, score: float) -> TargetPageStatus:
        if score >= self.policy.target_threshold:
            return TargetPageStatus.TARGET
        if score >= self.policy.candidate_threshold:
            return TargetPageStatus.CANDIDATE
        return TargetPageStatus.NON_TARGET

    @staticmethod
    def _with_status(
        assessment: TargetPageAssessment,
        status: TargetPageStatus,
    ) -> TargetPageAssessment:
        return TargetPageAssessment(
            url=assessment.url,
            status=status,
            score=assessment.score,
            evidence=assessment.evidence,
            discovery_index=assessment.discovery_index,
            parent_url=assessment.parent_url,
            depth=assessment.depth,
            probed=assessment.probed,
            probe_error_type=assessment.probe_error_type,
            probe_error_message=assessment.probe_error_message,
        )


def _normalize(value: str) -> str:
    value = (value or "").casefold().replace("ё", "е")
    return _WS_RE.sub(" ", value).strip()


def _matched_terms(value: str, terms: Sequence[str]) -> tuple[str, ...]:
    return tuple(term for term in terms if term and term in value)


def _signals_from_asset(asset: RawAsset) -> _PageSignals:
    if asset.html:
        parser = _SignalHTMLParser()
        try:
            parser.feed(asset.html)
            parser.close()
            return parser.signals()
        except Exception:
            pass
    return _PageSignals(visible_text=(asset.text or "")[:20_000])
