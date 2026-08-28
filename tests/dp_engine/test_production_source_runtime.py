from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from arvectum_data.acquisition import AcquisitionRequest
from src.sources.base import RawOffer
from src.sources.config import SourceConfig, load_source_configs
from src.sources.engine_runtime import (
    DiscountParserHTTPTransport,
    ProductionSourcePolicy,
    ProductionSourceRuntime,
)


@dataclass
class FakeResponse:
    url: str
    body: str
    status_code: int = 200

    @property
    def content(self) -> bytes:
        return self.body.encode("utf-8")

    @property
    def text(self) -> str:
        return self.body

    @property
    def headers(self) -> dict[str, str]:
        return {"content-type": "text/html; charset=utf-8"}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code} for {self.url}")


class RecordingAdapter:
    def __init__(self, config: SourceConfig, calls: dict[str, object]) -> None:
        self.config = config
        self.calls = calls

    def parse(self, html: str) -> list[RawOffer]:
        parse_urls = self.calls.setdefault("parse_urls", [])
        assert isinstance(parse_urls, list)
        parse_urls.append(self.config.base_url)
        if "PARSE_ERROR" in html:
            raise ValueError(f"decoder failed at {self.config.base_url}")
        if "SAVE10" not in html:
            return []
        external_id = "shared" if "SHARED_ID" in html else self.config.base_url.rstrip("/").split("/")[-1]
        return [
            RawOffer(
                source_key=self.config.key,
                external_id=external_id,
                title="Скидка 10% по промокоду",
                source_url=self.config.base_url,
                promo_code="SAVE10",
                discount_percent=Decimal("10"),
                raw_payload={"decoder": "fixture"},
            )
        ]

    def collect(self) -> list[RawOffer]:
        self.calls["collect"] = int(self.calls.get("collect", 0)) + 1
        return [
            RawOffer(
                source_key=self.config.key,
                external_id="legacy",
                title="Legacy скидка 5%",
                source_url=self.config.base_url,
                discount_percent=Decimal("5"),
            )
        ]


def _factory(calls: dict[str, object]):
    def build(config: SourceConfig) -> RecordingAdapter:
        built = calls.setdefault("built", [])
        assert isinstance(built, list)
        built.append(config)
        return RecordingAdapter(config, calls)

    return build


def _install_pages(monkeypatch: pytest.MonkeyPatch, pages: dict[str, str], calls: list[dict[str, object]]) -> None:
    def fake_get(url: str, **kwargs):
        calls.append({"url": url, **kwargs})
        if url not in pages:
            raise RuntimeError(f"missing fixture {url}")
        return FakeResponse(url=url, body=pages[url])

    monkeypatch.setattr("src.sources.engine_runtime.network_router.get", fake_get)


def _runtime(
    config: SourceConfig,
    *,
    calls: dict[str, object],
    transport: DiscountParserHTTPTransport | None = None,
) -> ProductionSourceRuntime:
    return ProductionSourceRuntime(
        config,
        policy=ProductionSourcePolicy(
            crawl_max_pages=8,
            crawl_max_depth=1,
            crawl_max_discovered_urls=50,
            crawl_max_links_per_page=50,
            target_max_probe_pages=20,
            target_max_selected_urls=20,
            timeout_s=2,
            max_bytes=200_000,
        ),
        transport=transport,
        adapter_factory=_factory(calls),
    )


def test_product_transport_uses_governed_route_and_run_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    network_calls: list[dict[str, object]] = []
    _install_pages(monkeypatch, {"https://example.test/": "<html>ok</html>"}, network_calls)
    transport = DiscountParserHTTPTransport(network_policy="proxy", retries=1)
    request = AcquisitionRequest(url="https://example.test/", timeout_s=3, max_bytes=1000)

    first = transport.fetch(request)
    second = transport.fetch(request)

    assert first is second
    assert transport.requests_made == 1
    assert transport.cache_hits == 1
    assert network_calls[0]["route"] == "proxy"
    assert network_calls[0]["timeout"] == 3
    assert network_calls[0]["follow_redirects"] is True


def test_auto_transport_preserves_route_retry_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
    network_calls: list[dict[str, object]] = []
    _install_pages(monkeypatch, {"https://example.test/": "<html>ok</html>"}, network_calls)
    transport = DiscountParserHTTPTransport(network_policy="auto", retries=1)
    transport.fetch(AcquisitionRequest(url="https://example.test/"))
    assert network_calls[0]["retry_statuses"] == {403, 451}


