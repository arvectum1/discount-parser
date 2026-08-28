from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from .acquisition import (
    AcquisitionAttempt,
    AcquisitionEngine,
    AcquisitionRequest,
    AcquisitionResult,
    RenderMode,
)
from .engine import (
    AutoDiscoveryProvider,
    CandidateProvider,
    ExtractionEngine,
    ExtractionResult,
    FieldSpec,
)
from .profile_lifecycle import (
    InMemorySiteProfileStore,
    ProfilePruneReport,
    SiteProfileStore,
)
from .profiles import (
    ConfirmationLearner,
    LearningEvent,
    LearningPolicy,
    ProfileAwareProvider,
)
from .recovery import SemanticRecoveryPolicy


@dataclass(frozen=True, slots=True)
class URLExtractionResult:
    """End-to-end result retaining acquisition, extraction and learning evidence."""

    acquisition: AcquisitionResult
    extraction: ExtractionResult
    learning_events: tuple[LearningEvent, ...] = ()
    learning_warnings: tuple[str, ...] = ()

    @property
    def asset(self):
        return self.extraction.asset

    @property
    def requires_confirmation(self) -> bool:
        return self.extraction.requires_confirmation

    @property
    def unresolved_required_fields(self) -> tuple[str, ...]:
        return self.extraction.unresolved_required_fields

    @property
    def ready(self) -> bool:
        """True only when downstream use needs neither review nor required-field repair."""

        return not self.requires_confirmation and not self.unresolved_required_fields

    def values(self, *, include_unconfirmed: bool = False) -> dict[str, Any]:
        return self.extraction.values(include_unconfirmed=include_unconfirmed)


