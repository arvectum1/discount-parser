from __future__ import annotations

from dataclasses import dataclass

import pytest

from arvectum_data import (
    AcquisitionAttempt,
    AcquisitionRequest,
    AcquisitionResult,
    CrawlDiscoveryResult,
    CrawlLink,
    FieldSpec,
    RawAsset,
    RenderMode,
    TargetPageClassifier,
    TargetPagePolicy,
    TargetPageStatus,
)


@dataclass
class FakePage:
    html: str | None = None
    text: str | None = None
    rendered: bool = False


class FakeAcquisition:
    def __init__(
        self,
        pages: dict[str, FakePage] | None = None,
        *,
        failures: dict[str, Exception] | None = None,
    ) -> None:
        self.pages = pages or {}
        self.failures = failures or {}
        self.calls: list[AcquisitionRequest] = []

    def acquire(self, request: AcquisitionRequest) -> AcquisitionResult:
        self.calls.append(request)
        if request.url in self.failures:
            raise self.failures[request.url]
        page = self.pages[request.url]
        asset = RawAsset(
            asset_id=request.resolved_asset_id,
            source_url=request.url,
            html=page.html,
            text=page.text,
        )
        return AcquisitionResult(
            asset=asset,
            attempts=(
                AcquisitionAttempt(
                    method="fake-browser" if page.rendered else "fake-http",
                    success=True,
                    reason="fixture",
                    status_code=200,
                    final_url=request.url,
                    rendered=page.rendered,
                ),
            ),
        )


def discovery(
    *links: CrawlLink,
    seeds: tuple[str, ...] = (),
) -> CrawlDiscoveryResult:
    return CrawlDiscoveryResult(
        seeds=seeds,
        links=tuple(links),
        pages=(),
        failures=(),
    )


def promo_html(title: str = "Промокоды магазина") -> str:
    return f"""
    <html>
      <head><title>{title}</title></head>
      <body>
        <h1>{title}</h1>
        <article>Промокод на скидку 20%. Активировать промокод.</article>
        <article>Акция и купон на скидку. Получить скидку.</article>
        <article>Новое предложение и кэшбэк.</article>
      </body>
    </html>
    """


def test_policy_rejects_invalid_threshold_order():
    with pytest.raises(ValueError, match="target_threshold"):
        TargetPagePolicy(target_threshold=2.0, candidate_threshold=2.0)


def test_promokood_merchant_path_becomes_target_after_content_probe():
    url = "https://promokood.ru/o/vseinstrumenti"
    source = discovery(
        CrawlLink(
            url=url,
            parent_url="https://promokood.ru/",
            depth=1,
            anchor_text="ВсеИнструменты",
        )
    )
    acquisition = FakeAcquisition(
        {url: FakePage(html=promo_html("Промокоды ВсеИнструменты"))}
    )

    result = TargetPageClassifier(acquisition=acquisition).classify(source)

    assessment = result.assessments[0]
    assert assessment.status is TargetPageStatus.TARGET
    assert assessment.probed is True
    assert any(e.signal == "merchant_path" for e in assessment.evidence)
    assert any(e.source == "h1" for e in assessment.evidence)


def test_plain_merchant_slug_can_be_target_from_content_without_url_marker():
    url = "https://example.test/vseinstrumenti"
    source = discovery(CrawlLink(url, "https://example.test/", 1, "ВсеИнструменты"))
    acquisition = FakeAcquisition(
        {url: FakePage(html=promo_html("ВсеИнструменты — промокоды"))}
    )

    result = TargetPageClassifier(acquisition=acquisition).classify(source)

    assert result.assessments[0].status is TargetPageStatus.TARGET


def test_discount_aggregator_seed_is_assessed_by_default():
    seed = "https://promko.net/ru"
    acquisition = FakeAcquisition(
        {seed: FakePage(html=promo_html("Промокоды и скидки для онлайн-магазинов"))}
    )
    source = discovery(seeds=(seed,))

    result = TargetPageClassifier(acquisition=acquisition).classify(source)

    assert [item.url for item in result.assessments] == [seed]
    assert result.assessments[0].status is TargetPageStatus.TARGET


def test_hard_negative_account_path_is_not_probed_even_with_positive_anchor():
    url = "https://example.test/account/promokody"
    source = discovery(CrawlLink(url, "https://example.test/", 1, "Промокоды"))
    acquisition = FakeAcquisition({})

    result = TargetPageClassifier(acquisition=acquisition).classify(source)

    assessment = result.assessments[0]
    assert assessment.status is TargetPageStatus.NON_TARGET
    assert acquisition.calls == []
    assert assessment.score == -100.0


