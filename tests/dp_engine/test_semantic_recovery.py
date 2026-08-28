from __future__ import annotations

from dataclasses import dataclass

from arvectum_data import (
    AcquisitionAttempt,
    AcquisitionError,
    AcquisitionRequest,
    AcquisitionResult,
    Candidate,
    ExtractionQuality,
    ExtractionResult,
    FieldDecision,
    FieldSpec,
    FieldStatus,
    RawAsset,
    RenderMode,
    SemanticRecoveryPolicy,
    URLExtractionPipeline,
)


@dataclass
class _Renderer:
    name: str = "fake-browser"


class ScriptedAcquisition:
    def __init__(self, *, fail_render: bool = False, auto_rendered: bool = False) -> None:
        self.fail_render = fail_render
        self.auto_rendered = auto_rendered
        self.renderer = _Renderer()
        self.calls: list[RenderMode] = []

    def acquire(self, request: AcquisitionRequest) -> AcquisitionResult:
        self.calls.append(request.render_mode)
        rendered = request.render_mode is RenderMode.ALWAYS or self.auto_rendered
        if rendered and self.fail_render:
            raise AcquisitionError("browser failed for https://secret.example/?token=x")
        marker = "rendered" if rendered else "static"
        asset = RawAsset(
            request.resolved_asset_id,
            source_url=request.url,
            html=marker,
            metadata={"acquisition": {"rendered": rendered}},
        )
        return AcquisitionResult(
            asset,
            (
                AcquisitionAttempt(
                    "fake-browser" if rendered else "fake-http",
                    True,
                    "ok",
                    200,
                    request.url,
                    rendered,
                ),
            ),
            (f"{marker}-warning",),
        )


class ScriptedExtraction:
    def __init__(
        self,
        static_statuses: dict[str, FieldStatus],
        rendered_statuses: dict[str, FieldStatus],
    ) -> None:
        self.static_statuses = static_statuses
        self.rendered_statuses = rendered_statuses
        self.calls: list[str] = []

    def extract(self, asset: RawAsset, fields) -> ExtractionResult:
        marker = asset.html or "static"
        self.calls.append(marker)
        statuses = self.rendered_statuses if marker == "rendered" else self.static_statuses
        decisions = {}
        for field in fields:
            status = statuses[field.key]
            selected = None
            candidates = ()
            if status in {
                FieldStatus.AUTO_SELECTED,
                FieldStatus.CONFIRMED,
                FieldStatus.NEEDS_CONFIRMATION,
            }:
                confidence = 0.91 if marker == "rendered" else 0.85
                selected = Candidate(field.key, f"{marker}-{field.key}", confidence, "test")
                candidates = (selected,)
            decisions[field.key] = FieldDecision(
                field,
                status,
                selected,
                candidates,
                marker,
            )
        return ExtractionResult(asset, decisions)


def fields() -> tuple[FieldSpec, ...]:
    return (
        FieldSpec("title"),
        FieldSpec("price", required=True),
    )


def pipeline(
    static: dict[str, FieldStatus],
    rendered: dict[str, FieldStatus],
    *,
    fail_render: bool = False,
    auto_rendered: bool = False,
    policy: SemanticRecoveryPolicy | None = None,
):
    acquisition = ScriptedAcquisition(
        fail_render=fail_render,
        auto_rendered=auto_rendered,
    )
    extraction = ScriptedExtraction(static, rendered)
    return (
        URLExtractionPipeline(
            acquisition=acquisition,
            extraction=extraction,
            learning_enabled=False,
            semantic_recovery_policy=policy,
        ),
        acquisition,
        extraction,
    )


def request(mode: RenderMode = RenderMode.AUTO) -> AcquisitionRequest:
    return AcquisitionRequest("https://example.test/item", render_mode=mode)


