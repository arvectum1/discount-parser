from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from typing import Callable, Mapping
from urllib.parse import urlparse

import httpx

from arvectum_data.acquisition import (
    AcquisitionEngine,
    AcquisitionRequest,
    PageSnapshot,
    RenderMode,
)
from arvectum_data.crawl import (
    CrawlDiscoveryResult,
    CrawlLink,
    CrawlPolicy,
    TargetPageAssessment,
    TargetPageClassifier,
    TargetPagePolicy,
    URLDiscoveryCrawler,
)
from src.modules.source_registry.known_site_crawl import discover_promokood_detail_urls
from src.shared.network import NetworkRouteError, network_router
from src.sources.base import RawOffer
from src.sources.config import SourceConfig
from src.sources.generic_multi_record import (
    GenericMultiRecordOfferDecoder,
    SourceParityReport,
    compare_offer_sets,
)
from src.sources.registry import build_adapter


_URL_RE = re.compile(r"(?i)https?://[^\s'\"<>]+")
_MAX_DIAGNOSTIC = 1000
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru,en;q=0.8",
}


def _diagnostic(exc: Exception) -> str:
    message = _URL_RE.sub("<url>", str(exc))
    value = f"{type(exc).__name__}: {message}"
    return value if len(value) <= _MAX_DIAGNOSTIC else value[: _MAX_DIAGNOSTIC - 3] + "..."


@dataclass(frozen=True, slots=True)
class ProductionSourcePolicy:
    """Conservative customer-runtime bounds above DP-ENGINE-011/012."""

    crawl_max_pages: int = 16
    crawl_max_depth: int = 1
    crawl_max_discovered_urls: int = 300
    crawl_max_links_per_page: int = 250
    target_max_probe_pages: int = 40
    target_max_selected_urls: int = 40
    timeout_s: float = 20.0
    max_bytes: int = 5_000_000

    def __post_init__(self) -> None:
        positive = (
            self.crawl_max_pages,
            self.crawl_max_discovered_urls,
            self.crawl_max_links_per_page,
            self.target_max_probe_pages,
            self.target_max_selected_urls,
        )
        if any(value < 1 for value in positive):
            raise ValueError("production source bounds must be >= 1")
        if self.crawl_max_depth < 0:
            raise ValueError("crawl_max_depth must be >= 0")
        if self.timeout_s <= 0 or self.max_bytes <= 0:
            raise ValueError("timeout_s and max_bytes must be positive")


@dataclass(frozen=True, slots=True)
class SourceCollectionResult:
    offers: tuple[RawOffer, ...]
    runtime_mode: str
    discovered_urls: int = 0
    selected_urls: int = 0
    decoded_pages: int = 0
    fallback_used: bool = False
    warnings: tuple[str, ...] = ()
    generic_pages: int = 0
    legacy_pages: int = 0
    parity_failures: int = 0


