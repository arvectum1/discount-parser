from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config

from src.shared.config import get_settings
from src.shared.db import reset_db_runtime
from src.sources.base import RawOffer
from src.sources.config import SourceConfig
from src.sources.engine_runtime import ProductionSourcePolicy
from src.sources.parity_runtime import ObservedProductionSourceRuntime
from src.sources.parity_telemetry import (
    ParityRetirementPolicy,
    ParityRunTelemetry,
    RetirementMode,
    SourceParityState,
    advance_parity_state,
    load_parity_state,
    parity_report,
    persist_parity_run,
)


def _offer(url: str, *, code: str = "SAVE10") -> RawOffer:
    external_id = url.rstrip("/").split("/")[-1] or "root"
    return RawOffer(
        source_key="demo",
        external_id=external_id,
        title=f"Скидка 10% {external_id}",
        source_url=url,
        promo_code=code,
        discount_percent=Decimal("10"),
    )


class FakeAcquisition:
    def acquire(self, request):
        return SimpleNamespace(asset=SimpleNamespace(html="<article>offer</article>", source_url=request.url))


class FakeGenericDecoder:
    def __init__(self, *, unusable_suffix: str | None = None) -> None:
        self.unusable_suffix = unusable_suffix

    def decode(self, html: str, *, page_url: str, source_key: str):
        if self.unusable_suffix and page_url.endswith(self.unusable_suffix):
            return SimpleNamespace(usable=False, offers=(), warnings=("record_not_ready",))
        return SimpleNamespace(usable=True, offers=(_offer(page_url),), warnings=())


class RecordingAdapter:
    def __init__(self, config: SourceConfig, calls: list[str], *, mismatch: bool = False) -> None:
        self.config = config
        self.calls = calls
        self.mismatch = mismatch

    def parse(self, html: str) -> list[RawOffer]:
        self.calls.append(self.config.base_url)
        code = "OTHER" if self.mismatch else "SAVE10"
        return [_offer(self.config.base_url, code=code)]


def _runtime(
    state: SourceParityState,
    *,
    calls: list[str],
    mismatch: bool = False,
    unusable_suffix: str | None = None,
    sample_every: int = 3,
) -> ObservedProductionSourceRuntime:
    config = SourceConfig("demo", "Demo", "demo", "https://example.test/", runtime_mode="hybrid")

    def factory(page_config: SourceConfig):
        return RecordingAdapter(page_config, calls, mismatch=mismatch)

    runtime = ObservedProductionSourceRuntime(
        config,
        state=state,
        retirement_policy=ParityRetirementPolicy(
            min_consecutive_pass_pages=6,
            min_clean_runs=3,
            sample_every=sample_every,
        ),
        policy=ProductionSourcePolicy(timeout_s=1, max_bytes=10_000),
        adapter_factory=factory,
    )
    runtime.acquisition = FakeAcquisition()
    runtime.generic_decoder = FakeGenericDecoder(unusable_suffix=unusable_suffix)
    return runtime


def test_observing_promotes_only_after_consecutive_live_evidence() -> None:
    policy = ParityRetirementPolicy(min_consecutive_pass_pages=6, min_clean_runs=3, sample_every=3)
    state = SourceParityState(source_key="demo")

    for run in range(2):
        state = advance_parity_state(
            state,
            ParityRunTelemetry(
                source_key="demo",
                mode_before=state.mode,
                parity_observed_pages=2,
                parity_pass_pages=2,
            ),
            policy=policy,
        )
        assert state.mode is RetirementMode.OBSERVING
        assert state.clean_runs == run + 1

    state = advance_parity_state(
        state,
        ParityRunTelemetry(
            source_key="demo",
            mode_before=state.mode,
            parity_observed_pages=2,
            parity_pass_pages=2,
        ),
        policy=policy,
    )
    assert state.mode is RetirementMode.GENERIC_PRIMARY
    assert state.consecutive_pass_pages == 6
    assert state.parity_rate == 1.0


def test_unobserved_direct_generic_pages_cannot_prove_parity() -> None:
    state = advance_parity_state(
        SourceParityState(source_key="demo"),
        ParityRunTelemetry(
            source_key="demo",
            mode_before=RetirementMode.OBSERVING,
            generic_direct_pages=100,
        ),
        policy=ParityRetirementPolicy(min_consecutive_pass_pages=1, min_clean_runs=1),
    )
    assert state.mode is RetirementMode.OBSERVING
    assert state.clean_runs == 0
    assert state.parity_observed_pages == 0
    assert state.generic_direct_pages == 100


