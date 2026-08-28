from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from ..acquisition import RenderMode
from ..engine import FieldSpec

if TYPE_CHECKING:
    from ..execution import ExtractionJob

_MAX_ERROR_MESSAGE = 1000
_URL_RE = re.compile(r"(?i)https?://[^\s\'\"<>]+")

DEFAULT_BLOCKED_SUFFIXES = (
    ".7z", ".avi", ".bmp", ".css", ".csv", ".doc", ".docx", ".eot",
    ".gif", ".gz", ".ico", ".jpeg", ".jpg", ".js", ".json", ".m4a",
    ".m4v", ".mov", ".mp3", ".mp4", ".mpeg", ".mpg", ".odp", ".ods",
    ".odt", ".pdf", ".png", ".ppt", ".pptx", ".rar", ".rss", ".svg",
    ".tar", ".tgz", ".tif", ".tiff", ".ttf", ".wav", ".webm", ".webp",
    ".woff", ".woff2", ".xls", ".xlsx", ".xml", ".zip",
)


@dataclass(frozen=True, slots=True)
class CrawlPolicy:
    max_pages: int = 50
    max_depth: int = 1
    max_discovered_urls: int = 500
    max_links_per_page: int = 250
    same_origin: bool = True
    allowed_hosts: tuple[str, ...] = ()
    respect_nofollow: bool = True
    blocked_suffixes: tuple[str, ...] = DEFAULT_BLOCKED_SUFFIXES
    render_mode: RenderMode = RenderMode.AUTO
    timeout_s: float = 20.0
    max_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        if self.max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        if self.max_depth < 0:
            raise ValueError("max_depth must be >= 0")
        if self.max_discovered_urls < 1:
            raise ValueError("max_discovered_urls must be >= 1")
        if self.max_links_per_page < 1:
            raise ValueError("max_links_per_page must be >= 1")
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if not isinstance(self.render_mode, RenderMode):
            object.__setattr__(self, "render_mode", RenderMode(self.render_mode))
        cleaned_hosts = tuple(
            host.strip().casefold().rstrip(".")
            for host in self.allowed_hosts
            if host.strip()
        )
        if len(cleaned_hosts) != len(set(cleaned_hosts)):
            raise ValueError("allowed_hosts must be unique")
        if not self.same_origin and not cleaned_hosts:
            raise ValueError(
                "allowed_hosts is required when same_origin=False to prevent unbounded cross-origin crawl"
            )
        object.__setattr__(self, "allowed_hosts", cleaned_hosts)
        suffixes = tuple(
            value.strip().casefold()
            for value in self.blocked_suffixes
            if value.strip()
        )
        object.__setattr__(self, "blocked_suffixes", suffixes)


@dataclass(frozen=True, slots=True)
class CrawlLink:
    url: str
    parent_url: str
    depth: int
    anchor_text: str = ""
    rel: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.depth < 1:
            raise ValueError("CrawlLink.depth must be >= 1")
        object.__setattr__(self, "rel", tuple(self.rel))


@dataclass(frozen=True, slots=True)
class CrawlPageRecord:
    url: str
    final_url: str
    depth: int
    rendered: bool
    discovered_links: int
    scope_allowed: bool = True


@dataclass(frozen=True, slots=True)
class CrawlFailure:
    url: str
    depth: int
    error_type: str
    message: str

    @classmethod
    def from_exception(cls, url: str, depth: int, exc: Exception) -> "CrawlFailure":
        message = _URL_RE.sub("<url>", str(exc))
        if len(message) > _MAX_ERROR_MESSAGE:
            message = message[: _MAX_ERROR_MESSAGE - 3] + "..."
        return cls(url=url, depth=depth, error_type=type(exc).__name__, message=message)


@dataclass(frozen=True, slots=True)
class CrawlDiscoveryResult:
    seeds: tuple[str, ...]
    links: tuple[CrawlLink, ...]
    pages: tuple[CrawlPageRecord, ...]
    failures: tuple[CrawlFailure, ...] = ()
    limit_reasons: tuple[str, ...] = ()

    @property
    def truncated(self) -> bool:
        return bool(self.limit_reasons)

    def urls(self, *, include_seeds: bool = False) -> tuple[str, ...]:
        values: list[str] = list(self.seeds) if include_seeds else []
        seen = set(values)
        for link in self.links:
            if link.url not in seen:
                values.append(link.url)
                seen.add(link.url)
        return tuple(values)

    def to_job(
        self,
        job_id: str,
        fields: Sequence[FieldSpec],
        *,
        include_seeds: bool = False,
    ) -> "ExtractionJob":
        from ..execution import ExtractionJob

        urls = self.urls(include_seeds=include_seeds)
        if not urls:
            raise ValueError("Crawl discovery produced no URLs for ExtractionJob")
        return ExtractionJob.from_urls(job_id, urls, fields)
