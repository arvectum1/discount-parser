from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from src import customer_feedback_17 as feedback17
from src.modules.source_registry import follow_collection, proposal_apply
from src.modules.source_registry.assisted_setup import AssistedSourceProposal
from src.modules.source_registry.collectors import GenericWebCollector
from src.modules.source_registry.follow_profiles import FollowProfile
from src.modules.source_registry.models import RegisteredSource
from src.modules.source_registry.service import ItemPayload
from src.telegram.publication_format import PublicationFormat


def test_background_runtime_has_same_follow_collector_as_web_runtime() -> None:
    # Importing src is part of both DiscountParser.exe and DiscountParserWorker.exe.
    # The website collector patch must therefore already be present before the
    # worker scheduler starts; it may not depend on importing src.web.application.
    assert getattr(GenericWebCollector, "_dp_cust_011_follow_profile_patch", False) is True


def test_worker_style_promokood_category_follows_internal_pages(monkeypatch) -> None:
    profile = FollowProfile(
        crawl_mode="follow_internal",
        detail_link_selector='a[href*="/o/"]',
        detail_url_contains="/o/",
        max_detail_pages=10,
    )
    monkeypatch.setattr(follow_collection, "get_follow_profile", lambda source_id: profile)

    class Response:
        def __init__(self, url: str, body: str):
            self.url = url
            self.text = body

    collector = GenericWebCollector()

    def fake_get(url: str, *, route: str = "auto", retry_statuses=None):
        if url == "https://promokood.ru/":
            return Response(url, '<a href="/o/store-one">Все промокоды</a>')
        assert url == "https://promokood.ru/o/store-one"
        return Response(url, '<article><h1>Store One</h1><div>Промокод SAVE20 — скидка 20%</div></article>')

    monkeypatch.setattr(collector, "_get", fake_get)
    monkeypatch.setattr(
        collector,
        "_known_site_items",
        lambda source, page_url, html: [
            ItemPayload(
                external_id="one",
                url=page_url,
                title="Скидка 20% в Store One",
                text="Промокод SAVE20 — скидка 20%",
                raw_payload={"collector": "known_site_adapter", "promo_code": "SAVE20"},
            )
        ],
    )

    source = SimpleNamespace(id=17, url="https://promokood.ru/", network_policy="auto", item_selector=None)
    items = collector.collect(source)

    assert len(items) == 1
    assert items[0].url == "https://promokood.ru/o/store-one"
    assert items[0].raw_payload["crawl_mode"] == "follow_internal"
    assert items[0].raw_payload["detail_url"] == "https://promokood.ru/o/store-one"


def test_automatic_apply_self_heals_missing_profile_tables_and_is_single_transaction(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    RegisteredSource.__table__.create(engine)

    @contextmanager
    def isolated_scope():
        with Session(engine) as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    monkeypatch.setattr(proposal_apply, "session_scope", isolated_scope)

    proposal = AssistedSourceProposal(
        url="https://promokood.ru/",
        name="Promokood",
        crawl_mode="follow_internal",
        strategy="preset:promokood-category",
        confidence=0.99,
        explanation="test",
        detail_link_selector='a[href*="/o/"]',
        detail_url_contains="/o/",
        previews=(),
    )

    source_id = proposal_apply.apply_assisted_proposal(proposal)

    with Session(engine) as session:
        source = session.get(RegisteredSource, source_id)
        assert source is not None
        assert source.enabled is True
        assert source.collector_type == "generic_web"
        follow = session.execute(
            text("SELECT crawl_mode, detail_link_selector, detail_url_contains FROM source_follow_profiles WHERE registered_source_id=:id"),
            {"id": source_id},
        ).mappings().one()
        assert follow["crawl_mode"] == "follow_internal"
        assert follow["detail_link_selector"] == 'a[href*="/o/"]'
        assert follow["detail_url_contains"] == "/o/"
        image = session.execute(
            text("SELECT registered_source_id FROM source_image_profiles WHERE registered_source_id=:id"),
            {"id": source_id},
        ).one()
        assert image[0] == source_id


def test_telegram_post_is_enriched_before_universal_structuring(monkeypatch) -> None:
    payload = ItemPayload(
        external_id="telegram:agni:1",
        url="https://agni.prfl.me/promoskid_en/offer",
        title=None,
        text=(
            "Сетевые фильтры Agni со скидкой 30%\n"
            "AGNISALE0058\n"
            "Скидка 30% на каждый заказ в магазине Agni на Яндекс Маркете\n"
            "Рекламодатель ООО «АГНИ». ИНН 1234567890"
        ),
        raw_payload={
            "collector": "telegram_public",
            "offer_url": "https://agni.prfl.me/promoskid_en/offer",
            "source_post_url": "https://t.me/agni/1",
        },
    )
    monkeypatch.setattr(feedback17, "_ORIGINAL_TELEGRAM_COLLECT", lambda self, source: [payload])

    result = feedback17._telegram_collect_v17(object(), SimpleNamespace())

    assert len(result) == 1
    enriched = result[0]
    assert enriched.title == "Сетевые фильтры Agni со скидкой 30%"
    assert enriched.raw_payload["promo_code"] == "AGNISALE0058"
    assert enriched.raw_payload["discount_percent"] == "30"
    assert "30%" in str(enriched.raw_payload.get("conditions") or "")
    assert enriched.raw_payload["telegram_structuring_version"] == "dp-cust-017"
    assert "SKID_EN" not in str(enriched.raw_payload.get("promo_code"))


def test_outgoing_caption_redacts_named_secrets_without_overriding_field_choices(monkeypatch) -> None:
    class Offer:
        promo_code = "SAVE20"

    monkeypatch.setattr(
        feedback17,
        "_ORIGINAL_RENDER",
        lambda offer, publication_format=None: "🔥 Акция\n📌 Условия: token=very-secret-value",
    )
    caption = feedback17._render_offer_caption_v17(
        Offer(),
        PublicationFormat(order=("conditions",), enabled=frozenset({"conditions"})),
    )

    assert "Промокод:" not in caption
    assert "very-secret-value" not in caption
    assert "***REDACTED***" in caption
