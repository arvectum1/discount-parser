from __future__ import annotations

import json
from pathlib import Path

import pytest

from arvectum_data import (
    Candidate,
    Evidence,
    FieldSpec,
    InMemorySiteProfileStore,
    JsonSiteProfileStore,
    LearningPolicy,
    ProfileAwareProvider,
    ProfileLifecyclePolicy,
    RawAsset,
    SQLiteSiteProfileStore,
    URLExtractionPipeline,
    candidate_fingerprints,
)


DAY = 86_400.0


def make_candidate(value="199", confidence=0.90):
    return Candidate(
        field_key="price",
        value=value,
        confidence=confidence,
        provider="test",
        evidence=(Evidence(kind="html_meta", source_ref="price"),),
        metadata={"matched_terms": ("price",)},
    )


class StaticProvider:
    name = "static"

    def __init__(self, candidate):
        self.candidate = candidate

    def candidates(self, asset, fields):
        return (self.candidate,)


def test_lifecycle_half_life_decays_effective_weight():
    now = [1_000_000.0]
    fingerprint = candidate_fingerprints(make_candidate())[0]
    store = InMemorySiteProfileStore(
        clock=lambda: now[0],
        lifecycle=ProfileLifecyclePolicy(decay_half_life_days=30),
    )
    store.record("shop.example.com", "price", positive=(fingerprint,))

    now[0] += 30 * DAY
    stats = store.get_stats("shop.example.com", "price", fingerprint)

    assert stats.confirmations == pytest.approx(0.5)
    assert LearningPolicy().adjustment(stats) == pytest.approx(0.03)


def test_new_feedback_adds_to_decayed_history_instead_of_reviving_it():
    now = [1_000_000.0]
    fingerprint = candidate_fingerprints(make_candidate())[0]
    store = InMemorySiteProfileStore(
        clock=lambda: now[0],
        lifecycle=ProfileLifecyclePolicy(decay_half_life_days=30),
    )
    store.record("shop.example.com", "price", positive=(fingerprint,))
    now[0] += 30 * DAY

    store.record("shop.example.com", "price", positive=(fingerprint,))

    assert store.get_stats(
        "shop.example.com", "price", fingerprint
    ).confirmations == pytest.approx(1.5)


def test_hard_ttl_is_enforced_lazily_before_prune():
    now = [1_000_000.0]
    fingerprint = candidate_fingerprints(make_candidate())[0]
    store = InMemorySiteProfileStore(
        clock=lambda: now[0],
        lifecycle=ProfileLifecyclePolicy(max_signal_age_days=10),
    )
    store.record("shop.example.com", "price", positive=(fingerprint,))
    now[0] += 11 * DAY

    assert (
        store.get_stats("shop.example.com", "price", fingerprint).confirmations
        == 0.0
    )
    assert store.snapshot()["sites"]["shop.example.com"]["price"]


def test_prune_removes_expired_pattern_field_and_site_and_bumps_revision():
    now = [1_000_000.0]
    fingerprint = candidate_fingerprints(make_candidate())[0]
    store = InMemorySiteProfileStore(
        clock=lambda: now[0],
        lifecycle=ProfileLifecyclePolicy(max_signal_age_days=1),
    )
    store.record("shop.example.com", "price", positive=(fingerprint,))
    assert store.revision == 1
    now[0] += 2 * DAY

    report = store.prune()

    assert report.removed_patterns == 1
    assert report.removed_fields == 1
    assert report.removed_sites == 1
    assert report.revision == 2
    assert store.snapshot()["sites"] == {}


def test_noop_prune_does_not_bump_revision():
    now = [1_000_000.0]
    fingerprint = candidate_fingerprints(make_candidate())[0]
    store = InMemorySiteProfileStore(clock=lambda: now[0])
    store.record("shop.example.com", "price", positive=(fingerprint,))
    revision = store.revision

    report = store.prune()

    assert not report.changed
    assert store.revision == revision