def test_pagination_and_navigation_evidence_penalize_non_offer_page():
    url = "https://example.test/catalog?page=2&sort=date"
    source = discovery(CrawlLink(url, "https://example.test/", 1, "Следующая"))
    acquisition = FakeAcquisition(
        {url: FakePage(html="<title>Каталог</title><h1>Каталог</h1>Магазины")}
    )

    result = TargetPageClassifier(acquisition=acquisition).classify(source)

    assessment = result.assessments[0]
    assert assessment.status is TargetPageStatus.NON_TARGET
    assert any(e.signal == "pagination_or_view" for e in assessment.evidence)
    assert any(e.signal == "navigation_path" for e in assessment.evidence)


def test_anchor_discount_terms_raise_preliminary_relevance():
    url = "https://example.test/store-x"
    source = discovery(
        CrawlLink(url, "https://example.test/", 1, "Промокоды и скидки Store X")
    )
    acquisition = FakeAcquisition(
        {url: FakePage(html="<title>Store X</title>Промокод скидка акция")}
    )

    result = TargetPageClassifier(acquisition=acquisition).classify(source)

    assessment = result.assessments[0]
    assert any(e.source == "anchor_text" for e in assessment.evidence)
    assert assessment.score >= 2.0


def test_probe_false_keeps_weak_unknown_as_unprobed():
    url = "https://example.test/random-slug"
    source = discovery(CrawlLink(url, "https://example.test/", 1, "Random"))
    acquisition = FakeAcquisition({})

    result = TargetPageClassifier(acquisition=acquisition).classify(source, probe=False)

    assert result.assessments[0].status is TargetPageStatus.UNPROBED
    assert acquisition.calls == []


def test_probe_limit_is_explicit_and_deterministic():
    urls = tuple(f"https://example.test/{index}" for index in range(3))
    source = discovery(
        *(CrawlLink(url, "https://example.test/", 1, "") for url in urls)
    )
    acquisition = FakeAcquisition(
        {
            url: FakePage(html=promo_html(f"Промокоды {index}"))
            for index, url in enumerate(urls)
        }
    )
    policy = TargetPagePolicy(max_probe_pages=2)

    result = TargetPageClassifier(acquisition=acquisition, policy=policy).classify(source)

    assert [call.url for call in acquisition.calls] == list(urls[:2])
    assert result.truncated
    assert "max_probe_pages" in result.limit_reasons
    assert result.assessments[2].status is TargetPageStatus.UNPROBED


def test_probe_failure_isolated_redacts_embedded_url_and_keeps_candidate():
    url = "https://example.test/o/store"
    source = discovery(CrawlLink(url, "https://example.test/", 1, "Store"))
    acquisition = FakeAcquisition(
        failures={
            url: RuntimeError("failed reading https://secret.test/path?token=abc")
        }
    )

    result = TargetPageClassifier(acquisition=acquisition).classify(source)

    assessment = result.assessments[0]
    assert assessment.status is TargetPageStatus.CANDIDATE
    assert assessment.probe_error_type == "RuntimeError"
    assert "<url>" in (assessment.probe_error_message or "")
    assert "secret.test" not in (assessment.probe_error_message or "")


def test_script_and_style_discount_words_do_not_create_visible_text_signal():
    url = "https://example.test/plain"
    html = """
    <html><head><title>Company</title>
    <style>.promo:after { content: "промокод скидка акция купон"; }</style></head>
    <body><script>window.words = "промокод скидка акция купон";</script>
    <h1>Company</h1><p>О компании.</p></body></html>
    """
    source = discovery(CrawlLink(url, "https://example.test/", 1, "Company"))
    acquisition = FakeAcquisition({url: FakePage(html=html)})

    result = TargetPageClassifier(acquisition=acquisition).classify(source)

    assert result.assessments[0].status is TargetPageStatus.NON_TARGET


def test_meta_description_can_support_discount_candidate():
    url = "https://example.test/shop-x"
    html = """
    <html><head>
      <title>Shop X</title>
      <meta name="description" content="Промокоды, скидки и акции Shop X">
    </head><body><h1>Shop X</h1></body></html>
    """
    source = discovery(CrawlLink(url, "https://example.test/", 1, "Shop X"))
    acquisition = FakeAcquisition({url: FakePage(html=html)})

    result = TargetPageClassifier(acquisition=acquisition).classify(source)

    assert result.assessments[0].status is TargetPageStatus.CANDIDATE
    assert any(e.source == "meta" for e in result.assessments[0].evidence)


def test_text_asset_without_html_is_classified():
    url = "https://example.test/feed-view"
    text = "Промокод скидка акция купон предложение кэшбэк. Активировать промокод."
    source = discovery(CrawlLink(url, "https://example.test/", 1, ""))
    acquisition = FakeAcquisition({url: FakePage(text=text)})

    result = TargetPageClassifier(acquisition=acquisition).classify(source)

    assert result.assessments[0].status in {
        TargetPageStatus.TARGET,
        TargetPageStatus.CANDIDATE,
    }


