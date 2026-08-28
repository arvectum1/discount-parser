from __future__ import annotations

from dataclasses import dataclass

import pytest

from arvectum_data import (
    AcquisitionRequest,
    AcquisitionResult,
    FieldSpec,
    RenderMode,
    URLExtractionPipeline,
)


@dataclass
class Asset:
    asset_id: str = "asset-1"


class FakeAcquisition:
    def __init__(self, result=None, error=None):
        self.result = result or AcquisitionResult(asset=Asset(), attempts=())
        self.error = error
        self.requests = []

    def acquire(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.result


class FakeExtractionResult:
    def __init__(
        self,
        asset,
        *,
        requires_confirmation=False,
        unresolved_required_fields=(),
        values=None,
    ):
        self.asset = asset
        self.decisions = {}
        self.requires_confirmation = requires_confirmation
        self.unresolved_required_fields = tuple(unresolved_required_fields)
        self._values = {} if values is None else dict(values)

    def values(self, *, include_unconfirmed=False):
        return dict(self._values)


class FakeExtraction:
    def __init__(self, result=None):
        self.result = result
        self.extract_calls = []
        self.confirm_calls = []

    def extract(self, asset, fields):
        self.extract_calls.append((asset, tuple(fields)))
        return self.result or FakeExtractionResult(asset, values={"title": "Example"})

    def confirm(self, result, selections):
        self.confirm_calls.append((result, dict(selections)))
        return FakeExtractionResult(result.asset, values={"title": "Confirmed"})


def test_pipeline_chains_acquisition_into_extraction():
    acquisition = FakeAcquisition()
    extraction = FakeExtraction()
    pipeline = URLExtractionPipeline(acquisition=acquisition, extraction=extraction)

    result = pipeline.extract(
        AcquisitionRequest("https://example.test/item"),
        [FieldSpec("title")],
    )

    assert extraction.extract_calls[0][0] is acquisition.result.asset
    assert result.values() == {"title": "Example"}
    assert result.ready


def test_extract_url_builds_acquisition_request_with_transport_controls():
    acquisition = FakeAcquisition()
    pipeline = URLExtractionPipeline(
        acquisition=acquisition,
        extraction=FakeExtraction(),
    )

    pipeline.extract_url(
        "https://example.test/item",
        [FieldSpec("title")],
        asset_id="known-id",
        headers={"X-Test": "1"},
        timeout_s=3.0,
        max_bytes=1234,
        render_mode=RenderMode.ALWAYS,
    )

    request = acquisition.requests[0]
    assert request.asset_id == "known-id"
    assert request.headers == {"X-Test": "1"}
    assert request.timeout_s == 3.0
    assert request.max_bytes == 1234
    assert request.render_mode is RenderMode.ALWAYS


def test_ready_is_false_when_any_field_requires_confirmation():
    acquisition = FakeAcquisition()
    extraction = FakeExtraction(
        FakeExtractionResult(acquisition.result.asset, requires_confirmation=True)
    )

    result = URLExtractionPipeline(
        acquisition=acquisition,
        extraction=extraction,
    ).extract(AcquisitionRequest("https://example.test"), [])

    assert not result.ready


def test_ready_is_false_when_required_fields_are_unresolved():
    acquisition = FakeAcquisition()
    extraction = FakeExtraction(
        FakeExtractionResult(
            acquisition.result.asset,
            unresolved_required_fields=("price",),
        )
    )

    result = URLExtractionPipeline(
        acquisition=acquisition,
        extraction=extraction,
    ).extract(AcquisitionRequest("https://example.test"), [])

    assert not result.ready
    assert result.unresolved_required_fields == ("price",)


def test_confirmation_preserves_acquisition_evidence():
    acquisition = FakeAcquisition()
    extraction = FakeExtraction()
    pipeline = URLExtractionPipeline(acquisition=acquisition, extraction=extraction)
    result = pipeline.extract(AcquisitionRequest("https://example.test"), [])

    confirmed = pipeline.confirm(result, {"title": "candidate-id"})

    assert confirmed.acquisition is result.acquisition
    assert confirmed.values() == {"title": "Confirmed"}
    assert extraction.confirm_calls[0][1] == {"title": "candidate-id"}


def test_extraction_and_provider_configuration_are_mutually_exclusive():
    with pytest.raises(ValueError, match="either extraction or providers"):
        URLExtractionPipeline(extraction=FakeExtraction(), providers=[])


def test_acquisition_failure_stops_before_extraction():
    acquisition = FakeAcquisition(error=RuntimeError("network failed"))
    extraction = FakeExtraction()
    pipeline = URLExtractionPipeline(acquisition=acquisition, extraction=extraction)

    with pytest.raises(RuntimeError, match="network failed"):
        pipeline.extract(AcquisitionRequest("https://example.test"), [])

    assert extraction.extract_calls == []


def test_values_forwards_include_unconfirmed_flag():
    class FlagResult(FakeExtractionResult):
        def values(self, *, include_unconfirmed=False):
            return {"include_unconfirmed": include_unconfirmed}

    acquisition = FakeAcquisition()
    extraction = FakeExtraction(FlagResult(acquisition.result.asset))
    result = URLExtractionPipeline(
        acquisition=acquisition,
        extraction=extraction,
    ).extract(AcquisitionRequest("https://example.test"), [])

    assert result.values(include_unconfirmed=True) == {"include_unconfirmed": True}
