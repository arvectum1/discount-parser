from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
NETWORK_POLICIES = {"auto", "direct", "proxy", "system"}
RUNTIME_MODES = {"legacy", "hybrid"}


@dataclass(frozen=True, slots=True)
class SourceConfig:
    key: str
    name: str
    adapter: str
    base_url: str
    enabled: bool = True
    network_policy: str = "auto"
    runtime_mode: str = "legacy"


def _resolve_config_path(path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    cwd_candidate = Path.cwd() / candidate
    if cwd_candidate.exists():
        return cwd_candidate
    return _PROJECT_ROOT / candidate


def _network_policy(value: object) -> str:
    policy = str(value or "auto").strip().lower()
    return policy if policy in NETWORK_POLICIES else "auto"


def _runtime_mode(value: object) -> str:
    mode = str(value or "legacy").strip().lower()
    return mode if mode in RUNTIME_MODES else "legacy"


def load_source_configs(path: str | Path = "config/sources.yaml") -> list[SourceConfig]:
    config_path = _resolve_config_path(path)
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    result: list[SourceConfig] = []
    for item in data.get("sources", []):
        result.append(
            SourceConfig(
                key=str(item["key"]),
                name=str(item.get("name") or item["key"]),
                adapter=str(item["adapter"]),
                base_url=str(item["base_url"]),
                enabled=bool(item.get("enabled", True)),
                network_policy=_network_policy(item.get("network_policy")),
                runtime_mode=_runtime_mode(item.get("runtime_mode")),
            )
        )
    return result


def set_source_enabled(key: str, enabled: bool, path: str | Path = "config/sources.yaml") -> SourceConfig:
    config_path = _resolve_config_path(path)
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    for item in data.get("sources", []):
        if str(item.get("key")) == key:
            item["enabled"] = bool(enabled)
            config_path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            return SourceConfig(
                key=str(item["key"]),
                name=str(item.get("name") or item["key"]),
                adapter=str(item["adapter"]),
                base_url=str(item["base_url"]),
                enabled=bool(item["enabled"]),
                network_policy=_network_policy(item.get("network_policy")),
                runtime_mode=_runtime_mode(item.get("runtime_mode")),
            )
    raise KeyError(f"Unknown source: {key}")