def test_generic_exact_title_is_penalized_but_content_can_still_win():
    url = "https://example.test/shops"
    html = promo_html("Магазины")
    source = discovery(CrawlLink(url, "https://example.test/", 1, "Магазины"))
    acquisition = FakeAcquisition({url: FakePage(html=html)})

    result = TargetPageClassifier(acquisition=acquisition).classify(source)

    assessment = result.assessments[0]
    assert any(e.signal == "generic_exact" for e in assessment.evidence)
    assert assessment.score < 8.0


def test_ranking_puts_target_before_candidate_before_non_target():
    target = "https://example.test/promokod/store"
    candidate = "https://example.test/o/store2"
    rejected = "https://example.test/login"
    source = discovery(
        CrawlLink(candidate, "https://example.test/", 1, "Store 2"),
        CrawlLink(rejected, "https://example.test/", 1, "Login"),
        CrawlLink(target, "https://example.test/", 1, "Промокоды Store"),
    )
    acquisition = FakeAcquisition(
        {
            target: FakePage(html=promo_html("Промокоды Store")),
            candidate: FakePage(
                html="<title>Store 2</title><h1>Store 2</h1>Промокод скидка"
            ),
        }
    )

    result = TargetPageClassifier(acquisition=acquisition).classify(source)

    ranked = result.ranked()
    assert ranked[0].status is TargetPageStatus.TARGET
    assert ranked[-1].status is TargetPageStatus.NON_TARGET


def test_selected_url_cap_is_explicit_without_rewriting_assessment_status():
    urls = tuple(f"https://example.test/promokod/{index}" for index in range(3))
    source = discovery(
        *(CrawlLink(url, "https://example.test/", 1, "Промокод") for url in urls)
    )
    acquisition = FakeAcquisition(
        {
            url: FakePage(html=promo_html(f"Промокоды {index}"))
            for index, url in enumerate(urls)
        }
    )
    policy = TargetPagePolicy(max_selected_urls=2)

    result = TargetPageClassifier(acquisition=acquisition, policy=policy).classify(source)

    assert result.urls() == urls[:2]
    assert "max_selected_urls" in result.limit_reasons
    assert all(item.status is TargetPageStatus.TARGET for item in result.assessments)


def test_to_job_reuses_existing_execution_job_and_includes_candidates_by_default():
    target = "https://example.test/promokod/store"
    candidate = "https://example.test/o/store2"
    source = discovery(
        CrawlLink(candidate, "https://example.test/", 1, "Store 2"),
        CrawlLink(target, "https://example.test/", 1, "Промокод Store"),
    )
    acquisition = FakeAcquisition(
        {
            target: FakePage(html=promo_html("Промокоды Store")),
            candidate: FakePage(
                html="<title>Store 2</title><h1>Store 2</h1>Промокод скидка"
            ),
        }
    )

    result = TargetPageClassifier(acquisition=acquisition).classify(source)
    job = result.to_job(
        "discount-targets",
        [FieldSpec("title"), FieldSpec("promo_code", required=True)],
    )

    assert job.job_id == "discount-targets"
    assert [item.url for item in job.items] == list(result.urls())


def test_to_job_can_exclude_candidates_for_strict_run():
    target = "https://example.test/promokod/store"
    candidate = "https://example.test/o/store2"
    source = discovery(
        CrawlLink(candidate, "https://example.test/", 1, "Store 2"),
        CrawlLink(target, "https://example.test/", 1, "Промокод Store"),
    )
    acquisition = FakeAcquisition(
        {
            target: FakePage(html=promo_html("Промокоды Store")),
            candidate: FakePage(
                html="<title>Store 2</title><h1>Store 2</h1>Промокод скидка"
            ),
        }
    )
    result = TargetPageClassifier(acquisition=acquisition).classify(source)

    job = result.to_job(
        "strict-targets",
        [FieldSpec("title")],
        include_candidates=False,
    )

    assert [item.url for item in job.items] == [target]


def test_duplicate_seed_and_discovered_url_is_assessed_once():
    url = "https://example.test/promokod/store"
    source = discovery(
        CrawlLink(url, "https://example.test/", 1, "Промокоды"),
        seeds=(url,),
    )
    acquisition = FakeAcquisition({url: FakePage(html=promo_html())})

    result = TargetPageClassifier(acquisition=acquisition).classify(source)

    assert len(result.assessments) == 1
    assert len(acquisition.calls) == 1


def test_probe_request_preserves_bounded_transport_controls_and_headers():
    url = "https://example.test/o/store"
    source = discovery(CrawlLink(url, "https://example.test/", 1, "Store"))
    acquisition = FakeAcquisition({url: FakePage(html=promo_html())})
    policy = TargetPagePolicy(
        timeout_s=7.0,
        max_bytes=123_456,
        render_mode=RenderMode.NEVER,
    )

    TargetPageClassifier(acquisition=acquisition, policy=policy).classify(
        source,
        headers={"X-Test": "yes"},
    )

    request = acquisition.calls[0]
    assert request.timeout_s == 7.0
    assert request.max_bytes == 123_456
    assert request.render_mode is RenderMode.NEVER
    assert request.headers == {"X-Test": "yes"}
