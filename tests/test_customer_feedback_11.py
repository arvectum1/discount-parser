from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup

from src.modules.source_registry.follow_profiles import FollowProfile, extract_internal_detail_urls
from src.modules.source_registry.manual_profile import (
    generalize_container_selector,
    normalize_manual_profile,
    preview_manual_profile_html,
)
from src.web import manual_mapping_routes


ROOT = Path(__file__).resolve().parents[1]


def test_copied_sample_card_selector_is_generalized_for_all_cards() -> None:
    assert (
        generalize_container_selector("#app > main > section > article.offer:nth-child(2)")
        == "#app > main > section > article.offer"
    )


def test_full_devtools_field_selectors_become_relative_to_card() -> None:
    profile = normalize_manual_profile(
        item_selector="#app > main > article.offer:nth-child(1)",
        title_selector="#app > main > article.offer:nth-child(1) > h3.title",
        promo_code_selector="#app > main > article.offer:nth-child(1) > code.code",
        conditions_selector="#app > main > article.offer:nth-child(1) > p.terms",
        valid_until_selector="#app > main > article.offer:nth-child(1) > time.until",
        link_selector="#app > main > article.offer:nth-child(1) > a.go",
        image_selector="#app > main > article.offer:nth-child(1) > img.pic",
    )

    assert profile.item_selector == "#app > main > article.offer"
    assert profile.title_selector == "h3.title"
    assert profile.promo_code_selector == "code.code"
    assert profile.conditions_selector == "p.terms"
    assert profile.valid_until_selector == "time.until"
    assert profile.link_selector == "a.go"
    assert profile.image_selector == "img.pic"


def test_profile_preview_maps_each_selected_element_to_expected_column() -> None:
    html_text = """
    <div id="app"><main>
      <article class="offer">
        <h3 class="title">Скидка на инструмент</h3>
        <code class="code" data-code="SALE20">SALE20</code>
        <p class="terms">Скидка 20% на заказ</p>
        <time class="until">до 31.12.2099</time>
        <a class="go" href="/deal/1">Открыть</a>
        <img class="pic" data-src="/img/1.jpg">
      </article>
      <article class="offer">
        <h3 class="title">Вторая акция</h3>
        <code class="code" data-code="TOOLS15">TOOLS15</code>
        <p class="terms">Скидка 15% на второй заказ</p>
        <time class="until">до 30.11.2099</time>
        <a class="go" href="/deal/2">Открыть</a>
        <img class="pic" data-src="/img/2.jpg">
      </article>
    </main></div>
    """
    profile = normalize_manual_profile(
        item_selector="#app > main > article.offer:nth-child(1)",
        title_selector="#app > main > article.offer:nth-child(1) > h3.title",
        promo_code_selector="#app > main > article.offer:nth-child(1) > code.code",
        promo_code_attribute="data-code",
        conditions_selector="#app > main > article.offer:nth-child(1) > p.terms",
        valid_until_selector="#app > main > article.offer:nth-child(1) > time.until",
        link_selector="#app > main > article.offer:nth-child(1) > a.go",
        image_selector="#app > main > article.offer:nth-child(1) > img.pic",
        image_attribute="data-src",
    )

    items = preview_manual_profile_html(
        html_text,
        page_url="https://shop.example/promos",
        profile=profile,
        limit=5,
    )

    assert len(items) == 2
    assert items[0].title == "Скидка на инструмент"
    assert items[0].promo_code == "SALE20"
    assert items[0].discount_percent == "20"
    assert items[0].conditions == "Скидка 20% на заказ"
    assert items[0].valid_until == "2099-12-31"
    assert items[0].link == "https://shop.example/deal/1"
    assert items[0].image_url == "https://shop.example/img/1.jpg"
    assert items[1].promo_code == "TOOLS15"