def test_legacy_mode_preserves_existing_collect_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}
    config = SourceConfig("demo", "Demo", "demo", "https://example.test/")
    result = _runtime(config, calls=calls).collect()
    assert result.runtime_mode == "legacy"
    assert result.fallback_used is False
    assert [offer.external_id for offer in result.offers] == ["legacy"]
    assert calls["collect"] == 1


def test_hybrid_discovers_selects_and_decodes_without_legacy_collect(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        "https://example.test/": '<html><body><a href="/deals/shop">Скидки и промокоды</a></body></html>',
        "https://example.test/deals/shop": (
            "<html><title>Shop промокоды и скидки</title><h1>Shop</h1>"
            "<main>Промокод SAVE10 дает скидку 10%. Получить промокод.</main></html>"
        ),
    }
    network_calls: list[dict[str, object]] = []
    _install_pages(monkeypatch, pages, network_calls)
    calls: dict[str, object] = {}
    config = SourceConfig("demo", "Demo", "demo", "https://example.test/", runtime_mode="hybrid")

    result = _runtime(config, calls=calls).collect()

    assert result.runtime_mode == "hybrid"
    assert result.fallback_used is False
    assert [offer.external_id for offer in result.offers] == ["shop"]
    assert calls.get("collect", 0) == 0
    assert "https://example.test/deals/shop" in calls["parse_urls"]
    assert result.discovered_urls >= 2
    assert result.selected_urls >= 1
    assert result.decoded_pages >= 1
    # Crawl, relevance and decoder share the same transport cache.
    assert len(network_calls) == 2


def test_effective_decoder_base_url_is_selected_page(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        "https://example.test/": '<a href="/deals/merchant">Промокоды</a>',
        "https://example.test/deals/merchant": "<h1>Merchant промокоды</h1><p>SAVE10 скидка 10%</p>",
    }
    _install_pages(monkeypatch, pages, [])
    calls: dict[str, object] = {}
    config = SourceConfig("demo", "Demo", "demo", "https://example.test/", runtime_mode="hybrid")
    result = _runtime(config, calls=calls).collect()
    assert result.offers[0].source_url == "https://example.test/deals/merchant"


def test_promokood_existing_js_attribute_discovery_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        "https://promokood.ru/": '<html><div data-target="\\/o\\/vseinstrumenti">Все предложения</div></html>',
        "https://promokood.ru/o/vseinstrumenti": (
            "<html><title>ВсеИнструменты — промокоды</title><h1>ВсеИнструменты</h1>"
            "<p>Промокод SAVE10, скидка 10%</p></html>"
        ),
    }
    network_calls: list[dict[str, object]] = []
    _install_pages(monkeypatch, pages, network_calls)
    calls: dict[str, object] = {}
    config = SourceConfig("promokood", "Promokood", "promokood", "https://promokood.ru/", runtime_mode="hybrid")

    result = _runtime(config, calls=calls).collect()

    assert any(offer.source_url.endswith("/o/vseinstrumenti") for offer in result.offers)
    assert "https://promokood.ru/o/vseinstrumenti" in calls["parse_urls"]
    assert len(network_calls) == 2


def test_duplicate_external_ids_across_pages_are_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        "https://example.test/": '<a href="/deals/a">Скидки</a><a href="/deals/b">Промокоды</a>',
        "https://example.test/deals/a": "<h1>Акции</h1><p>SAVE10 скидка 10% SHARED_ID</p>",
        "https://example.test/deals/b": "<h1>Промокоды</h1><p>SAVE10 скидка 10% SHARED_ID</p>",
    }
    _install_pages(monkeypatch, pages, [])
    calls: dict[str, object] = {}
    config = SourceConfig("demo", "Demo", "demo", "https://example.test/", runtime_mode="hybrid")
    result = _runtime(config, calls=calls).collect()
    assert len(result.offers) == 1
    assert result.offers[0].external_id == "shared"


