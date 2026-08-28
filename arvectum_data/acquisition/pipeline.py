from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Sequence
from urllib.parse import urlsplit

from arvectum_data.engine.models import RawAsset

from .http import UrllibHTTPTransport
from .models import (
    AcquisitionAttempt,
    AcquisitionError,
    AcquisitionRequest,
    AcquisitionResult,
    PageSnapshot,
    RenderMode,
)
from .protocols import HTTPTransport, PageRenderer
from .render import PlaywrightRenderer


_DEFAULT_RENDERER = object()


_CHARSET_RE = re.compile(r"charset\s*=\s*['\"]?([^;\s'\"]+)", re.I)
_JS_ROOT_RE = re.compile(
    r"(?:id|data-reactroot)\s*=\s*['\"](?:root|app|__next|__nuxt)['\"]",
    re.I,
)


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.script_count = 0
        self._ignored_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        if tag == "script":
            self.script_count += 1
            self._ignored_depth += 1
        elif tag in {"style", "noscript"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag):
        if tag.casefold() in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data):
        if not self._ignored_depth:
            cleaned = " ".join(data.split())
            if cleaned:
                self.parts.append(cleaned)


class DefaultRenderPolicy:
    """Detect obvious client-rendered shells without domain-specific selectors."""

    def __init__(self, *, min_visible_chars: int = 80, min_scripts: int = 2) -> None:
        self.min_visible_chars = min_visible_chars
        self.min_scripts = min_scripts

    def reason(self, snapshot: PageSnapshot) -> str | None:
        if not _is_html(snapshot.content_type, snapshot.body):
            return None
        html = _decode(snapshot)
        parser = _VisibleTextParser()
        try:
            parser.feed(html)
            parser.close()
        except Exception:
            return None
        visible = " ".join(parser.parts).strip()
        lower = html.casefold()
        explicit_js_message = (
            "enable javascript" in lower
            or "javascript is required" in lower
            or "you need to enable javascript" in lower
        )
        shell_marker = bool(_JS_ROOT_RE.search(html))
        if explicit_js_message and len(visible) < self.min_visible_chars:
            return "javascript_required_message"
        if (
            shell_marker
            and parser.script_count >= self.min_scripts
            and len(visible) < self.min_visible_chars
        ):
            return "client_rendered_shell"
        if not visible and len(html.strip()) < 128:
            return "empty_html"
        return None


