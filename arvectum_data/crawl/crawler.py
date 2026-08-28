from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit

from ..acquisition import AcquisitionEngine, AcquisitionRequest
from .links import canonicalize_url, extract_anchors, origin_key
from .models import (
    CrawlDiscoveryResult,
    CrawlFailure,
    CrawlLink,
    CrawlPageRecord,
    CrawlPolicy,
)


class URLDiscoveryCrawler:
    """Bounded deterministic breadth-first URL discovery over generic HTML links."""

    def __init__(
        self,
        *,
        acquisition: AcquisitionEngine | None = None,
        policy: CrawlPolicy | None = None,
    ) -> None:
        self.acquisition = acquisition or AcquisitionEngine()
        self.policy = policy or CrawlPolicy()

    def discover(
        self,
        seeds: Sequence[str],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> CrawlDiscoveryResult:
        if not seeds:
            raise ValueError("at least one seed URL is required")

        canonical_seeds: list[str] = []
        seed_seen: set[str] = set()
        for raw in seeds:
            canonical = canonicalize_url(raw, raw)
            if canonical is None:
                raise ValueError(f"invalid seed URL: {raw!r}")
            if canonical not in seed_seen:
                canonical_seeds.append(canonical)
                seed_seen.add(canonical)
        if not canonical_seeds:
            raise ValueError("at least one valid seed URL is required")

        seed_origins = {origin_key(url) for url in canonical_seeds}
        queue = deque((url, 0) for url in canonical_seeds)
        queued = set(canonical_seeds)
        visited: set[str] = set()
        resolved_pages: set[str] = set()
        known = set(canonical_seeds)

        discovered: list[CrawlLink] = []
        pages: list[CrawlPageRecord] = []
        failures: list[CrawlFailure] = []
        limit_reasons: list[str] = []
        request_headers = {} if headers is None else dict(headers)

        while queue:
            if len(visited) >= self.policy.max_pages:
                self._add_limit(limit_reasons, "max_pages")
                break
            url, depth = queue.popleft()
            queued.discard(url)
            if url in visited or url in resolved_pages:
                continue
            visited.add(url)

            try:
                acquired = self.acquisition.acquire(
                    AcquisitionRequest(
                        url=url,
                        headers=request_headers,
                        timeout_s=self.policy.timeout_s,
                        max_bytes=self.policy.max_bytes,
                        render_mode=self.policy.render_mode,
                    )
                )
            except Exception as exc:
                failures.append(CrawlFailure.from_exception(url, depth, exc))
                continue

            final_url = canonicalize_url(url, acquired.asset.source_url or url) or url
            resolved_pages.add(final_url)
            known.add(final_url)
            scope_allowed = self._scope_allowed(final_url, seed_origins)
            page_links = 0

            if scope_allowed and acquired.asset.html is not None and depth < self.policy.max_depth:
                base_href, anchors = extract_anchors(
                    acquired.asset.html,
                    max_links=self.policy.max_links_per_page,
                )
                base_url = final_url
                if base_href:
                    candidate_base = canonicalize_url(final_url, base_href)
                    if candidate_base is not None:
                        base_url = candidate_base

                for anchor in anchors:
                    if self.policy.respect_nofollow and "nofollow" in anchor.rel:
                        continue
                    candidate = canonicalize_url(base_url, anchor.href)
                    if candidate is None:
                        continue
                    if not self._scope_allowed(candidate, seed_origins):
                        continue
                    if self._blocked_by_suffix(candidate):
                        continue
                    if candidate in known:
                        continue
                    if len(discovered) >= self.policy.max_discovered_urls:
                        self._add_limit(limit_reasons, "max_discovered_urls")
                        break

                    link = CrawlLink(
                        url=candidate,
                        parent_url=final_url,
                        depth=depth + 1,
                        anchor_text=anchor.text,
                        rel=anchor.rel,
                    )
                    discovered.append(link)
                    known.add(candidate)
                    page_links += 1

                    if (
                        depth + 1 <= self.policy.max_depth
                        and candidate not in visited
                        and candidate not in resolved_pages
                        and candidate not in queued
                    ):
                        queue.append((candidate, depth + 1))
                        queued.add(candidate)

            pages.append(
                CrawlPageRecord(
                    url=url,
                    final_url=final_url,
                    depth=depth,
                    rendered=acquired.used_renderer,
                    discovered_links=page_links,
                    scope_allowed=scope_allowed,
                )
            )
            if "max_discovered_urls" in limit_reasons:
                break

        if queue and len(visited) >= self.policy.max_pages:
            self._add_limit(limit_reasons, "max_pages")

        return CrawlDiscoveryResult(
            seeds=tuple(canonical_seeds),
            links=tuple(discovered),
            pages=tuple(pages),
            failures=tuple(failures),
            limit_reasons=tuple(limit_reasons),
        )

    def _scope_allowed(
        self,
        url: str,
        seed_origins: set[tuple[str, str, int | None]],
    ) -> bool:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        if origin_key(url) in seed_origins:
            return True
        if self.policy.same_origin:
            return False
        return host in self.policy.allowed_hosts

    def _blocked_by_suffix(self, url: str) -> bool:
        path = urlsplit(url).path.casefold()
        return any(path.endswith(suffix) for suffix in self.policy.blocked_suffixes)

    @staticmethod
    def _add_limit(reasons: list[str], reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)