def test_catalog_mode_follows_only_internal_detail_links() -> None:
    soup = BeautifulSoup(
        """
        <main>
          <article class="service"><a class="all" href="/o/one">Все промокоды</a><a class="activate" href="https://advertiser.example/go">Активировать</a></article>
          <article class="service"><a class="all" href="https://promokood.ru/o/two">Все промокоды</a></article>
          <article class="service"><a class="all" href="/category/not-a-detail">Категория</a></article>
        </main>
        """,
        "html.parser",
    )
    profile = FollowProfile(
        crawl_mode="follow_internal",
        listing_item_selector="article.service",
        detail_link_selector="a.all",
        detail_url_contains="/o/",
        max_detail_pages=100,
    )

    urls = extract_internal_detail_urls(soup, entry_url="https://promokood.ru/travel", profile=profile)

    assert urls == ["https://promokood.ru/o/one", "https://promokood.ru/o/two"]
    assert all("advertiser.example" not in url for url in urls)


def test_follow_profile_migration_is_present() -> None:
    migration = (ROOT / "migrations" / "versions" / "0009_source_follow_profiles.py").read_text(encoding="utf-8")
    assert 'revision = "0009"' in migration
    assert 'down_revision = "0008"' in migration
    assert "source_follow_profiles" in migration
    assert "follow_internal" in migration


def test_manual_mapping_routes_are_declared_before_auto_and_legacy_routes() -> None:
    manual_source = (ROOT / "src" / "web" / "manual_mapping_routes.py").read_text(encoding="utf-8")
    application_source = (ROOT / "src" / "web" / "application.py").read_text(encoding="utf-8")

    assert "@router.post('/sources-registry/{source_id}/settings')" in manual_source
    assert "@router.post('/sources-registry/{source_id}/mapping/save')" in manual_source
    assert "@router.post('/sources-registry/add-auto')" in manual_source
    assert application_source.index("app.include_router(customer_feedback_13_router)") < application_source.index(
        "app.include_router(manual_mapping_router)"
    )
    assert application_source.index("app.include_router(manual_mapping_router)") < application_source.index(
        "app.include_router(source_setup_router)"
    )
    assert application_source.index("app.include_router(source_setup_router)") < application_source.index(
        "app.include_router(source_registry_router)"
    )
    assert 'app.add_api_route("/developer/sources-registry/{source_id}/mapping", mapping_page_v2' in application_source
    assert "install_follow_profile_collection()" in application_source


def test_mapping_page_instructs_customer_to_copy_selectors_not_line_numbers(monkeypatch) -> None:
    monkeypatch.setattr(manual_mapping_routes, "_require_setup", lambda: None)
    monkeypatch.setattr(
        manual_mapping_routes,
        "_source_snapshot",
        lambda source_id: {
            "id": source_id,
            "name": "Test source",
            "url": "https://shop.example/promos",
            "platform": "website",
            "collector_type": "generic_web",
            "network_policy": "auto",
            "item_selector": None,
            "title_selector": None,
            "promo_code_selector": None,
            "promo_code_attribute": None,
            "conditions_selector": None,
            "valid_until_selector": None,
            "link_selector": None,
            "image_selector": None,
            "image_attribute": None,
        },
    )

    response = manual_mapping_routes.source_mapping_page(7)
    body = response.body.decode("utf-8")

    assert "Copy selector" in body
    assert "Номер строки" in body
    assert "Название предложения → графа" in body
    assert "Промокод → графа" in body
    assert "Срок действия → графа" in body
    assert "Изображение → графа" in body


def test_v2_mapping_copy_explains_catalog_and_external_activation_rules() -> None:
    source = (ROOT / "src" / "web" / "manual_mapping_v2.py").read_text(encoding="utf-8")
    assert "Каталог → открыть внутреннюю страницу каждого сервиса" in source
    assert "Все промокоды" in source
    assert "внешние «Активировать»" in source or "Внешние кнопки активации" in source
    assert "Номер строки HTML не используем" in source


def test_feedback_11_windows_installer_version() -> None:
    installer = (ROOT / "packaging" / "windows" / "installer.iss").read_text(encoding="utf-8")
    assert '#define MyAppVersion "' in installer
