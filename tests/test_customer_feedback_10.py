from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.modules.source_registry import auto_setup
from src.modules.source_registry.service import ItemPayload
from src.web import source_registry_routes, source_setup_routes


ROOT = Path(__file__).resolve().parents[1]


def test_bare_domain_is_accepted_without_protocol() -> None:
    assert auto_setup.normalize_source_url("example.ru/promokody") == "https://example.ru/promokody"


def test_website_auto_analysis_requires_no_css_or_html(monkeypatch) -> None:
    payloads = [
        ItemPayload(
            external_id="1",
            url="https://shop.example/deal/1",
            title="Скидка 20% на заказ",
            text="Промокод SALE20 действует до 31.12.2099",
            raw_payload={"collector": "generic_web"},
        ),
        ItemPayload(
            external_id="2",
            url="https://shop.example/deal/2",
            title="Вторая акция",
            text="Скидка на доставку",
            raw_payload={"collector": "generic_web"},
        ),
        ItemPayload(
            external_id="3",
            url="https://shop.example/deal/3",
            title="Третья акция",
            text="Специальное предложение",
            raw_payload={"collector": "generic_web"},
        ),
    ]

    monkeypatch.setattr(auto_setup.GenericWebCollector, "collect", lambda self, source: payloads)

    analysis = auto_setup.analyze_source_url("shop.example/promos")

    assert analysis.platform == "website"
    assert analysis.collector_type == "generic_web"
    assert analysis.source_type == "promotion_page"
    assert analysis.fetched == 3
    assert analysis.promo_codes_found == 1
    assert analysis.items[0].promo_code == "SALE20"
    assert analysis.confidence >= 0.8


def test_customer_sources_page_asks_only_for_link(monkeypatch) -> None:
    monkeypatch.setattr(source_setup_routes, "_require_setup", lambda: None)
    monkeypatch.setattr(source_setup_routes, "_source_rows", lambda: "<tr><td>source</td></tr>")
    monkeypatch.setattr(source_setup_routes, "_specialist_block", lambda: "")

    response = source_setup_routes.friendly_registry_page()
    body = response.body.decode("utf-8")

    assert "Проверить источник" in body
    assert "Вставьте ссылку" in body
    assert "HTML, CSS, атрибуты" in body
    assert 'name="url"' in body
    assert 'name="promo_code_selector"' not in body
    assert 'name="collector_type"' not in body
    assert 'name="trust_level"' not in body


def test_source_technical_settings_are_explicitly_secondary() -> None:
    source = (ROOT / "src" / "web" / "source_setup_routes.py").read_text(encoding="utf-8")
    assert "Для специалиста — технические настройки" in source
    assert "/sources-registry/{snapshot['id']}/edit" in source
    assert "CSS-селекторы, collector" in source


def test_friendly_settings_post_precedes_legacy_generic_action_route() -> None:
    test_app = FastAPI()
    test_app.include_router(source_setup_routes.router)
    test_app.include_router(source_registry_routes.router)

    with TestClient(test_app) as client:
        response = client.post("/sources-registry/123/settings", data={})

    assert response.status_code == 422

    application_source = (ROOT / "src" / "web" / "application.py").read_text(encoding="utf-8")
    assert application_source.index("app.include_router(source_setup_router)") < application_source.index(
        "app.include_router(source_registry_router)"
    )


def test_feedback_10_windows_installer_version() -> None:
    installer = (ROOT / "packaging" / "windows" / "installer.iss").read_text(encoding="utf-8")
    assert '#define MyAppVersion "0.1.13"' in installer