def test_auto_required_unresolved_recovers_with_rendered_result():
    pipe, acquisition, extraction = pipeline(
        {"title": FieldStatus.AUTO_SELECTED, "price": FieldStatus.UNRESOLVED},
        {"title": FieldStatus.AUTO_SELECTED, "price": FieldStatus.AUTO_SELECTED},
    )

    result = pipe.extract(request(), fields())

    assert result.ready
    assert result.asset.html == "rendered"
    assert acquisition.calls == [RenderMode.AUTO, RenderMode.ALWAYS]
    assert extraction.calls == ["static", "rendered"]
    assert [attempt.rendered for attempt in result.acquisition.attempts] == [False, True]
    assert "semantic_render_recovery_selected:rendered" in result.acquisition.warnings


def test_ready_static_result_does_not_render():
    pipe, acquisition, extraction = pipeline(
        {"title": FieldStatus.AUTO_SELECTED, "price": FieldStatus.AUTO_SELECTED},
        {"title": FieldStatus.AUTO_SELECTED, "price": FieldStatus.AUTO_SELECTED},
    )

    result = pipe.extract(request(), fields())

    assert result.asset.html == "static"
    assert acquisition.calls == [RenderMode.AUTO]
    assert extraction.calls == ["static"]


def test_optional_unresolved_alone_does_not_render():
    pipe, acquisition, _ = pipeline(
        {"title": FieldStatus.UNRESOLVED, "price": FieldStatus.AUTO_SELECTED},
        {"title": FieldStatus.AUTO_SELECTED, "price": FieldStatus.AUTO_SELECTED},
    )

    pipe.extract(request(), fields())

    assert acquisition.calls == [RenderMode.AUTO]


def test_never_mode_blocks_semantic_recovery():
    pipe, acquisition, _ = pipeline(
        {"title": FieldStatus.AUTO_SELECTED, "price": FieldStatus.UNRESOLVED},
        {"title": FieldStatus.AUTO_SELECTED, "price": FieldStatus.AUTO_SELECTED},
    )

    result = pipe.extract(request(RenderMode.NEVER), fields())

    assert result.unresolved_required_fields == ("price",)
    assert acquisition.calls == [RenderMode.NEVER]


def test_always_mode_does_not_double_render():
    pipe, acquisition, extraction = pipeline(
        {"title": FieldStatus.AUTO_SELECTED, "price": FieldStatus.UNRESOLVED},
        {"title": FieldStatus.AUTO_SELECTED, "price": FieldStatus.UNRESOLVED},
    )

    pipe.extract(request(RenderMode.ALWAYS), fields())

    assert acquisition.calls == [RenderMode.ALWAYS]
    assert extraction.calls == ["rendered"]


def test_auto_acquisition_that_already_rendered_does_not_render_again():
    pipe, acquisition, extraction = pipeline(
        {"title": FieldStatus.AUTO_SELECTED, "price": FieldStatus.UNRESOLVED},
        {"title": FieldStatus.AUTO_SELECTED, "price": FieldStatus.UNRESOLVED},
        auto_rendered=True,
    )

    pipe.extract(request(), fields())

    assert acquisition.calls == [RenderMode.AUTO]
    assert extraction.calls == ["rendered"]


def test_render_failure_retains_static_and_records_safe_failure():
    pipe, acquisition, extraction = pipeline(
        {"title": FieldStatus.AUTO_SELECTED, "price": FieldStatus.UNRESOLVED},
        {"title": FieldStatus.AUTO_SELECTED, "price": FieldStatus.AUTO_SELECTED},
        fail_render=True,
    )

    result = pipe.extract(request(), fields())

    assert result.asset.html == "static"
    assert result.unresolved_required_fields == ("price",)
    assert acquisition.calls == [RenderMode.AUTO, RenderMode.ALWAYS]
    assert extraction.calls == ["static"]
    assert result.acquisition.attempts[-1].success is False
    assert result.acquisition.attempts[-1].reason == "semantic_required_recovery:AcquisitionError"
    assert all("secret.example" not in warning for warning in result.acquisition.warnings)


def test_rendered_review_is_better_than_required_unresolved():
    pipe, _, _ = pipeline(
        {"title": FieldStatus.AUTO_SELECTED, "price": FieldStatus.UNRESOLVED},
        {"title": FieldStatus.AUTO_SELECTED, "price": FieldStatus.NEEDS_CONFIRMATION},
    )

    result = pipe.extract(request(), fields())

    assert result.asset.html == "rendered"
    assert result.requires_confirmation
    assert result.unresolved_required_fields == ()