def test_any_live_failure_revokes_generic_primary() -> None:
    state = SourceParityState(
        source_key="demo",
        mode=RetirementMode.GENERIC_PRIMARY,
        parity_observed_pages=40,
        parity_pass_pages=40,
        consecutive_pass_pages=40,
        clean_runs=4,
    )
    updated = advance_parity_state(
        state,
        ParityRunTelemetry(
            source_key="demo",
            mode_before=RetirementMode.GENERIC_PRIMARY,
            parity_observed_pages=1,
            parity_failure_pages=1,
            failure_reasons=("sample mismatch",),
        ),
    )
    assert updated.mode is RetirementMode.OBSERVING
    assert updated.consecutive_pass_pages == 0
    assert updated.clean_runs == 0
    assert updated.parity_failure_pages == 1
    assert updated.last_failure_reason == "sample mismatch"


def test_generic_primary_rotates_oracle_and_skips_legacy_on_direct_pages() -> None:
    calls: list[str] = []
    state = SourceParityState(
        source_key="demo",
        mode=RetirementMode.GENERIC_PRIMARY,
        parity_observed_pages=30,
        parity_pass_pages=30,
        consecutive_pass_pages=30,
        clean_runs=3,
    )
    runtime = _runtime(state, calls=calls, sample_every=3)
    selected = (
        "https://example.test/a",
        "https://example.test/b",
        "https://example.test/c",
    )

    offers, decoded, warnings, generic_pages, legacy_pages, failures = runtime._decode_selected(selected, ())

    assert decoded == 3
    assert len(offers) == 3
    assert warnings == []
    assert generic_pages == 3
    assert legacy_pages == 0
    assert failures == 0
    assert calls == ["https://example.test/a"]
    assert runtime.last_telemetry.parity_observed_pages == 1
    assert runtime.last_telemetry.parity_pass_pages == 1
    assert runtime.last_telemetry.generic_direct_pages == 2


def test_generic_not_ready_forces_emergency_legacy_and_demotion_evidence() -> None:
    calls: list[str] = []
    state = SourceParityState(
        source_key="demo",
        mode=RetirementMode.GENERIC_PRIMARY,
        parity_observed_pages=30,
        parity_pass_pages=30,
        consecutive_pass_pages=30,
        clean_runs=3,
    )
    runtime = _runtime(state, calls=calls, unusable_suffix="/b", sample_every=3)
    selected = (
        "https://example.test/a",
        "https://example.test/b",
        "https://example.test/c",
    )

    offers, _, warnings, _, legacy_pages, failures = runtime._decode_selected(selected, ())

    assert len(offers) == 3
    assert legacy_pages == 1
    assert failures == 1
    assert calls == ["https://example.test/a", "https://example.test/b"]
    assert runtime.last_telemetry.emergency_fallback_pages == 1
    assert runtime.last_telemetry.parity_failure_pages == 1
    assert any("generic_not_ready_fallback" in value for value in warnings)

    updated = advance_parity_state(state, runtime.last_telemetry)
    assert updated.mode is RetirementMode.OBSERVING


def test_sampled_mismatch_returns_legacy_for_that_page_and_revokes_mode() -> None:
    calls: list[str] = []
    state = SourceParityState(
        source_key="demo",
        mode=RetirementMode.GENERIC_PRIMARY,
        parity_observed_pages=30,
        parity_pass_pages=30,
        consecutive_pass_pages=30,
        clean_runs=3,
    )
    runtime = _runtime(state, calls=calls, mismatch=True, sample_every=3)
    offers, _, warnings, _, legacy_pages, failures = runtime._decode_selected(
        ("https://example.test/a", "https://example.test/b", "https://example.test/c"),
        (),
    )
    by_id = {offer.external_id: offer for offer in offers}
    assert by_id["a"].promo_code == "OTHER"
    assert by_id["b"].promo_code == "SAVE10"
    assert legacy_pages == 1
    assert failures == 1
    assert any("generic_parity_fallback" in value for value in warnings)
    assert advance_parity_state(state, runtime.last_telemetry).mode is RetirementMode.OBSERVING


def test_0010_persists_and_reports_parity_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "parity.db"
    monkeypatch.setenv("DP_DATABASE_URL", f"sqlite:///{db_path}")
    get_settings.cache_clear()
    reset_db_runtime()
    try:
        command.upgrade(Config("alembic.ini"), "head")
        policy = ParityRetirementPolicy(min_consecutive_pass_pages=2, min_clean_runs=1, sample_every=2)
        updated = persist_parity_run(
            "promokood",
            ParityRunTelemetry(
                source_key="promokood",
                mode_before=RetirementMode.OBSERVING,
                parity_observed_pages=2,
                parity_pass_pages=2,
            ),
            policy=policy,
        )
        assert updated.mode is RetirementMode.GENERIC_PRIMARY
        reloaded = load_parity_state("promokood")
        assert reloaded.mode is RetirementMode.GENERIC_PRIMARY
        assert reloaded.parity_observed_pages == 2
        rows = parity_report()
        assert len(rows) == 1
        assert rows[0].source_key == "promokood"
        assert rows[0].parity_rate == 1.0
    finally:
        reset_db_runtime()
        get_settings.cache_clear()
