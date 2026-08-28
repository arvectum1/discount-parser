from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping
from urllib.parse import urlsplit

from arvectum_data.engine.models import RawAsset


class RenderMode(StrEnum):
    AUTO = "auto"
    NEVER = "never"
    ALWAYS = "always"


class AcquisitionError(RuntimeError):
    """Raised when no acquisition path can produce a usable source asset."""


class MissingBrowserDependencyError(AcquisitionError):
    """Raised when the optional browser renderer is requested but unavailable."""


@dataclass(frozen=True, slots=True)
class AcquisitionRequest:
    url: str
    asset_id: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout_s: float = 20.0
    max_bytes: int = 5_000_000
    render_mode: RenderMode = RenderMode.AUTO

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
            raise ValueError("AcquisitionRequest.url must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("URL-embedded credentials are not supported")
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        object.__setattr__(self, "headers", dict(self.headers))
        if not isinstance(self.render_mode, RenderMode):
            object.__setattr__(self, "render_mode", RenderMode(self.render_mode))

    @property
    def resolved_asset_id(self) -> str:
        if self.asset_id:
            return self.asset_id
        digest = hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:20]
        return f"url-{digest}"


@dataclass(frozen=True, slots=True)
class PageSnapshot:
    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)
    rendered: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.body, bytes):
            raise TypeError("PageSnapshot.body must be bytes")
        if not 100 <= self.status_code <= 599:
            raise ValueError("PageSnapshot.status_code must be between 100 and 599")
        object.__setattr__(self, "headers", dict(self.headers))


@dataclass(frozen=True, slots=True)
class AcquisitionAttempt:
    method: str
    success: bool
    reason: str
    status_code: int | None = None
    final_url: str | None = None
    rendered: bool = False


@dataclass(frozen=True, slots=True)
class AcquisitionResult:
    asset: RawAsset
    attempts: tuple[AcquisitionAttempt, ...]
    warnings: tuple[str, ...] = ()

    @property
    def used_renderer(self) -> bool:
        return any(attempt.success and attempt.rendered for attempt in self.attempts)