def test_v1_json_is_migrated_to_v2_with_timestamp(tmp_path: Path):
    now = [1234.0]
    fingerprint = candidate_fingerprints(make_candidate())[0]
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "sites": {
                    "shop.example.com": {
                        "price": {
                            fingerprint.key: {
                                "confirmations": 2,
                                "rejections": 1,
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    store = JsonSiteProfileStore(path, clock=lambda: now[0])
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["version"] == 2
    assert payload["revision"] == 0
    raw = payload["sites"]["shop.example.com"]["price"][fingerprint.key]
    assert raw["updated_at"] == 1234.0
    assert store.get_stats(
        "shop.example.com", "price", fingerprint
    ).confirmations == 2


def test_json_revision_survives_reload(tmp_path: Path):
    fingerprint = candidate_fingerprints(make_candidate())[0]
    path = tmp_path / "profiles.json"
    store = JsonSiteProfileStore(path, clock=lambda: 100.0)
    store.record("shop.example.com", "price", positive=(fingerprint,))
    store.record("shop.example.com", "price", negative=(fingerprint,))

    reloaded = JsonSiteProfileStore(path, clock=lambda: 100.0)

    assert reloaded.revision == 2
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 2


def test_unknown_json_schema_is_rejected(tmp_path: Path):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({"version": 99, "sites": {}}), encoding="utf-8")

    with pytest.raises(ValueError, match="Unsupported site profile store version"):
        JsonSiteProfileStore(path)


def test_sqlite_two_instances_share_transactional_state_and_revision(tmp_path: Path):
    fingerprint = candidate_fingerprints(make_candidate())[0]
    path = tmp_path / "profiles.db"
    first = SQLiteSiteProfileStore(path, clock=lambda: 100.0)
    second = SQLiteSiteProfileStore(path, clock=lambda: 100.0)
    try:
        first.record("shop.example.com", "price", positive=(fingerprint,))
        second.record("shop.example.com", "price", positive=(fingerprint,))

        assert first.get_stats(
            "shop.example.com", "price", fingerprint
        ).confirmations == pytest.approx(2.0)
        assert first.revision == 2
        assert second.revision == 2
    finally:
        first.close()
        second.close()


def test_sqlite_persistence_survives_reopen(tmp_path: Path):
    fingerprint = candidate_fingerprints(make_candidate())[0]
    path = tmp_path / "profiles.db"
    with SQLiteSiteProfileStore(path, clock=lambda: 100.0) as store:
        store.record("shop.example.com", "price", positive=(fingerprint,))

    with SQLiteSiteProfileStore(path, clock=lambda: 100.0) as reopened:
        assert reopened.get_stats(
            "shop.example.com", "price", fingerprint
        ).confirmations == 1
        assert reopened.snapshot()["version"] == 2


def test_sqlite_uses_same_ttl_and_prune_semantics(tmp_path: Path):
    now = [100.0]
    fingerprint = candidate_fingerprints(make_candidate())[0]
    store = SQLiteSiteProfileStore(
        tmp_path / "profiles.db",
        clock=lambda: now[0],
        lifecycle=ProfileLifecyclePolicy(max_signal_age_days=1),
    )
    try:
        store.record("shop.example.com", "price", positive=(fingerprint,))
        now[0] += 2 * DAY

        assert store.get_stats(
            "shop.example.com", "price", fingerprint
        ).confirmations == 0
        report = store.prune()
        assert report.removed_patterns == 1
        assert report.revision == 2
    finally:
        store.close()


def test_profile_aware_provider_consumes_decayed_stats():
    now = [100.0]
    candidate = make_candidate(confidence=0.70)
    fingerprint = candidate_fingerprints(candidate)[0]
    store = InMemorySiteProfileStore(
        clock=lambda: now[0],
        lifecycle=ProfileLifecyclePolicy(decay_half_life_days=30),
    )
    store.record("shop.example.com", "price", positive=(fingerprint,))
    now[0] += 30 * DAY
    provider = ProfileAwareProvider(StaticProvider(candidate), store)

    adjusted = provider.candidates(
        RawAsset("a1", source_url="https://shop.example.com/item"),
        [FieldSpec("price")],
    )[0]

    assert adjusted.confidence == pytest.approx(0.73)


def test_lifecycle_policy_validation():
    with pytest.raises(ValueError, match="half_life"):
        ProfileLifecyclePolicy(decay_half_life_days=0)
    with pytest.raises(ValueError, match="max_signal_age_days"):
        ProfileLifecyclePolicy(max_signal_age_days=0)
    with pytest.raises(ValueError, match="prune_effective_weight_below"):
        ProfileLifecyclePolicy(prune_effective_weight_below=-1)


def test_pipeline_maintenance_delegates_to_profile_store():
    class Store(InMemorySiteProfileStore):
        def __init__(self):
            super().__init__()
            self.called = 0

        def prune(self):
            self.called += 1
            return super().prune()

    store = Store()
    pipeline = URLExtractionPipeline(profile_store=store)

    report = pipeline.maintain_profiles()

    assert store.called == 1
    assert report.removed_patterns == 0