def test_one_page_decoder_failure_does_not_abort_other_target(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        "https://example.test/": '<a href="/deals/bad">Скидки</a><a href="/deals/good">Промокоды</a>',
        "https://example.test/deals/bad": "<h1>Скидки</h1><p>Промокод PARSE_ERROR скидка 10%</p>",
        "https://example.test/deals/good": "<h1>Промокоды</h1><p>SAVE10 скидка 10%</p>",
    }
    _install_pages(monkeypatch, pages, [])
    calls: dict[str, object] = {}
    config = SourceConfig("demo", "Demo", "demo", "https://example.test/", runtime_mode="hybrid")
    result = _runtime(config, calls=calls).collect()
    assert [offer.external_id for offer in result.offers] == ["good"]
    assert result.fallback_used is False
    assert any(value.startswith("page_decode_failed:") for value in result.warnings)


def test_engine_failure_falls_back_to_existing_collect(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_get(url: str, **kwargs):
        raise RuntimeError(f"network failed for {url}?token=secret")

    monkeypatch.setattr("src.sources.engine_runtime.network_router.get", fail_get)
    calls: dict[str, object] = {}
    config = SourceConfig("demo", "Demo", "demo", "https://example.test/", runtime_mode="hybrid")
    transport = DiscountParserHTTPTransport(network_policy="auto", retries=1, retry_backoff_s=0)
    result = _runtime(config, calls=calls, transport=transport).collect()

    assert [offer.external_id for offer in result.offers] == ["legacy"]
    assert result.fallback_used is True
    assert calls["collect"] == 1
    warning = "\n".join(result.warnings)
    assert "token=secret" not in warning
    assert "<url>" in warning


def test_zero_engine_offers_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {"https://example.test/": "<html><body>ordinary page</body></html>"}
    _install_pages(monkeypatch, pages, [])
    calls: dict[str, object] = {}
    config = SourceConfig("demo", "Demo", "demo", "https://example.test/", runtime_mode="hybrid")
    result = _runtime(config, calls=calls).collect()
    assert result.fallback_used is True
    assert result.offers[0].external_id == "legacy"
    assert "engine_no_offers:legacy_fallback" in result.warnings


def test_hard_negative_page_is_never_decoded(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        "https://example.test/": '<a href="/privacy">Privacy</a><a href="/deals/good">Промокоды</a>',
        "https://example.test/privacy": "<h1>Privacy</h1><p>SAVE10 скидка 10%</p>",
        "https://example.test/deals/good": "<h1>Промокоды</h1><p>SAVE10 скидка 10%</p>",
    }
    _install_pages(monkeypatch, pages, [])
    calls: dict[str, object] = {}
    config = SourceConfig("demo", "Demo", "demo", "https://example.test/", runtime_mode="hybrid")
    result = _runtime(config, calls=calls).collect()
    assert result.offers
    assert "https://example.test/privacy" not in calls["parse_urls"]


def test_offer_observation_payload_gets_engine_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        "https://example.test/": '<a href="/deals/shop">Промокоды</a>',
        "https://example.test/deals/shop": "<h1>Промокоды</h1><p>SAVE10 скидка 10%</p>",
    }
    _install_pages(monkeypatch, pages, [])
    calls: dict[str, object] = {}
    config = SourceConfig("demo", "Demo", "demo", "https://example.test/", runtime_mode="hybrid")
    result = _runtime(config, calls=calls).collect()
    metadata = result.offers[0].raw_payload["dp_engine"]
    assert metadata["runtime"] == "hybrid"
    assert metadata["page_url"] == "https://example.test/deals/shop"
    assert metadata["target_status"] in {"target", "candidate"}
    assert isinstance(metadata["evidence"], list)


def test_source_config_default_is_legacy_and_invalid_value_falls_back(tmp_path: Path) -> None:
    default = SourceConfig("demo", "Demo", "demo", "https://example.test/")
    assert default.runtime_mode == "legacy"

    path = tmp_path / "sources.yaml"
    path.write_text(
        "sources:\n"
        "  - key: demo\n"
        "    adapter: demo\n"
        "    base_url: https://example.test/\n"
        "    runtime_mode: surprise\n",
        encoding="utf-8",
    )
    assert load_source_configs(path)[0].runtime_mode == "legacy"


def test_shipped_five_sources_explicitly_use_hybrid_runtime() -> None:
    configs = load_source_configs("config/sources.yaml")
    assert {config.key for config in configs} == {
        "promokood",
        "promokodik",
        "berikod",
        "promokodi_net_ru",
        "promko",
    }
    assert all(config.runtime_mode == "hybrid" for config in configs)
