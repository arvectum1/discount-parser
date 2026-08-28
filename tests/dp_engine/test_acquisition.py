from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from arvectum_data.acquisition import (
    AcquisitionEngine,
    AcquisitionError,
    AcquisitionRequest,
    PageSnapshot,
    RenderMode,
)


@dataclass
class FakeHTTP:
    snapshot: PageSnapshot | None = None
    error: Exception | None = None
    calls: list[str] = field(default_factory=list)
    name: str = "fake_http"

    def fetch(self, request):
        self.calls.append(request.url)
        if self.error:
            raise self.error
        assert self.snapshot is not None
        return self.snapshot


@dataclass
class FakeRenderer:
    snapshot: PageSnapshot | None = None
    error: Exception | None = None
    calls: list[str] = field(default_factory=list)
    name: str = "fake_browser"

    def render(self, request):
        self.calls.append(request.url)
        if self.error:
            raise self.error
        assert self.snapshot is not None
        return self.snapshot


def page(body, *, rendered=False, content_type="text/html", status=200, final=None):
    return PageSnapshot(
        requested_url="https://example.test/item",
        final_url=final or "https://example.test/item",
        status_code=status,
        content_type=content_type,
        body=body.encode("utf-8"),
        rendered=rendered,
    )


def test_static_html_uses_http_only():
    static = page("<html><body>" + ("Useful product content " * 10) + "</body></html>")
    http = FakeHTTP(static)
    renderer = FakeRenderer(page("<html>rendered</html>", rendered=True))

    result = AcquisitionEngine(http=http, renderer=renderer).acquire(
        AcquisitionRequest("https://example.test/item")
    )

    assert result.asset.html == static.body.decode()
    assert not result.used_renderer
    assert renderer.calls == []
    assert result.asset.metadata["acquisition"]["method"] == "fake_http"


def test_client_rendered_shell_falls_back_automatically():
    shell = page(
        '<html><body><div id="root"></div><script src="a.js"></script>'
        '<script src="b.js"></script></body></html>'
    )
    rendered = page(
        "<html><body>Rendered product price: 199</body></html>", rendered=True
    )
    renderer = FakeRenderer(rendered)

    result = AcquisitionEngine(http=FakeHTTP(shell), renderer=renderer).acquire(
        AcquisitionRequest("https://example.test/item")
    )

    assert result.used_renderer
    assert result.asset.html == rendered.body.decode()
    assert result.attempts[-1].reason == "client_rendered_shell"


def test_http_failure_falls_back_to_browser_in_auto_mode():
    renderer = FakeRenderer(page("<html>browser result</html>", rendered=True))
    result = AcquisitionEngine(
        http=FakeHTTP(error=AcquisitionError("connection reset")),
        renderer=renderer,
    ).acquire(AcquisitionRequest("https://example.test/item"))

    assert result.used_renderer
    assert [attempt.success for attempt in result.attempts] == [False, True]
    assert result.attempts[-1].reason == "http_failed"


def test_render_mode_never_does_not_call_renderer():
    shell = page('<div id="app"></div><script></script><script></script>')
    renderer = FakeRenderer(page("<html>rendered</html>", rendered=True))
    result = AcquisitionEngine(http=FakeHTTP(shell), renderer=renderer).acquire(
        AcquisitionRequest("https://example.test/item", render_mode=RenderMode.NEVER)
    )

    assert renderer.calls == []
    assert not result.used_renderer


def test_render_mode_always_skips_http():
    http = FakeHTTP(page("<html>static</html>"))
    renderer = FakeRenderer(page("<html>rendered</html>", rendered=True))
    result = AcquisitionEngine(http=http, renderer=renderer).acquire(
        AcquisitionRequest("https://example.test/item", render_mode=RenderMode.ALWAYS)
    )

    assert http.calls == []
    assert result.used_renderer
    assert result.attempts[0].reason == "render_mode_always"


def test_renderer_failure_retains_successful_static_snapshot():
    shell = page(
        '<div id="root"></div><script src="a"></script><script src="b"></script>'
    )
    result = AcquisitionEngine(
        http=FakeHTTP(shell),
        renderer=FakeRenderer(error=AcquisitionError("browser crashed")),
    ).acquire(AcquisitionRequest("https://example.test/item"))

    assert result.asset.html == shell.body.decode()
    assert result.warnings
    assert "renderer_failed_static_retained" in result.warnings[0]


