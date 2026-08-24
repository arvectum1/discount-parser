from __future__ import annotations

from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from src.web import application


def test_sources_request_is_intercepted_before_legacy_router(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_friendly_registry(message: str | None = None, error: str | None = None):
        calls["message"] = message
        calls["error"] = error
        return HTMLResponse("middleware-safe-sources", status_code=200)

    monkeypatch.setattr(application, "friendly_registry_page", fake_friendly_registry)

    with TestClient(application.app) as client:
        response = client.get("/sources-registry?message=hello&error=problem")

    assert response.status_code == 200
    assert "middleware-safe-sources" in response.text
    assert calls == {"message": "hello", "error": "problem"}


def test_windows_installer_is_feedback_7_build() -> None:
    from pathlib import Path

    installer = (Path(__file__).resolve().parents[1] / "packaging" / "windows" / "installer.iss").read_text(encoding="utf-8")
    assert '#define MyAppVersion "0.1.12"' in installer