class DiscountParserHTTPTransport:
    """DP Engine HTTP transport backed by Discount Parser's governed router.

    The generic engine deliberately knows nothing about the customer's Windows
    WinINet/VPN/proxy routing. Production source runs must therefore adapt the
    application-owned ``network_router`` into the DP ``HTTPTransport`` contract.

    Successful responses are cached only for the lifetime of one source run so
    crawl, relevance probing and decoding do not repeatedly download the same
    page. No cache survives the run and no credentials are persisted.
    """

    name = "discount-parser-network-router"

    def __init__(
        self,
        *,
        network_policy: str = "auto",
        retries: int = 3,
        retry_backoff_s: float = 0.5,
    ) -> None:
        if network_policy not in {"auto", "direct", "proxy", "system"}:
            raise ValueError(f"unsupported network policy: {network_policy}")
        if retries < 1:
            raise ValueError("retries must be >= 1")
        self.network_policy = network_policy
        self.retries = retries
        self.retry_backoff_s = max(0.0, retry_backoff_s)
        self.requests_made = 0
        self.cache_hits = 0
        self._snapshots: dict[tuple[str, tuple[tuple[str, str], ...]], PageSnapshot] = {}
        self._html: dict[str, str] = {}

    @staticmethod
    def _cache_key(request: AcquisitionRequest) -> tuple[str, tuple[tuple[str, str], ...]]:
        return request.url, tuple(sorted((str(k), str(v)) for k, v in request.headers.items()))

    def fetch(self, request: AcquisitionRequest) -> PageSnapshot:
        key = self._cache_key(request)
        cached = self._snapshots.get(key)
        if cached is not None:
            self.cache_hits += 1
            return cached

        headers = dict(_DEFAULT_HEADERS)
        headers.update({str(k): str(v) for k, v in request.headers.items()})
        retry_statuses = {403, 451} if self.network_policy == "auto" else set()
        last_error: Exception | None = None

        for attempt in range(self.retries):
            try:
                self.requests_made += 1
                response = network_router.get(
                    request.url,
                    route=self.network_policy,
                    retry_statuses=retry_statuses,
                    timeout=request.timeout_s,
                    headers=headers,
                    follow_redirects=True,
                )
                response.raise_for_status()
                body = bytes(response.content)
                if len(body) > request.max_bytes:
                    raise RuntimeError(f"response exceeds max_bytes={request.max_bytes}")
                snapshot = PageSnapshot(
                    requested_url=request.url,
                    final_url=str(response.url),
                    status_code=int(response.status_code),
                    content_type=str(response.headers.get("content-type") or ""),
                    body=body,
                    headers=dict(response.headers),
                    rendered=False,
                )
                self._snapshots[key] = snapshot
                content_type = snapshot.content_type.casefold()
                if "html" in content_type or "xhtml" in content_type:
                    text = response.text
                    self._html[request.url] = text
                    self._html[str(response.url)] = text
                return snapshot
            except (httpx.HTTPError, NetworkRouteError, RuntimeError) as exc:
                last_error = exc
                if attempt + 1 < self.retries and self.retry_backoff_s:
                    time.sleep(self.retry_backoff_s * (2**attempt))
        assert last_error is not None
        raise last_error

    def cached_html_items(self) -> tuple[tuple[str, str], ...]:
        return tuple(self._html.items())


AdapterFactory = Callable[[SourceConfig], object]


