from __future__ import annotations

from dataclasses import dataclass

import pytest

from arvectum_data import (
    AcquisitionAttempt,
    AcquisitionRequest,
    AcquisitionResult,
    FieldSpec,
    RawAsset,
)
from arvectum_data.crawl import (
    CrawlPolicy,
    URLDiscoveryCrawler,
    canonicalize_url,
    extract_anchors,
)


@dataclass
class FakePage:
    html: str | None
    final_url: str | None = None
    rendered: bool = False


class FakeAcquisition:
    def __init__(self, pages: dict[str, FakePage], *, failures: dict[str, Exception] | None = None):
        self.pages = pages
        self.failures = failures or {}
        self.calls: list[AcquisitionRequest] = []

    def acquire(self, request: AcquisitionRequest) -> AcquisitionResult:
        self.calls.append(request)
        if request.url in self.failures:
            raise self.failures[request.url]
        page = self.pages[request.url]
        final_url = page.final_url or request.url
        asset = RawAsset(
            request.resolved_asset_id,
            source_url=final_url,
            html=page.html,
        )
        return AcquisitionResult(
            asset,
            (
                AcquisitionAttempt(
                    method="fake-browser" if page.rendered else "fake-http",
                    success=True,
                    reason="fixture",
                    status_code=200,
                    final_url=final_url,
                    rendered=page.rendered,
                ),
            ),
        )


def test_canonicalize_normalizes_host_default_port_and_fragment():
    assert canonicalize_url(
        "https://EXAMPLE.com:443/list",
        "/Offer?id=1#details",
    ) == "https://example.com/Offer?id=1"


def test_canonicalize_rejects_non_http_and_embedded_credentials():
    assert canonicalize_url("https://example.com/", "mailto:test@example.com") is None
    assert canonicalize_url(
        "https://example.com/",
        "https://user:secret@example.com/private",
    ) is None


def test_anchor_parser_preserves_text_rel_and_base():
    base, anchors = extract_anchors(
        '<base href="/catalog/"><a href="one" rel="nofollow sponsored"> One <b>door</b> </a>',
        max_links=10,
    )

    assert base == "/catalog/"
    assert anchors[0].href == "one"
    assert anchors[0].text == "One door"
    assert anchors[0].rel == ("nofollow", "sponsored")


def test_same_origin_default_discovers_internal_links_only():
    acquisition = FakeAcquisition(
        {
            "https://example.com/": FakePage(
                '<a href="/a">A</a><a href="https://outside.test/b">B</a>'
            ),
            "https://example.com/a": FakePage(""),
        }
    )
    result = URLDiscoveryCrawler(
        acquisition=acquisition,
        policy=CrawlPolicy(max_depth=1),
    ).discover(["https://example.com/"])

    assert result.urls() == ("https://example.com/a",)
    assert [request.url for request in acquisition.calls] == [
        "https://example.com/",
        "https://example.com/a",
    ]


def test_duplicate_and_fragment_variants_are_deduplicated():
    acquisition = FakeAcquisition(
        {
            "https://example.com/": FakePage(
                '<a href="/a#one">A1</a><a href="/a#two">A2</a><a href="https://EXAMPLE.com:443/a">A3</a>'
            ),
            "https://example.com/a": FakePage(""),
        }
    )

    result = URLDiscoveryCrawler(
        acquisition=acquisition,
        policy=CrawlPolicy(max_depth=1),
    ).discover(["https://example.com/"])

    assert result.urls() == ("https://example.com/a",)
    assert len(result.links) == 1


def test_query_variants_are_not_semantically_collapsed():
    acquisition = FakeAcquisition(
        {
            "https://example.com/": FakePage(
                '<a href="/offer?id=1">One</a><a href="/offer?id=2">Two</a>'
            ),
            "https://example.com/offer?id=1": FakePage(""),
            "https://example.com/offer?id=2": FakePage(""),
        }
    )
    result = URLDiscoveryCrawler(
        acquisition=acquisition,
        policy=CrawlPolicy(max_depth=1),
    ).discover(["https://example.com/"])

    assert result.urls() == (
        "https://example.com/offer?id=1",
        "https://example.com/offer?id=2",
    )