def test_equal_rendered_quality_retains_static():
    pipe, _, _ = pipeline(
        {"title": FieldStatus.AUTO_SELECTED, "price": FieldStatus.UNRESOLVED},
        {"title": FieldStatus.AUTO_SELECTED, "price": FieldStatus.UNRESOLVED},
    )

    result = pipe.extract(request(), fields())

    assert result.asset.html == "static"
    assert "semantic_render_recovery_selected:static" in result.acquisition.warnings


def test_better_optional_result_breaks_tie_when_required_stays_unresolved():
    pipe, _, _ = pipeline(
        {"title": FieldStatus.UNRESOLVED, "price": FieldStatus.UNRESOLVED},
        {"title": FieldStatus.AUTO_SELECTED, "price": FieldStatus.UNRESOLVED},
    )

    result = pipe.extract(request(), fields())

    assert result.asset.html == "rendered"
    assert result.unresolved_required_fields == ("price",)


def test_disabled_policy_preserves_pre_010_behavior():
    pipe, acquisition, _ = pipeline(
        {"title": FieldStatus.AUTO_SELECTED, "price": FieldStatus.UNRESOLVED},
        {"title": FieldStatus.AUTO_SELECTED, "price": FieldStatus.AUTO_SELECTED},
        policy=SemanticRecoveryPolicy(enabled=False),
    )

    result = pipe.extract(request(), fields())

    assert result.asset.html == "static"
    assert acquisition.calls == [RenderMode.AUTO]


def test_quality_prefers_required_review_over_required_unresolved():
    required = FieldSpec("price", required=True)
    unresolved = ExtractionResult(
        RawAsset("a"),
        {"price": FieldDecision(required, FieldStatus.UNRESOLVED, None)},
    )
    candidate = Candidate("price", "x", 0.8, "test")
    review = ExtractionResult(
        RawAsset("b"),
        {
            "price": FieldDecision(
                required,
                FieldStatus.NEEDS_CONFIRMATION,
                candidate,
                (candidate,),
            )
        },
    )

    policy = SemanticRecoveryPolicy()

    assert policy.prefer_rendered(unresolved, review)
    assert ExtractionQuality.from_result(review).unresolved_required == 0
    assert ExtractionQuality.from_result(review).review_required == 1


def test_quality_does_not_select_rendered_on_confidence_only():
    field = FieldSpec("price", required=True)
    low = Candidate("price", "same", 0.81, "test")
    high = Candidate("price", "same", 0.99, "test")
    low_result = ExtractionResult(
        RawAsset("a"),
        {"price": FieldDecision(field, FieldStatus.AUTO_SELECTED, low, (low,))},
    )
    high_result = ExtractionResult(
        RawAsset("b"),
        {"price": FieldDecision(field, FieldStatus.AUTO_SELECTED, high, (high,))},
    )

    assert not SemanticRecoveryPolicy().prefer_rendered(low_result, high_result)


def test_acquisition_warnings_from_both_paths_are_preserved():
    pipe, _, _ = pipeline(
        {"title": FieldStatus.AUTO_SELECTED, "price": FieldStatus.UNRESOLVED},
        {"title": FieldStatus.AUTO_SELECTED, "price": FieldStatus.AUTO_SELECTED},
    )

    result = pipe.extract(request(), fields())

    assert "static-warning" in result.acquisition.warnings
    assert "rendered-warning" in result.acquisition.warnings


def test_trigger_warning_lists_only_field_keys_not_values():
    pipe, _, _ = pipeline(
        {"title": FieldStatus.AUTO_SELECTED, "price": FieldStatus.UNRESOLVED},
        {"title": FieldStatus.AUTO_SELECTED, "price": FieldStatus.AUTO_SELECTED},
    )

    result = pipe.extract(request(), fields())
    trigger = next(
        warning
        for warning in result.acquisition.warnings
        if warning.startswith("semantic_render_recovery_triggered:")
    )

    assert trigger == "semantic_render_recovery_triggered:price"
    assert "rendered-price" not in trigger
