from __future__ import annotations

from .models import (
    AcquisitionError,
    AcquisitionRequest,
    MissingBrowserDependencyError,
    PageSnapshot,
)


class PlaywrightRenderer:
    """Optional Chromium renderer; Playwright is imported only when used."""

    name = "playwright"

    def __init__(self, *, browser_name: str = "chromium") -> None:
        if browser_name not in {"chromium", "firefox", "webkit"}:
            raise ValueError("browser_name must be chromium, firefox or webkit")
        self.browser_name = browser_name

    def render(self, request: AcquisitionRequest) -> PageSnapshot:
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depends on optional package
            raise MissingBrowserDependencyError(
                "Browser rendering requires the optional 'browser' dependency and "
                "an installed Playwright browser."
            ) from exc

        timeout_ms = max(1, int(request.timeout_s * 1000))
        try:
            with sync_playwright() as playwright:
                browser_type = getattr(playwright, self.browser_name)
                browser = browser_type.launch(headless=True)
                try:
                    context_kwargs = {}
                    if request.headers:
                        context_kwargs["extra_http_headers"] = dict(request.headers)
                    context = browser.new_context(**context_kwargs)
                    page = context.new_page()
                    response = page.goto(
                        request.url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                    try:
                        page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 5000))
                    except PlaywrightTimeoutError:
                        pass
                    html = page.content()
                    final_url = page.url
                    status = response.status if response is not None else 200
                    headers = response.headers if response is not None else {}
                finally:
                    browser.close()
        except PlaywrightTimeoutError as exc:  # pragma: no cover - integration behavior
            raise AcquisitionError(f"Browser render timed out for {request.url}") from exc
        except MissingBrowserDependencyError:
            raise
        except Exception as exc:  # pragma: no cover - integration behavior
            raise AcquisitionError(f"Browser render failed for {request.url}: {exc}") from exc

        body = html.encode("utf-8")
        if len(body) > request.max_bytes:
            raise AcquisitionError(
                f"Rendered response exceeds max_bytes={request.max_bytes} for {request.url}"
            )
        if status >= 400:
            raise AcquisitionError(f"Browser navigation returned HTTP {status} for {request.url}")

        return PageSnapshot(
            requested_url=request.url,
            final_url=final_url,
            status_code=status,
            content_type="text/html",
            body=body,
            headers=headers,
            rendered=True,
        )