class URLExtractionPipeline:
    """One governed URL -> acquisition -> learned discovery -> decisions path."""

    def __init__(
        self,
        *,
        acquisition: AcquisitionEngine | None = None,
        extraction: ExtractionEngine | None = None,
        providers: Sequence[CandidateProvider] | None = None,
        profile_store: SiteProfileStore | None = None,
        learning_policy: LearningPolicy | None = None,
        learning_enabled: bool = True,
        strict_learning: bool = False,
        semantic_recovery_policy: SemanticRecoveryPolicy | None = None,
    ) -> None:
        if extraction is not None and providers is not None:
            raise ValueError("Pass either extraction or providers, not both")

        self.acquisition = acquisition or AcquisitionEngine()
        self.learning_enabled = learning_enabled
        self.strict_learning = strict_learning
        self.learning_policy = learning_policy or LearningPolicy()
        self.semantic_recovery_policy = (
            semantic_recovery_policy or SemanticRecoveryPolicy()
        )
        self.profile_store = (
            profile_store
            if profile_store is not None
            else (InMemorySiteProfileStore() if learning_enabled else None)
        )
        self.learner = (
            ConfirmationLearner(self.profile_store)
            if learning_enabled and self.profile_store is not None
            else None
        )

        if extraction is not None:
            self.extraction = extraction
        else:
            base_providers = (
                tuple(providers)
                if providers is not None
                else (AutoDiscoveryProvider(),)
            )
            if learning_enabled and self.profile_store is not None:
                effective_providers = tuple(
                    provider
                    if isinstance(provider, ProfileAwareProvider)
                    else ProfileAwareProvider(
                        provider,
                        self.profile_store,
                        policy=self.learning_policy,
                    )
                    for provider in base_providers
                )
            else:
                effective_providers = base_providers
            self.extraction = ExtractionEngine(effective_providers)

    def extract(
        self,
        request: AcquisitionRequest,
        fields: Sequence[FieldSpec],
    ) -> URLExtractionResult:
        acquired = self.acquisition.acquire(request)
        extracted = self.extraction.extract(acquired.asset, fields)
        primary = URLExtractionResult(acquisition=acquired, extraction=extracted)
        if not self.semantic_recovery_policy.should_retry(
            request,
            acquired,
            extracted,
        ):
            return primary
        return self._semantic_render_recovery(request, fields, primary)

    def _semantic_render_recovery(
        self,
        request: AcquisitionRequest,
        fields: Sequence[FieldSpec],
        primary: URLExtractionResult,
    ) -> URLExtractionResult:
        trigger_fields = primary.extraction.unresolved_required_fields
        recovery_request = replace(request, render_mode=RenderMode.ALWAYS)

        try:
            rendered_acquisition = self.acquisition.acquire(recovery_request)
            rendered_extraction = self.extraction.extract(
                rendered_acquisition.asset,
                fields,
            )
        except Exception as exc:
            renderer = getattr(self.acquisition, "renderer", None)
            method = getattr(renderer, "name", "browser")
            failed_attempt = AcquisitionAttempt(
                method=method,
                success=False,
                reason=f"semantic_required_recovery:{type(exc).__name__}",
                rendered=True,
            )
            acquisition = AcquisitionResult(
                asset=primary.acquisition.asset,
                attempts=primary.acquisition.attempts + (failed_attempt,),
                warnings=primary.acquisition.warnings
                + (
                    "semantic_render_recovery_triggered:"
                    + ",".join(trigger_fields),
                    f"semantic_render_recovery_failed:{type(exc).__name__}",
                ),
            )
            return URLExtractionResult(
                acquisition=acquisition,
                extraction=primary.extraction,
                learning_events=primary.learning_events,
                learning_warnings=primary.learning_warnings,
            )

        combined_attempts = (
            primary.acquisition.attempts + rendered_acquisition.attempts
        )
        combined_warnings = (
            primary.acquisition.warnings
            + rendered_acquisition.warnings
            + (
                "semantic_render_recovery_triggered:"
                + ",".join(trigger_fields),
            )
        )

        if self.semantic_recovery_policy.prefer_rendered(
            primary.extraction,
            rendered_extraction,
        ):
            acquisition = AcquisitionResult(
                asset=rendered_acquisition.asset,
                attempts=combined_attempts,
                warnings=combined_warnings
                + ("semantic_render_recovery_selected:rendered",),
            )
            return URLExtractionResult(
                acquisition=acquisition,
                extraction=rendered_extraction,
                learning_events=primary.learning_events,
                learning_warnings=primary.learning_warnings,
            )

        acquisition = AcquisitionResult(
            asset=primary.acquisition.asset,
            attempts=combined_attempts,
            warnings=combined_warnings
            + ("semantic_render_recovery_selected:static",),
        )
        return URLExtractionResult(
            acquisition=acquisition,
            extraction=primary.extraction,
            learning_events=primary.learning_events,
            learning_warnings=primary.learning_warnings,
        )

    def extract_url(
        self,
        url: str,
        fields: Sequence[FieldSpec],
        *,
        asset_id: str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float = 20.0,
        max_bytes: int = 5_000_000,
        render_mode: RenderMode = RenderMode.AUTO,
    ) -> URLExtractionResult:
        return self.extract(
            AcquisitionRequest(
                url=url,
                asset_id=asset_id,
                headers={} if headers is None else dict(headers),
                timeout_s=timeout_s,
                max_bytes=max_bytes,
                render_mode=render_mode,
            ),
            fields,
        )

    def confirm(
        self,
        result: URLExtractionResult,
        selections: Mapping[str, str | None],
    ) -> URLExtractionResult:
        confirmed = self.extraction.confirm(result.extraction, selections)

        new_events: tuple[LearningEvent, ...] = ()
        new_warnings: tuple[str, ...] = ()
        if self.learner is not None:
            try:
                new_events = self.learner.learn(result.extraction, selections)
            except Exception as exc:
                if self.strict_learning:
                    raise
                new_warnings = (
                    f"profile_learning_failed:{type(exc).__name__}:{exc}",
                )

        return URLExtractionResult(
            acquisition=result.acquisition,
            extraction=confirmed,
            learning_events=result.learning_events + new_events,
            learning_warnings=result.learning_warnings + new_warnings,
        )

    def maintain_profiles(self) -> ProfilePruneReport | None:
        """Prune expired/near-zero learned signals from the configured profile backend."""

        if self.profile_store is None:
            return None
        prune = getattr(self.profile_store, "prune", None)
        if prune is None:
            return None
        return prune()