class ProductionSourceRuntime:
    """Governed production bridge from generic discovery to multi-offer records.

    DP-ENGINE-016 runs the generic DP-014 multi-record decoder on each already
    acquired target page. During migration, the existing source adapter parses
    that same in-memory HTML as a safety oracle. Generic records are returned to
    production only when the page-level parity check proves they preserve every
    legacy-populated business field and the exact record identity set.

    A mismatch, review-required/incomplete generic record, or generic exception
    automatically selects the legacy decode for that page. This adds no network
    request and exposes no site-specific control to the customer. The adapter is
    therefore a temporary migration oracle rather than the default returned
    decoder whenever parity succeeds.
    """

    def __init__(
        self,
        config: SourceConfig,
        *,
        policy: ProductionSourcePolicy | None = None,
        transport: DiscountParserHTTPTransport | None = None,
        adapter_factory: AdapterFactory = build_adapter,
        generic_decoder: GenericMultiRecordOfferDecoder | None = None,
    ) -> None:
        self.config = config
        self.policy = policy or ProductionSourcePolicy()
        self.transport = transport or DiscountParserHTTPTransport(
            network_policy=config.network_policy
        )
        self.adapter_factory = adapter_factory
        self.generic_decoder = generic_decoder or GenericMultiRecordOfferDecoder()
        # Browser execution is deliberately not forced into the shipped client.
        # Static acquisition remains routed through the application's proxy/VPN
        # logic; optional browser packaging can be added later without changing
        # this source-runtime contract.
        self.acquisition = AcquisitionEngine(http=self.transport, renderer=None)

    def collect(self) -> SourceCollectionResult:
        if self.config.runtime_mode != "hybrid":
            offers = tuple(self.adapter_factory(self.config).collect())
            return SourceCollectionResult(offers=offers, runtime_mode="legacy")

        warnings: list[str] = []
        try:
            crawler = URLDiscoveryCrawler(
                acquisition=self.acquisition,
                policy=CrawlPolicy(
                    max_pages=self.policy.crawl_max_pages,
                    max_depth=self.policy.crawl_max_depth,
                    max_discovered_urls=self.policy.crawl_max_discovered_urls,
                    max_links_per_page=self.policy.crawl_max_links_per_page,
                    same_origin=True,
                    render_mode=RenderMode.AUTO,
                    timeout_s=self.policy.timeout_s,
                    max_bytes=self.policy.max_bytes,
                ),
            )
            discovery = crawler.discover([self.config.base_url])
            discovery = self._supplement_known_site_discovery(discovery)
            classifier = TargetPageClassifier(
                acquisition=self.acquisition,
                policy=TargetPagePolicy(
                    max_probe_pages=self.policy.target_max_probe_pages,
                    max_selected_urls=self.policy.target_max_selected_urls,
                    include_seeds=True,
                    render_mode=RenderMode.AUTO,
                    timeout_s=self.policy.timeout_s,
                    max_bytes=self.policy.max_bytes,
                ),
            )
            relevance = classifier.classify(discovery)
            selected = relevance.urls(include_candidates=True)
            (
                offers,
                decoded_pages,
                decode_warnings,
                generic_pages,
                legacy_pages,
                parity_failures,
            ) = self._decode_selected(selected, relevance.assessments)
            warnings.extend(decode_warnings)
            if offers:
                return SourceCollectionResult(
                    offers=tuple(offers),
                    runtime_mode="hybrid",
                    discovered_urls=len(discovery.urls(include_seeds=True)),
                    selected_urls=len(selected),
                    decoded_pages=decoded_pages,
                    warnings=tuple(warnings),
                    generic_pages=generic_pages,
                    legacy_pages=legacy_pages,
                    parity_failures=parity_failures,
                )
            warnings.append("engine_no_offers:legacy_fallback")
        except Exception as exc:
            warnings.append(f"engine_failed:legacy_fallback:{_diagnostic(exc)}")

        legacy = tuple(self.adapter_factory(self.config).collect())
        return SourceCollectionResult(
            offers=legacy,
            runtime_mode="hybrid",
            fallback_used=True,
            warnings=tuple(warnings),
        )

    def _supplement_known_site_discovery(
        self,
        discovery: CrawlDiscoveryResult,
    ) -> CrawlDiscoveryResult:
        host = (urlparse(self.config.base_url).hostname or "").casefold().removeprefix("www.")
        if host != "promokood.ru":
            return discovery

        known = set(discovery.urls(include_seeds=True))
        links = list(discovery.links)
        limit_reasons = list(discovery.limit_reasons)
        remaining = max(0, self.policy.crawl_max_discovered_urls - len(links))
        if not remaining:
            return discovery

        for parent_url, html_text in self.transport.cached_html_items():
            parent_host = (urlparse(parent_url).hostname or "").casefold().removeprefix("www.")
            if parent_host != host:
                continue
            for url in discover_promokood_detail_urls(
                html_text,
                entry_url=parent_url,
                limit=remaining,
            ):
                if url in known:
                    continue
                links.append(
                    CrawlLink(
                        url=url,
                        parent_url=parent_url,
                        depth=1,
                        anchor_text="promokood internal offer page",
                    )
                )
                known.add(url)
                remaining -= 1
                if not remaining:
                    limit_reasons.append("max_discovered_urls")
                    break
            if not remaining:
                break

        return CrawlDiscoveryResult(
            seeds=discovery.seeds,
            links=tuple(links),
            pages=discovery.pages,
            failures=discovery.failures,
            limit_reasons=tuple(dict.fromkeys(limit_reasons)),
        )

    def _decode_selected(
        self,
        selected: tuple[str, ...],
        assessments: tuple[TargetPageAssessment, ...],
    ) -> tuple[list[RawOffer], int, list[str], int, int, int]:
        by_url = {item.url: item for item in assessments}
        offers: list[RawOffer] = []
        warnings: list[str] = []
        seen: set[tuple[str, str]] = set()
        decoded_pages = 0
        generic_pages = 0
        legacy_pages = 0
        parity_failures = 0

        for page_url in selected:
            try:
                acquired = self.acquisition.acquire(
                    AcquisitionRequest(
                        url=page_url,
                        timeout_s=self.policy.timeout_s,
                        max_bytes=self.policy.max_bytes,
                        render_mode=RenderMode.AUTO,
                    )
                )
                html = acquired.asset.html
                if not html:
                    continue
                effective_url = acquired.asset.source_url or page_url
                page_config = replace(
                    self.config,
                    base_url=effective_url,
                    runtime_mode="legacy",
                )
                adapter = self.adapter_factory(page_config)
                parser = getattr(adapter, "parse", None)
                if not callable(parser):
                    warnings.append(f"decoder_missing:{page_url}")
                    continue

                legacy_decoded = list(parser(html))
                decoded_pages += 1
                chosen = legacy_decoded
                decoder_name = "legacy_adapter"
                parity: SourceParityReport | None = None

                try:
                    generic = self.generic_decoder.decode(
                        html,
                        page_url=effective_url,
                        source_key=self.config.key,
                    )
                    if generic.usable:
                        parity = compare_offer_sets(legacy_decoded, generic.offers)
                        if parity.safe_to_adopt:
                            chosen = list(generic.offers)
                            decoder_name = "generic_multi_record"
                            generic_pages += 1
                        else:
                            legacy_pages += 1
                            parity_failures += 1
                            warnings.append(
                                f"generic_parity_fallback:{page_url}:{parity.diagnostic()}"
                            )
                    else:
                        legacy_pages += 1
                        parity_failures += 1
                        detail = ",".join(generic.warnings[:4]) or "no_ready_records"
                        warnings.append(f"generic_not_ready_fallback:{page_url}:{detail}")
                except Exception as exc:
                    legacy_pages += 1
                    parity_failures += 1
                    warnings.append(f"generic_decode_fallback:{_diagnostic(exc)}")

                assessment = by_url.get(page_url)
                for raw in chosen:
                    identity = (raw.source_key, raw.external_id)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    offers.append(
                        self._with_engine_provenance(
                            raw,
                            page_url,
                            assessment,
                            decoder=decoder_name,
                            parity=parity,
                        )
                    )
            except Exception as exc:
                warnings.append(f"page_decode_failed:{_diagnostic(exc)}")
        return offers, decoded_pages, warnings, generic_pages, legacy_pages, parity_failures

    @staticmethod
    def _with_engine_provenance(
        raw: RawOffer,
        page_url: str,
        assessment: TargetPageAssessment | None,
        *,
        decoder: str,
        parity: SourceParityReport | None,
    ) -> RawOffer:
        payload = dict(raw.raw_payload or {})
        evidence = []
        if assessment is not None:
            evidence = [
                {
                    "source": item.source,
                    "signal": item.signal,
                    "weight": item.weight,
                    "detail": item.detail,
                }
                for item in assessment.evidence[:12]
            ]
        engine_meta = dict(payload.get("dp_engine") or {})
        engine_meta.update(
            {
                "runtime": "hybrid",
                "decoder": decoder,
                "page_url": page_url,
                "target_status": assessment.status.value if assessment else None,
                "target_score": assessment.score if assessment else None,
                "probed": assessment.probed if assessment else None,
                "evidence": evidence,
                "parity_safe": parity.safe_to_adopt if parity is not None else None,
                "parity_legacy_count": parity.legacy_count if parity is not None else None,
                "parity_generic_count": parity.generic_count if parity is not None else None,
            }
        )
        payload["dp_engine"] = engine_meta
        return replace(raw, raw_payload=payload)


def collect_source_offers(
    config: SourceConfig,
    *,
    policy: ProductionSourcePolicy | None = None,
    transport: DiscountParserHTTPTransport | None = None,
    adapter_factory: AdapterFactory = build_adapter,
) -> SourceCollectionResult:
    return ProductionSourceRuntime(
        config,
        policy=policy,
        transport=transport,
        adapter_factory=adapter_factory,
    ).collect()