def test_both_paths_failure_is_explicit():
    engine = AcquisitionEngine(
        http=FakeHTTP(error=AcquisitionError("network down")),
        renderer=FakeRenderer(error=AcquisitionError("browser down")),
    )
    with pytest.raises(AcquisitionError, match="Both HTTP and browser acquisition failed"):
        engine.acquire(AcquisitionRequest("https://example.test/item"))


def test_missing_renderer_keeps_static_with_warning_when_render_recommended():
    shell = page(
        '<div id="root"></div><script src="a"></script><script src="b"></script>'
    )
    result = AcquisitionEngine(http=FakeHTTP(shell), renderer=None).acquire(
        AcquisitionRequest("https://example.test/item")
    )

    assert result.asset.html == shell.body.decode()
    assert result.warnings == (
        "render_recommended_but_renderer_unavailable:client_rendered_shell",
    )


def test_non_http_url_and_embedded_credentials_are_rejected():
    with pytest.raises(ValueError, match=r"HTTP\(S\)"):
        AcquisitionRequest("file:///etc/passwd")
    with pytest.raises(ValueError, match="credentials"):
        AcquisitionRequest("https://user:pass@example.test/item")


def test_snapshot_size_guard_applies_to_custom_adapters():
    oversized = page("x" * 101, content_type="text/plain")
    engine = AcquisitionEngine(http=FakeHTTP(oversized))
    with pytest.raises(AcquisitionError, match="max_bytes=100"):
        engine.acquire(AcquisitionRequest("https://example.test/item", max_bytes=100))


def test_plain_text_becomes_raw_asset_text_and_redirect_is_provenance():
    snapshot = page(
        "price: 199",
        content_type="text/plain; charset=utf-8",
        final="https://example.test/final",
    )
    result = AcquisitionEngine(http=FakeHTTP(snapshot)).acquire(
        AcquisitionRequest("https://example.test/item", asset_id="known-id")
    )

    assert result.asset.asset_id == "known-id"
    assert result.asset.source_url == "https://example.test/final"
    assert result.asset.text == "price: 199"
    assert result.asset.html is None
    assert result.asset.metadata["acquisition"]["requested_url"] == "https://example.test/item"


def test_asset_id_is_deterministic_for_url():
    one = AcquisitionRequest("https://example.test/item").resolved_asset_id
    two = AcquisitionRequest("https://example.test/item").resolved_asset_id
    other = AcquisitionRequest("https://example.test/other").resolved_asset_id
    assert one == two
    assert one != other


def test_non_http_final_url_is_rejected_after_redirect():
    snapshot = page("ok", content_type="text/plain", final="file:///tmp/result")
    with pytest.raises(AcquisitionError, match=r"non-HTTP\(S\) final URL"):
        AcquisitionEngine(http=FakeHTTP(snapshot)).acquire(
            AcquisitionRequest("https://example.test/item", render_mode=RenderMode.NEVER)
        )


def test_charset_from_content_type_is_used():
    snapshot = PageSnapshot(
        requested_url="https://example.test/item",
        final_url="https://example.test/item",
        status_code=200,
        content_type="text/plain; charset=windows-1251",
        body="Цена: 199".encode("cp1251"),
    )
    result = AcquisitionEngine(http=FakeHTTP(snapshot)).acquire(
        AcquisitionRequest("https://example.test/item")
    )
    assert result.asset.text == "Цена: 199"


def test_javascript_required_message_triggers_renderer():
    shell = page("<html><body>Please enable JavaScript to continue.</body></html>")
    rendered = page("<html><body>Loaded</body></html>", rendered=True)
    result = AcquisitionEngine(
        http=FakeHTTP(shell), renderer=FakeRenderer(rendered)
    ).acquire(AcquisitionRequest("https://example.test/item"))
    assert result.used_renderer
    assert result.attempts[-1].reason == "javascript_required_message"


def test_always_mode_without_renderer_fails_explicitly():
    with pytest.raises(AcquisitionError, match="no renderer"):
        AcquisitionEngine(http=FakeHTTP(page("<html>static</html>")), renderer=None).acquire(
            AcquisitionRequest(
                "https://example.test/item",
                render_mode=RenderMode.ALWAYS,
            )
        )
