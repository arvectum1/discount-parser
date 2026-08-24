from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from src.telegram.publisher import PublishResult
from src.web import customer_hotfixes


ROOT = Path(__file__).resolve().parents[1]


def test_manual_publish_route_is_replaced_with_network_routed_hotfix() -> None:
    app = FastAPI()

    @app.post('/publish/{offer_id}')
    def legacy_publish(offer_id: int):
        return PlainTextResponse(str(offer_id))

    customer_hotfixes.install_customer_hotfixes(app)

    matches = [
        route
        for route in app.router.routes
        if getattr(route, 'path', None) == '/publish/{offer_id}'
        and 'POST' in set(getattr(route, 'methods', set()) or set())
    ]
    assert len(matches) == 1
    assert matches[0].endpoint is customer_hotfixes.web_publish_hotfix


def test_manual_publish_uses_shared_bot_builder(monkeypatch) -> None:
    class Settings:
        telegram_bot_token = 'test-token'
        telegram_channel_id = '-1001234567890'

    class Session:
        closed = False

        async def close(self) -> None:
            self.closed = True

    class Bot:
        def __init__(self) -> None:
            self.session = Session()

    bot = Bot()
    calls: dict[str, object] = {}

    def fake_build_bot(token: str):
        calls['token'] = token
        return bot

    async def fake_publish_offer(actual_bot, *, offer_id: int, channel_id: str):
        calls['bot'] = actual_bot
        calls['offer_id'] = offer_id
        calls['channel_id'] = channel_id
        return PublishResult(
            offer_id=offer_id,
            channel_id=channel_id,
            status='published',
            publication_id=7,
            telegram_message_id='99',
        )

    monkeypatch.setattr(customer_hotfixes, 'get_settings', lambda: Settings())
    monkeypatch.setattr(customer_hotfixes, 'build_bot', fake_build_bot)
    monkeypatch.setattr(customer_hotfixes, 'publish_offer', fake_publish_offer)

    response = customer_hotfixes.web_publish_hotfix(121)

    assert response.status_code == 303
    assert calls == {
        'token': 'test-token',
        'bot': bot,
        'offer_id': 121,
        'channel_id': '-1001234567890',
    }
    assert bot.session.closed is True
    assert 'Публикация выполнена' in unquote(response.headers['location'])


def test_failed_manual_publish_keeps_retry_message() -> None:
    result = PublishResult(
        offer_id=121,
        channel_id='-1001234567890',
        status='failed',
        publication_id=8,
        error='TelegramNetworkError: timeout',
    )
    message = customer_hotfixes._result_message(result)
    assert 'осталось в очереди' in message
    assert 'повторить' in message
    assert 'timeout' in message


def test_legacy_registry_repair_migration_is_present() -> None:
    migration = (ROOT / 'migrations' / 'versions' / '0007_legacy_registry_null_repair.py').read_text(encoding='utf-8')
    assert 'down_revision = "0006"' in migration
    assert 'UPDATE registered_sources' in migration
    assert "COALESCE(NULLIF(name, ''), NULLIF(key, ''), 'Источник #' || id)" in migration
    assert "ELSE 'unknown'" in migration
    assert 'UPDATE source_candidates' in migration
    assert 'UPDATE source_keywords' in migration


def test_windows_installer_preflights_owned_process_locks() -> None:
    installer = (ROOT / 'packaging' / 'windows' / 'installer.iss').read_text(encoding='utf-8')
    assert '#define MyAppVersion "0.1.13"' in installer
    assert 'function PrepareToInstall(var NeedsRestart: Boolean): String;' in installer
    assert "StopProductProcess('{#MyWorkerExeName}')" in installer
    assert "StopProductProcess('{#MyAppExeName}')" in installer
    assert 'ProbeUnlockedFile(AppExe)' in installer
    assert 'ProbeUnlockedFile(WorkerExe)' in installer
    assert 'taskkill.exe' in installer