def test_nofollow_and_static_assets_are_skipped_by_default():
    acquisition = FakeAcquisition(
        {
            "https://example.com/": FakePage(
                '<a href="/skip" rel="nofollow">No</a>'
                '<a href="/image.JPG?size=large">Image</a>'
                '<a href="/keep">Keep</a>'
            ),
            "https://example.com/keep": FakePage(""),
        }
    )
    result = URLDiscoveryCrawler(
        acquisition=acquisition,
        policy=CrawlPolicy(max_depth=1),
    ).discover(["https://example.com/"])

    assert result.urls() == ("https://example.com/keep",)


def test_base_href_is_supported_but_cannot_escape_same_origin_scope():
    acquisition = FakeAcquisition(
        {
            "https://example.com/list": FakePage(
                '<base href="https://outside.test/catalog/"><a href="one">One</a><a href="https://example.com/local">Local</a>'
            ),
            "https://example.com/local": FakePage(""),
        }
    )
    result = URLDiscoveryCrawler(
        acquisition=acquisition,
        policy=CrawlPolicy(max_depth=1),
    ).discover(["https://example.com/list"])

    assert result.urls() == ("https://example.com/local",)


def test_depth_bound_is_deterministic_breadth_first():
    acquisition = FakeAcquisition(
        {
            "https://example.com/": FakePage('<a href="/a">A</a><a href="/b">B</a>'),
            "https://example.com/a": FakePage('<a href="/c">C</a>'),
            "https://example.com/b": FakePage('<a href="/d">D</a>'),
        }
    )
    result = URLDiscoveryCrawler(
        acquisition=acquisition,
        policy=CrawlPolicy(max_depth=1, max_pages=10),
    ).discover(["https://example.com/"])

    assert result.urls() == ("https://example.com/a", "https://example.com/b")
    assert [page.url for page in result.pages] == [
        "https://example.com/",
        "https://example.com/a",
        "https://example.com/b",
    ]


def test_max_pages_stops_frontier_and_marks_result_truncated():
    acquisition = FakeAcquisition(
        {
            "https://example.com/": FakePage('<a href="/a">A</a><a href="/b">B</a>'),
            "https://example.com/a": FakePage(""),
            "https://example.com/b": FakePage(""),
        }
    )
    result = URLDiscoveryCrawler(
        acquisition=acquisition,
        policy=CrawlPolicy(max_depth=1, max_pages=2),
    ).discover(["https://example.com/"])

    assert [request.url for request in acquisition.calls] == [
        "https://example.com/",
        "https://example.com/a",
    ]
    assert result.truncated
    assert result.limit_reasons == ("max_pages",)


def test_max_discovered_urls_stops_growth_and_marks_result_truncated():
    acquisition = FakeAcquisition(
        {
            "https://example.com/": FakePage(
                '<a href="/a">A</a><a href="/b">B</a><a href="/c">C</a>'
            ),
        }
    )
    result = URLDiscoveryCrawler(
        acquisition=acquisition,
        policy=CrawlPolicy(max_depth=1, max_discovered_urls=2),
    ).discover(["https://example.com/"])

    assert result.urls() == ("https://example.com/a", "https://example.com/b")
    assert result.limit_reasons == ("max_discovered_urls",)


def test_max_links_per_page_bounds_parser_work():
    acquisition = FakeAcquisition(
        {
            "https://example.com/": FakePage(
                '<a href="/a">A</a><a href="/b">B</a><a href="/c">C</a>'
            ),
            "https://example.com/a": FakePage(""),
        }
    )
    result = URLDiscoveryCrawler(
        acquisition=acquisition,
        policy=CrawlPolicy(max_depth=1, max_links_per_page=1),
    ).discover(["https://example.com/"])

    assert result.urls() == ("https://example.com/a",)


