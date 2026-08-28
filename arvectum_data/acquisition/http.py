from __future__ import annotations

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import AcquisitionError, AcquisitionRequest, PageSnapshot


_DEFAULT_USER_AGENT = "ArvectumDataPlatform/0.3 (+https://arvectum.com)"


class UrllibHTTPTransport:
    """Zero-dependency HTTP transport for the cheap/static acquisition path."""

    name = "http"

    def fetch(self, request: AcquisitionRequest) -> PageSnapshot:
        headers = {"User-Agent": _DEFAULT_USER_AGENT, **dict(request.headers)}
        native = Request(request.url, headers=headers, method="GET")
        try:
            with urlopen(native, timeout=request.timeout_s) as response:  # noqa: S310
                status = int(getattr(response, "status", 200))
                final_url = response.geturl()
                response_headers = {key: value for key, value in response.headers.items()}
                content_type = response.headers.get("Content-Type") or "application/octet-stream"
                body = response.read(request.max_bytes + 1)
        except HTTPError as exc:
            raise AcquisitionError(f"HTTP status {exc.code} for {request.url}") from exc
        except URLError as exc:
            raise AcquisitionError(f"HTTP transport failed for {request.url}: {exc.reason}") from exc
        except OSError as exc:
            raise AcquisitionError(f"HTTP transport failed for {request.url}: {exc}") from exc

        if len(body) > request.max_bytes:
            raise AcquisitionError(
                f"HTTP response exceeds max_bytes={request.max_bytes} for {request.url}"
            )
        if status >= 400:
            raise AcquisitionError(f"HTTP status {status} for {request.url}")

        return PageSnapshot(
            requested_url=request.url,
            final_url=final_url,
            status_code=status,
            content_type=content_type,
            body=body,
            headers=response_headers,
            rendered=False,
        )
