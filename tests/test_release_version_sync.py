from __future__ import annotations

import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_package_and_installer_versions_match() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_version = pyproject["project"]["version"]

    installer = (ROOT / "packaging" / "windows" / "installer.iss").read_text(encoding="utf-8")
    match = re.search(r'^#define MyAppVersion "([^"]+)"$', installer, flags=re.MULTILINE)
    assert match is not None
    installer_version = match.group(1)

    assert package_version == installer_version
    assert package_version == "0.1.13"
