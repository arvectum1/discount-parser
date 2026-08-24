from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_desktop_shortcut_is_not_deleted_when_task_is_unselected() -> None:
    installer = (ROOT / "packaging" / "windows" / "installer.iss").read_text(encoding="utf-8")
    procedure = installer.split("procedure CreateDesktopShortcutBestEffort();", 1)[1].split("procedure CurStepChanged", 1)[0]

    task_check = procedure.index("if not WizardIsTaskSelected('desktopicon') then")
    remove_call = procedure.index("RemoveDesktopShortcutBestEffort();")

    assert task_check < remove_call
    assert "preserving existing shortcut" in procedure


def test_feedback_9_windows_installer_version() -> None:
    installer = (ROOT / "packaging" / "windows" / "installer.iss").read_text(encoding="utf-8")
    assert '#define MyAppVersion "0.1.12"' in installer


def test_source_configuration_guide_explains_selector_vs_attribute() -> None:
    guide = (ROOT / "docs" / "SOURCE_CONFIGURATION_RU.md").read_text(encoding="utf-8")
    assert "не нужно вставлять целую строку HTML" in guide
    assert "Атрибут промокода: `data-code`" in guide
    assert "Атрибут промокода: оставить пустым" in guide
