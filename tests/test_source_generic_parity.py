from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from arvectum_data.acquisition import PageSnapshot
from src.sources.adapters.berikod import BerikodAdapter
from src.sources.adapters.promko import PromkoAdapter
from src.sources.adapters.promokodi_net_ru import PromokodiNetRuAdapter
from src.sources.adapters.promokodik import PromokodikAdapter
from src.sources.adapters.promokood import PromokoodAdapter
from src.sources.config import SourceConfig
from src.sources.engine_runtime import ProductionSourceRuntime
from src.sources.generic_multi_record import (
    GenericMultiRecordOfferDecoder,
    GenericOfferDecodeResult,
    compare_offer_sets,
)

FIXTURES = Path("tests/fixtures")
ADAPTERS = {
    "berikod": BerikodAdapter,
    "promko": PromkoAdapter,
    "promokodi_net_ru": PromokodiNetRuAdapter,
    "promokodik": PromokodikAdapter,
    "promokood": PromokoodAdapter,
}


def _corpus():
    return json.loads((FIXTURES / "parser_corpus.json").read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _corpus(), ids=lambda case: case["adapter"])
def test_generic_multi_record_decoder_is_safe_superset_of_adapter_corpus(case: dict) -> None:
    html = (FIXTURES / case["fixture"]).read_text(encoding="utf-8")
    legacy = ADAPTERS[case["adapter"]](case["base_url"]).parse(html)
    generic = GenericMultiRecordOfferDecoder().decode(
        html,
        page_url=case["base_url"],
        source_key=case["adapter"],
    )
    assert generic.usable, (case["adapter"], generic.warnings, generic.records)
    report = compare_offer_sets(legacy, generic.offers)
    assert report.safe_to_adopt, (case["adapter"], report)
    assert report.matched_count == case["expected_count"]


def test_parity_gate_rejects_changed_legacy_populated_business_field() -> None:
    html = (FIXTURES / "promokodik.html").read_text(encoding="utf-8")
    legacy = PromokodikAdapter("https://promokodik.ru/").parse(html)
    generic = GenericMultiRecordOfferDecoder().decode(
        html,
        page_url="https://promokodik.ru/",
        source_key="promokodik",
    )
    changed = list(generic.offers)
    changed[0] = replace(changed[0], discount_percent=Decimal("99"))
    report = compare_offer_sets(legacy, changed)
    assert not report.safe_to_adopt
    assert any(item.field == "discount_percent" for item in report.mismatches)


def test_parity_gate_allows_generic_enrichment_of_legacy_null_field() -> None:
    html = (FIXTURES / "berikod.html").read_text(encoding="utf-8")
    legacy = BerikodAdapter("https://berikod.ru/").parse(html)
    generic = GenericMultiRecordOfferDecoder().decode(
        html,
        page_url="https://berikod.ru/",
        source_key="berikod",
    )
    enriched = list(generic.offers)
    enriched[0] = replace(enriched[0], conditions="Дополнительное автоматически найденное условие")
    assert legacy[0].conditions is None
    assert compare_offer_sets(legacy, enriched).safe_to_adopt


class _FixtureTransport:
    name = "fixture-transport"

    def __init__(self, html: str) -> None:
        self.html = html
        self.requests = 0

    def fetch(self, request):
        self.requests += 1
        return PageSnapshot(
            requested_url=request.url,
            final_url=request.url,
            status_code=200,
            content_type="text/html; charset=utf-8",
            body=self.html.encode("utf-8"),
            headers={},
            rendered=False,
        )

    def cached_html_items(self):
        return ()


def test_runtime_returns_generic_records_when_same_html_proves_parity() -> None:
    html = (FIXTURES / "berikod.html").read_text(encoding="utf-8")
    url = "https://berikod.ru/"
    transport = _FixtureTransport(html)
    runtime = ProductionSourceRuntime(
        SourceConfig("berikod", "БериКод", "berikod", url, runtime_mode="hybrid"),
        transport=transport,
    )
    offers, decoded_pages, warnings, generic_pages, legacy_pages, parity_failures = runtime._decode_selected((url,), ())
    assert len(offers) == 2
    assert decoded_pages == 1
    assert generic_pages == 1
    assert legacy_pages == 0
    assert parity_failures == 0
    assert not [item for item in warnings if "fallback" in item]
    assert all(item.raw_payload["dp_engine"]["decoder"] == "generic_multi_record" for item in offers)
    assert transport.requests == 1


class _ChangedGenericDecoder:
    def __init__(self) -> None:
        self.delegate = GenericMultiRecordOfferDecoder()

    def decode(self, html: str, *, page_url: str, source_key: str) -> GenericOfferDecodeResult:
        result = self.delegate.decode(html, page_url=page_url, source_key=source_key)
        offers = list(result.offers)
        offers[0] = replace(offers[0], title="Несовпадающий generic title")
        return replace(result, offers=tuple(offers))


def test_runtime_falls_back_to_legacy_on_page_parity_mismatch_without_refetch() -> None:
    html = (FIXTURES / "berikod.html").read_text(encoding="utf-8")
    url = "https://berikod.ru/"
    transport = _FixtureTransport(html)
    runtime = ProductionSourceRuntime(
        SourceConfig("berikod", "БериКод", "berikod", url, runtime_mode="hybrid"),
        transport=transport,
        generic_decoder=_ChangedGenericDecoder(),
    )
    offers, decoded_pages, warnings, generic_pages, legacy_pages, parity_failures = runtime._decode_selected((url,), ())
    assert len(offers) == 2
    assert decoded_pages == 1
    assert generic_pages == 0
    assert legacy_pages == 1
    assert parity_failures == 1
    assert any(item.startswith("generic_parity_fallback:") for item in warnings)
    assert all(item.raw_payload["dp_engine"]["decoder"] == "legacy_adapter" for item in offers)
    assert transport.requests == 1