def test_page_failure_is_isolated_and_error_message_redacts_urls():
    acquisition = FakeAcquisition(
        {
            "https://example.com/": FakePage('<a href="/bad">Bad</a><a href="/good">Good</a>'),
            "https://example.com/good": FakePage(""),
        },
        failures={
            "https://example.com/bad": RuntimeError(
                "failed while reading https://secret.example/path?token=abc"
            )
        },
    )
    result = URLDiscoveryCrawler(
        acquisition=acquisition,
        policy=CrawlPolicy(max_depth=1),
    ).discover(["https://example.com/"])

    assert len(result.failures) == 1
    assert result.failures[0].url == "https://example.com/bad"
    assert "<url>" in result.failures[0].message
    assert "secret.example" not in result.failures[0].message
    assert any(page.url == "https://example.com/good" for page in result.pages)


def test_off_origin_redirect_is_recorded_but_not_expanded():
    acquisition = FakeAcquisition(
        {
            "https://example.com/": FakePage(
                '<a href="/inside">Inside</a>',
                final_url="https://outside.test/landing",
            ),
        }
    )
    result = URLDiscoveryCrawler(
        acquisition=acquisition,
        policy=CrawlPolicy(max_depth=1),
    ).discover(["https://example.com/"])

    assert result.urls() == ()
    assert result.pages[0].scope_allowed is False
    assert result.pages[0].final_url == "https://outside.test/landing"


def test_cross_origin_mode_requires_explicit_allow_list():
    with pytest.raises(ValueError, match="allowed_hosts"):
        CrawlPolicy(same_origin=False)


def test_explicit_cross_origin_allow_list_is_bounded():
    acquisition = FakeAcquisition(
        {
            "https://example.com/": FakePage(
                '<a href="https://allowed.test/a">A</a>'
                '<a href="https://blocked.test/b">B</a>'
            ),
            "https://allowed.test/a": FakePage(""),
        }
    )
    result = URLDiscoveryCrawler(
        acquisition=acquisition,
        policy=CrawlPolicy(
            same_origin=False,
            allowed_hosts=("allowed.test",),
            max_depth=1,
        ),
    ).discover(["https://example.com/"])

    assert result.urls() == ("https://allowed.test/a",)


def test_rendered_acquisition_marker_is_preserved_in_page_record():
    acquisition = FakeAcquisition(
        {
            "https://example.com/": FakePage('<a href="/a">A</a>', rendered=True),
            "https://example.com/a": FakePage(""),
        }
    )
    result = URLDiscoveryCrawler(
        acquisition=acquisition,
        policy=CrawlPolicy(max_depth=1),
    ).discover(["https://example.com/"])

    assert result.pages[0].rendered is True


def test_discovery_result_builds_extraction_job_without_reinventing_job_layer():
    acquisition = FakeAcquisition(
        {
            "https://example.com/": FakePage('<a href="/a">A</a>'),
            "https://example.com/a": FakePage(""),
        }
    )
    result = URLDiscoveryCrawler(
        acquisition=acquisition,
        policy=CrawlPolicy(max_depth=1),
    ).discover(["https://example.com/"])

    job = result.to_job(
        "crawl-job",
        [FieldSpec("title"), FieldSpec("price", required=True)],
    )

    assert job.job_id == "crawl-job"
    assert [item.url for item in job.items] == ["https://example.com/a"]
    assert [field.key for field in job.fields] == ["title", "price"]


def test_multiple_seed_origins_are_each_in_scope_without_cross_expansion():
    acquisition = FakeAcquisition(
        {
            "https://one.test/": FakePage(
                '<a href="/a">A</a><a href="https://two.test/b">B</a>'
            ),
            "https://two.test/": FakePage('<a href="/b">B</a>'),
            "https://one.test/a": FakePage(""),
            "https://two.test/b": FakePage(""),
        }
    )
    result = URLDiscoveryCrawler(
        acquisition=acquisition,
        policy=CrawlPolicy(max_depth=1),
    ).discover(["https://one.test/", "https://two.test/"])

    assert result.urls() == (
        "https://one.test/a",
        "https://two.test/b",
    )
