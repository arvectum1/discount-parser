from __future__ import annotations

from dataclasses import dataclass

from arvectum_data import AcquisitionAttempt, AcquisitionRequest, AcquisitionResult, RawAsset
from arvectum_data.crawl import CrawlPolicy, URLDiscoveryCrawler


@dataclass
class FakePage:
    html: str
    final_url: str


class FakeAcquisition:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.pages = {
            "https://example.com/old": FakePage(
                '<a href="/new">Canonical</a><a href="/item">Item</a>',
                "https://example.com/new",
            ),
            "https://example.com/item": FakePage("", "https://example.com/item"),
        }

    def acquire(self, request: AcquisitionRequest) -> AcquisitionResult:
        self.calls.append(request.url)
        page = self.pages[request.url]
        return AcquisitionResult(
            RawAsset(request.resolved_asset_id, source_url=page.final_url, html=page.html),
            (
                AcquisitionAttempt(
                    "fake-http",
                    True,
                    "fixture",
                    200,
                    page.final_url,
                    False,
                ),
            ),
        )


def test_redirect_target_is_treated_as_already_processed_frontier_alias():
    acquisition = FakeAcquisition()
    result = URLDiscoveryCrawler(
        acquisition=acquisition,
        policy=CrawlPolicy(max_depth=1, max_pages=10),
    ).discover(["https://example.com/old"])

    assert acquisition.calls == [
        "https://example.com/old",
        "https://example.com/item",
    ]
    assert result.urls() == ("https://example.com/item",)
    assert result.pages[0].final_url == "https://example.com/new"
