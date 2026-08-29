from __future__ import annotations

from dataclasses import dataclass, replace

from arvectum_data.acquisition import AcquisitionRequest, RenderMode
from arvectum_data.crawl import TargetPageAssessment

from src.sources.base import RawOffer
from src.sources.config import SourceConfig
from src.sources.engine_runtime import (
    AdapterFactory,
    DiscountParserHTTPTransport,
    ProductionSourcePolicy,
    ProductionSourceRuntime,
    SourceCollectionResult,
    _diagnostic,
)
from src.sources.generic_multi_record import SourceParityReport, compare_offer_sets
from src.sources.parity_telemetry import (
    ParityRetirementPolicy,
    ParityRunTelemetry,
    RetirementMode,
    SourceParityState,
    load_parity_state,
    persist_parity_run,
)
from src.sources.registry import build_adapter


@dataclass(frozen=True, slots=True)
class ObservedSourceCollectionResult:
    offers: tuple[RawOffer, ...]
    runtime_mode: str
    discovered_urls: int = 0
    selected_urls: int = 0
    decoded_pages: int = 0
    fallback_used: bool = False
    warnings: tuple[str, ...] = ()
    generic_pages: int = 0
    legacy_pages: int = 0
    parity_failures: int = 0
    retirement_mode_before: str = RetirementMode.OBSERVING.value
    retirement_mode_after: str = RetirementMode.OBSERVING.value
    parity_observed_pages: int = 0
    parity_pass_pages: int = 0
    oracle_pages: int = 0
    direct_generic_pages: int = 0
    emergency_fallback_pages: int = 0

    @classmethod
    def from_collection(
        cls,
        collection: SourceCollectionResult,
        telemetry: ParityRunTelemetry,
        state_after: SourceParityState,
    ) -> "ObservedSourceCollectionResult":
        return cls(
            offers=collection.offers,
            runtime_mode=collection.runtime_mode,
            discovered_urls=collection.discovered_urls,
            selected_urls=collection.selected_urls,
            decoded_pages=collection.decoded_pages,
            fallback_used=collection.fallback_used,
            warnings=collection.warnings,
            generic_pages=collection.generic_pages,
            legacy_pages=collection.legacy_pages,
            parity_failures=collection.parity_failures,
            retirement_mode_before=telemetry.mode_before.value,
            retirement_mode_after=state_after.mode.value,
            parity_observed_pages=telemetry.parity_observed_pages,
            parity_pass_pages=telemetry.parity_pass_pages,
            oracle_pages=telemetry.parity_observed_pages,
            direct_generic_pages=telemetry.generic_direct_pages,
            emergency_fallback_pages=telemetry.emergency_fallback_pages,
        )


