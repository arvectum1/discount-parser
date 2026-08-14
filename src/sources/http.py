from __future__ import annotations

import time

import httpx

from src.shared.network import NetworkRouteError, network_router


_DEFAULT_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)


class HttpClient:
    def __init__(
        self,
        timeout_seconds: float = 15.0,
        retries: int = 3,
        user_agent: str | None = None,
        network_policy: str = "auto",
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.user_agent = user_agent or _DEFAULT_BROWSER_UA
        self.network_policy = network_policy


    def get_text(self, url: str) -> str:
        last_error: Exception | None = None
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru,en;q=0.8",
        }
        retry_statuses = {403, 451} if self.network_policy == "auto" else set()
        for attempt in range(self.retries):
            try:
                response = network_router.get(
                    url,
                    route=self.network_policy,
                    retry_statuses=retry_statuses,
                    timeout=self.timeout_seconds,
                    headers=headers,
                )
                response.raise_for_status()
                return response.text
            except (httpx.HTTPError, NetworkRouteError) as exc:
                last_error = exc
                if attempt < self.retries - 1:
                    time.sleep(0.5 * (2**attempt))
        assert last_error is not None
        raise last_error