class AcquisitionEngine:
    """Acquire a URL cheaply first and render only when policy says it is needed."""

    def __init__(
        self,
        *,
        http: HTTPTransport | None = None,
        renderer: PageRenderer | None | object = _DEFAULT_RENDERER,
        render_policy: DefaultRenderPolicy | None = None,
    ) -> None:
        self.http = http or UrllibHTTPTransport()
        self.renderer = (
            PlaywrightRenderer() if renderer is _DEFAULT_RENDERER else renderer
        )
        self.render_policy = render_policy or DefaultRenderPolicy()

    def acquire(self, request: AcquisitionRequest) -> AcquisitionResult:
        attempts: list[AcquisitionAttempt] = []
        warnings: list[str] = []

        if request.render_mode is RenderMode.ALWAYS:
            rendered = self._render_required(request, attempts, reason="render_mode_always")
            return self._result(request, rendered, attempts, warnings)

        static: PageSnapshot | None = None
        http_error: Exception | None = None
        try:
            static = self._checked_snapshot(self.http.fetch(request), request, "http")
            attempts.append(
                AcquisitionAttempt(
                    method=self.http.name,
                    success=True,
                    reason="http_success",
                    status_code=static.status_code,
                    final_url=static.final_url,
                    rendered=False,
                )
            )
        except Exception as exc:
            http_error = exc
            attempts.append(
                AcquisitionAttempt(
                    method=self.http.name,
                    success=False,
                    reason=f"{type(exc).__name__}: {exc}",
                    rendered=False,
                )
            )

        if request.render_mode is RenderMode.NEVER:
            if static is None:
                raise AcquisitionError(
                    f"HTTP acquisition failed and render_mode=never: {http_error}"
                ) from http_error
            return self._result(request, static, attempts, warnings)

        fallback_reason = "http_failed" if static is None else self.render_policy.reason(static)
        if fallback_reason is None:
            return self._result(request, static, attempts, warnings)

        if self.renderer is None:
            if static is not None:
                warnings.append(f"render_recommended_but_renderer_unavailable:{fallback_reason}")
                return self._result(request, static, attempts, warnings)
            raise AcquisitionError(
                f"HTTP acquisition failed and no renderer is configured: {http_error}"
            ) from http_error

        try:
            rendered = self._render_required(request, attempts, reason=fallback_reason)
            return self._result(request, rendered, attempts, warnings)
        except Exception as render_error:
            if static is not None:
                warnings.append(
                    f"renderer_failed_static_retained:{type(render_error).__name__}:{render_error}"
                )
                return self._result(request, static, attempts, warnings)
            raise AcquisitionError(
                f"Both HTTP and browser acquisition failed for {request.url}: "
                f"http={http_error}; browser={render_error}"
            ) from render_error

    def _render_required(
        self,
        request: AcquisitionRequest,
        attempts: list[AcquisitionAttempt],
        *,
        reason: str,
    ) -> PageSnapshot:
        if self.renderer is None:
            attempts.append(
                AcquisitionAttempt(
                    method="browser",
                    success=False,
                    reason=f"renderer_unavailable:{reason}",
                    rendered=True,
                )
            )
            raise AcquisitionError("Browser rendering was required but no renderer is configured")
        try:
            snapshot = self._checked_snapshot(
                self.renderer.render(request), request, self.renderer.name
            )
        except Exception as exc:
            attempts.append(
                AcquisitionAttempt(
                    method=self.renderer.name,
                    success=False,
                    reason=f"{reason}:{type(exc).__name__}: {exc}",
                    rendered=True,
                )
            )
            raise
        attempts.append(
            AcquisitionAttempt(
                method=self.renderer.name,
                success=True,
                reason=reason,
                status_code=snapshot.status_code,
                final_url=snapshot.final_url,
                rendered=True,
            )
        )
        return snapshot

    @staticmethod
    def _checked_snapshot(
        snapshot: PageSnapshot,
        request: AcquisitionRequest,
        method: str,
    ) -> PageSnapshot:
        final = urlsplit(snapshot.final_url)
        if final.scheme.casefold() not in {"http", "https"} or not final.netloc:
            raise AcquisitionError(f"{method} returned a non-HTTP(S) final URL")
        if snapshot.status_code >= 400:
            raise AcquisitionError(f"{method} returned HTTP {snapshot.status_code}")
        if len(snapshot.body) > request.max_bytes:
            raise AcquisitionError(
                f"{method} response exceeds max_bytes={request.max_bytes}"
            )
        return snapshot

    @staticmethod
    def _result(
        request: AcquisitionRequest,
        snapshot: PageSnapshot,
        attempts: Sequence[AcquisitionAttempt],
        warnings: Sequence[str],
    ) -> AcquisitionResult:
        decoded = _decode(snapshot)
        html = decoded if _is_html(snapshot.content_type, snapshot.body) else None
        text = None if html is not None else decoded
        method = next(
            (attempt.method for attempt in reversed(attempts) if attempt.success),
            "unknown",
        )
        asset = RawAsset(
            asset_id=request.resolved_asset_id,
            source_url=snapshot.final_url,
            html=html,
            text=text,
            metadata={
                "acquisition": {
                    "requested_url": request.url,
                    "final_url": snapshot.final_url,
                    "method": method,
                    "rendered": snapshot.rendered,
                    "status_code": snapshot.status_code,
                    "content_type": snapshot.content_type,
                }
            },
        )
        return AcquisitionResult(
            asset=asset,
            attempts=tuple(attempts),
            warnings=tuple(warnings),
        )


def _is_html(content_type: str, body: bytes) -> bool:
    normalized = content_type.casefold().split(";", 1)[0].strip()
    if normalized in {"text/html", "application/xhtml+xml"}:
        return True
    prefix = body.lstrip()[:128].lower()
    return prefix.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))


def _decode(snapshot: PageSnapshot) -> str:
    charset = "utf-8"
    match = _CHARSET_RE.search(snapshot.content_type)
    if match:
        charset = match.group(1)
    try:
        return snapshot.body.decode(charset, errors="replace")
    except LookupError:
        return snapshot.body.decode("utf-8", errors="replace")