class ObservedProductionSourceRuntime(ProductionSourceRuntime):
    """DP-016 runtime with durable DP-017 observation and staged oracle retirement."""

    def __init__(
        self,
        config: SourceConfig,
        *,
        state: SourceParityState,
        retirement_policy: ParityRetirementPolicy | None = None,
        policy: ProductionSourcePolicy | None = None,
        transport: DiscountParserHTTPTransport | None = None,
        adapter_factory: AdapterFactory = build_adapter,
    ) -> None:
        super().__init__(
            config,
            policy=policy,
            transport=transport,
            adapter_factory=adapter_factory,
        )
        self.parity_state = state
        self.retirement_policy = retirement_policy or ParityRetirementPolicy()
        self.last_telemetry = ParityRunTelemetry(
            source_key=config.key,
            mode_before=state.mode,
        )

    def _decode_selected(
        self,
        selected: tuple[str, ...],
        assessments: tuple[TargetPageAssessment, ...],
    ) -> tuple[list[RawOffer], int, list[str], int, int, int]:
        if self.parity_state.mode is RetirementMode.OBSERVING:
            result = super()._decode_selected(selected, assessments)
            _, _, warnings, generic_pages, legacy_pages, failures = result
            observed = generic_pages + legacy_pages
            self.last_telemetry = ParityRunTelemetry(
                source_key=self.config.key,
                mode_before=RetirementMode.OBSERVING,
                parity_observed_pages=observed,
                parity_pass_pages=generic_pages,
                parity_failure_pages=failures,
                failure_reasons=tuple(
                    item for item in warnings if "generic_" in item and "fallback" in item
                )[:20],
            )
            return result
        return self._decode_generic_primary(selected, assessments)

    def _sample_required(self, index: int, total: int) -> bool:
        if total <= 0:
            return False
        span = min(self.retirement_policy.sample_every, total)
        # The persistent observed-page count rotates the sampled slot from one
        # live run to the next; stable URLs therefore do not create a permanently
        # unobserved subset.
        slot = self.parity_state.parity_observed_pages % span
        return index % self.retirement_policy.sample_every == slot

    def _legacy_parser(self, effective_url: str):
        page_config = replace(
            self.config,
            base_url=effective_url,
            runtime_mode="legacy",
        )
        adapter = self.adapter_factory(page_config)
        parser = getattr(adapter, "parse", None)
        return parser if callable(parser) else None

    def _decode_generic_primary(
        self,
        selected: tuple[str, ...],
        assessments: tuple[TargetPageAssessment, ...],
    ) -> tuple[list[RawOffer], int, list[str], int, int, int]:
        by_url = {item.url: item for item in assessments}
        offers: list[RawOffer] = []
        warnings: list[str] = []
        seen: set[tuple[str, str]] = set()
        decoded_pages = 0
        generic_pages = 0
        legacy_pages = 0
        parity_failures = 0
        parity_observed = 0
        parity_passes = 0
        direct_generic = 0
        emergency_fallback = 0
        failure_reasons: list[str] = []

        for index, page_url in enumerate(selected):
            try:
                acquired = self.acquisition.acquire(
                    AcquisitionRequest(
                        url=page_url,
                        timeout_s=self.policy.timeout_s,
                        max_bytes=self.policy.max_bytes,
                        render_mode=RenderMode.AUTO,
                    )
                )
                html = acquired.asset.html
                if not html:
                    continue
                decoded_pages += 1
                effective_url = acquired.asset.source_url or page_url
                assessment = by_url.get(page_url)
                chosen: list[RawOffer] = []
                decoder_name = "generic_multi_record_direct"
                parity: SourceParityReport | None = None

                try:
                    generic = self.generic_decoder.decode(
                        html,
                        page_url=effective_url,
                        source_key=self.config.key,
                    )
                except Exception as exc:
                    generic = None
                    reason = f"generic_decode_fallback:{_diagnostic(exc)}"
                    warnings.append(reason)
                    failure_reasons.append(reason)

                if generic is not None and generic.usable:
                    if self._sample_required(index, len(selected)):
                        parser = self._legacy_parser(effective_url)
                        if parser is None:
                            reason = f"generic_sample_oracle_missing:{page_url}"
                            warnings.append(reason)
                            failure_reasons.append(reason)
                            parity_failures += 1
                            parity_observed += 1
                            chosen = list(generic.offers)
                            generic_pages += 1
                            decoder_name = "generic_multi_record_unverified"
                        else:
                            legacy_decoded = list(parser(html))
                            parity = compare_offer_sets(legacy_decoded, generic.offers)
                            parity_observed += 1
                            if parity.safe_to_adopt:
                                parity_passes += 1
                                generic_pages += 1
                                chosen = list(generic.offers)
                                decoder_name = "generic_multi_record_sampled"
                            else:
                                parity_failures += 1
                                legacy_pages += 1
                                chosen = legacy_decoded
                                decoder_name = "legacy_adapter"
                                reason = f"generic_parity_fallback:{page_url}:{parity.diagnostic()}"
                                warnings.append(reason)
                                failure_reasons.append(reason)
                    else:
                        chosen = list(generic.offers)
                        generic_pages += 1
                        direct_generic += 1
                else:
                    detail = "generic_error"
                    if generic is not None:
                        detail = ",".join(generic.warnings[:4]) or "no_ready_records"
                    reason = f"generic_not_ready_fallback:{page_url}:{detail}"
                    warnings.append(reason)
                    failure_reasons.append(reason)
                    parity_failures += 1
                    parity_observed += 1
                    emergency_fallback += 1
                    parser = self._legacy_parser(effective_url)
                    if parser is None:
                        warnings.append(f"decoder_missing:{page_url}")
                        continue
                    chosen = list(parser(html))
                    legacy_pages += 1
                    decoder_name = "legacy_emergency_fallback"

                for raw in chosen:
                    identity = (raw.source_key, raw.external_id)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    offers.append(
                        self._with_engine_provenance(
                            raw,
                            page_url,
                            assessment,
                            decoder=decoder_name,
                            parity=parity,
                        )
                    )
            except Exception as exc:
                warning = f"page_decode_failed:{_diagnostic(exc)}"
                warnings.append(warning)
                failure_reasons.append(warning)

        self.last_telemetry = ParityRunTelemetry(
            source_key=self.config.key,
            mode_before=RetirementMode.GENERIC_PRIMARY,
            parity_observed_pages=parity_observed,
            parity_pass_pages=parity_passes,
            parity_failure_pages=parity_failures,
            generic_direct_pages=direct_generic,
            emergency_fallback_pages=emergency_fallback,
            failure_reasons=tuple(failure_reasons[:20]),
        )
        return offers, decoded_pages, warnings, generic_pages, legacy_pages, parity_failures


def collect_source_offers_observed(
    config: SourceConfig,
    *,
    policy: ProductionSourcePolicy | None = None,
    retirement_policy: ParityRetirementPolicy | None = None,
    transport: DiscountParserHTTPTransport | None = None,
    adapter_factory: AdapterFactory = build_adapter,
) -> ObservedSourceCollectionResult:
    state = load_parity_state(config.key)
    runtime = ObservedProductionSourceRuntime(
        config,
        state=state,
        retirement_policy=retirement_policy,
        policy=policy,
        transport=transport,
        adapter_factory=adapter_factory,
    )
    collection = runtime.collect()
    telemetry = runtime.last_telemetry
    if config.runtime_mode != "hybrid" or collection.fallback_used and not collection.decoded_pages:
        state_after = state
    else:
        state_after = persist_parity_run(
            config.key,
            telemetry,
            policy=retirement_policy,
        )
    return ObservedSourceCollectionResult.from_collection(collection, telemetry, state_after)
