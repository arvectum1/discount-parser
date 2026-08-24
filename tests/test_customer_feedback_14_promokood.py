from __future__ import annotations

from decimal import Decimal

from src.sources.adapters.promokood import PromokoodAdapter


def test_promokood_detail_text_is_split_into_individual_promo_blocks() -> None:
    html = """
    <main>
      <h1>CDEK</h1>
      <div>курьерская служба</div>
      <button>Активировать промокод</button>

      <div>cdek15new</div>
      <div>промокод на скидку 15%</div>
      <div>на услуги CDEK для новых пользователей</div>
      <div>до 31.12.2026</div>

      <div>Adv500</div>
      <div>промокод на скидку 500 ₽</div>
      <div>на покупку от 15000 ₽ в CDEK.Shopping</div>
      <div>до 31.12.2026</div>

      <h2>О сервисе:</h2>
      <div>Описание сервиса</div>
      <div>RELATED999</div>
      <div>промокод на скидку 99%</div>
      <div>до 31.12.2026</div>
    </main>
    """

    offers = PromokoodAdapter("https://promokood.ru/o/cdek").parse(html)

    assert len(offers) == 2
    assert [offer.promo_code for offer in offers] == ["cdek15new", "Adv500"]
    assert offers[0].merchant == "CDEK"
    assert offers[0].discount_percent == Decimal("15")
    assert offers[0].conditions == "на услуги CDEK для новых пользователей"
    assert offers[1].discount_amount == Decimal("500")
    assert offers[1].conditions == "на покупку от 15000 ₽ в CDEK.Shopping"
    assert all(offer.source_url == "https://promokood.ru/o/cdek" for offer in offers)
    assert all("RELATED999" not in (offer.description or "") for offer in offers)


def test_promokood_duplicate_rendered_blocks_are_deduplicated() -> None:
    block = """
      <div>ALLVSNTPO</div>
      <div>промокод на скидку 7% (не более 3500 ₽)</div>
      <div>на любое по счету бронирование отеля</div>
      <div>до 31.05.2027</div>
    """
    html = f"<main><h1>Отелло</h1><button>Активировать промокод</button>{block}{block}<h2>О сервисе:</h2></main>"

    offers = PromokoodAdapter("https://promokood.ru/o/otello").parse(html)

    assert len(offers) == 1
    assert offers[0].promo_code == "ALLVSNTPO"
    assert offers[0].discount_percent == Decimal("7")
    assert offers[0].conditions == "на любое по счету бронирование отеля"


def test_promokood_same_code_with_different_conditions_stays_as_two_offers() -> None:
    html = """
    <main>
      <h1>Отелло</h1>
      <button>Активировать промокод</button>
      <div>ALLVSNTPO</div>
      <div>промокод на скидку 7% (не более 3500 ₽)</div>
      <div>на любое по счету бронирование отеля</div>
      <div>до 31.05.2027</div>
      <div>ALLVSNTPO</div>
      <div>промокод на скидку 7% (не более 3500 ₽)</div>
      <div>на первое бронирование отеля</div>
      <div>до 31.05.2027</div>
      <h2>О сервисе:</h2>
    </main>
    """

    offers = PromokoodAdapter("https://promokood.ru/o/otello").parse(html)

    assert len(offers) == 2
    assert {offer.conditions for offer in offers} == {
        "на любое по счету бронирование отеля",
        "на первое бронирование отеля",
    }
