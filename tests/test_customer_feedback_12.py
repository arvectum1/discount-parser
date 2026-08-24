from __future__ import annotations

from pathlib import Path

from src.modules.source_registry import assisted_setup
from src.modules.source_registry.known_site_crawl import discover_promokood_detail_urls
from src.modules.source_registry.service import ItemPayload


ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, url: str, text: str):
        self.url = url
        self.text = text


def test_promokood_detail_discovery_handles_links_buttons_and_embedded_data() -> None:
    html = """
    <a href="/o/one">Все промокоды</a>
    <button data-url="/o/two">Все промокоды</button>
    <button onclick="location.href='/o/three'">Все акции</button>
    <script>window.__DATA__={"next":"\\/o\\/four","external":"https://advertiser.example/go"}</script>
    <a href="https://advertiser.example/activate">Активировать</a>
    """
    urls = discover_promokood_detail_urls(html, entry_url="https://promokood.ru/travel")
    assert urls == [
        "https://promokood.ru/o/one",
        "https://promokood.ru/o/two",
        "https://promokood.ru/o/three",
        "https://promokood.ru/o/four",
    ]


def test_promokood_category_is_confirm_only_preset(monkeypatch) -> None:
    category_html = """
    <main>
      <article class="merchant-card"><h2>ВсеИнструменты</h2><a class="all-codes" href="/o/vseinstrumenti">Все промокоды</a><a href="https://advertiser.example">Активировать</a></article>
      <article class="merchant-card"><h2>Островок</h2><button data-url="/o/ostrovok">Все промокоды</button></article>
    </main>
    """

    def fake_get(self, url: str, *, route: str = "auto", retry_statuses=None):
        return FakeResponse(url, category_html)

    def fake_collect(self, source):
        if "/o/" in source.url:
            slug = source.url.rstrip("/").split("/")[-1]
            return [
                ItemPayload(
                    external_id=slug,
                    url=source.url,
                    title=f"Скидка {slug}",
                    text="Промокод SALE20 скидка 20% до 31.12.2099",
                    raw_payload={"collector": "known_site_adapter", "promo_code": "SALE20"},
                )
            ]
        return []

    monkeypatch.setattr(assisted_setup.GenericWebCollector, "_get", fake_get)
    monkeypatch.setattr(assisted_setup.GenericWebCollector, "collect", fake_collect)

    proposal = assisted_setup.analyze_assisted_source("https://promokood.ru/travel")

    assert proposal.crawl_mode == "follow_internal"
    assert proposal.strategy == "preset:promokood-category"
    assert proposal.detail_url_contains == "/o/"
    assert proposal.detail_link_selector == 'a[href*="/o/"]'
    assert proposal.discovered_detail_pages == 2
    assert proposal.can_confirm is True
    assert proposal.item_selector is None
    assert proposal.previews[0].promo_code == "SALE20"


def test_promokood_detail_page_needs_no_manual_mapping(monkeypatch) -> None:
    def fake_collect(self, source):
        return [
            ItemPayload(
                external_id="1",
                url=source.url,
                title="Скидка на инструменты",
                text="Промокод TOOL20",
                raw_payload={"collector": "known_site_adapter", "promo_code": "TOOL20"},
            )
        ]

    monkeypatch.setattr(assisted_setup.GenericWebCollector, "collect", fake_collect)
    proposal = assisted_setup.analyze_assisted_source("https://promokood.ru/o/vseinstrumenti")

    assert proposal.crawl_mode == "direct"
    assert proposal.confidence == 0.99
    assert proposal.can_confirm is True
    assert proposal.item_selector is None
    assert proposal.previews[0].promo_code == "TOOL20"


def test_generic_repeated_cards_get_automatic_structural_profile(monkeypatch) -> None:
    page = """
    <main>
      <article class="offer"><h3 class="title">Скидка 20%</h3><p class="terms">Промокод SAVE20 до 31.12.2099</p><a class="go" href="/1">Открыть</a></article>
      <article class="offer"><h3 class="title">Скидка 15%</h3><p class="terms">Промокод SAVE15 до 30.11.2099</p><a class="go" href="/2">Открыть</a></article>
      <article class="offer"><h3 class="title">Скидка 10%</h3><p class="terms">Промокод SAVE10 до 30.10.2099</p><a class="go" href="/3">Открыть</a></article>
    </main>
    """

    monkeypatch.setattr(
        assisted_setup.GenericWebCollector,
        "_get",
        lambda self, url, *, route="auto", retry_statuses=None: FakeResponse(url, page),
    )
    proposal = assisted_setup.analyze_assisted_source("https://shop.example/promos")

    assert proposal.strategy == "automatic-structural-profile"
    assert proposal.item_selector == "article.offer"
    assert proposal.title_selector == "h3.title"
    assert proposal.can_confirm is True
    assert len(proposal.previews) >= 2


def test_customer_facing_application_uses_confirm_only_auto_routes() -> None:
    application = (ROOT / "src" / "web" / "application.py").read_text(encoding="utf-8")
    routes = (ROOT / "src" / "web" / "customer_feedback_13_routes.py").read_text(encoding="utf-8")
    assert "app.include_router(customer_feedback_13_router)" in application
    assert "'/sources-registry/analyze'" in routes
    assert "customer_source_analysis_page" in routes
    assert "'/sources-registry/confirm-auto'" in routes
    assert "customer_confirm_assisted_source" in routes
    assert "Настроить автоматически" in application
    assert "проверить несколько найденных строк и подтвердить" in application


def test_follow_collection_allows_known_detail_adapter_without_css() -> None:
    follow = (ROOT / "src" / "modules" / "source_registry" / "follow_collection.py").read_text(encoding="utf-8")
    assert "two-stage source requires a saved detail extraction profile" not in follow
    assert "SimpleNamespace(url=detail_page_url)" in follow
    assert "if not items and source.item_selector" in follow
    assert "discover_promokood_detail_urls" in follow


def test_feedback_12_windows_installer_version() -> None:
    installer = (ROOT / "packaging" / "windows" / "installer.iss").read_text(encoding="utf-8")
    assert '#define MyAppVersion "0.1.13"' in installer
