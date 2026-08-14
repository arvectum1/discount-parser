from __future__ import annotations
import logging
import pytest
from pathlib import Path
from starlette.testclient import TestClient
from src.shared.logging import app_log_path, configure_logging, redact_secrets
from src.web.application import app
from src.sources.adapters.berikod import BerikodAdapter
from src.sources.http import HttpClient
from src.shared.db import reset_db_runtime

def test_secrets_redaction() -> None:
    text = "bot_token: 12345678:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    redacted = redact_secrets(text)
    assert "REDACTED" in redacted
    assert "12345678" not in redacted

def test_web_500_traceback_logging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Setup log dir in tmp_path
    monkeypatch.setenv("DP_RUNTIME_ROOT", str(tmp_path))
    from src.web import application, system_routes
    monkeypatch.setattr(application, "is_setup_complete", lambda: True)
    monkeypatch.setattr(system_routes, "is_setup_complete", lambda: True)
    configure_logging(level="INFO", enable_file=True)
    
    client = TestClient(app)
    
    # Force an error by monkeypatching a function called inside the route
    from src.web.processes import process_manager
    def crash_states(*args, **kwargs):
        raise ValueError("Simulated process crash")
    monkeypatch.setattr(process_manager, "states", crash_states)
    
    response = client.get("/system", follow_redirects=False)
    assert response.status_code == 500
    assert "app.log" in response.text
    
    log_file = app_log_path()
    assert log_file.exists()
    log_content = log_file.read_text(encoding="utf-8")
    assert "Simulated process crash" in log_content

def test_berikod_pagination_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Prepare HTML fixtures with Russian keywords to match _TITLE_RE
    page1 = '<html><body><article><h2>Скидка 10%</h2><a href="/1">Link 1</a></article></body></html>'
    page2 = '<html><body><article><h2>Промокод 20%</h2><a href="/2">Link 2</a></article></body></html>'
    
    class MockClient:
        def get_text(self, url):
            if "page=2" in url:
                return page2
            return page1
    
    adapter = BerikodAdapter(base_url="https://berikod.test/", client=MockClient(), max_pages=2)
    offers = adapter.collect()
    
    assert len(offers) == 2
    assert "10%" in offers[0].title
    assert "20%" in offers[1].title

def test_source_metrics_in_run_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.sources.runner import RunResult
    res = RunResult(source_key="test", fetched=10, created=5, updated=3, duplicates=2, duration_seconds=1.5)
    assert res.duplicates == 2
    assert res.duration_seconds == 1.5
