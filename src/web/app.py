from __future__ import annotations

import asyncio
import html
import tempfile
import threading
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from aiogram import Bot
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import func, select

from src.jobs.status import get_source_run_statuses
from src.modules.offers.models import Offer
from src.modules.publishing.filters import get_or_create_default_filter, update_default_filter
from src.modules.publishing.service import PublishCriteria, list_publish_candidates
from src.modules.xlsx.service import export_offers_xlsx, import_offer_corrections
from src.shared.config import get_settings
from src.shared.db import create_session
from src.sources.runner import run_all
from src.sources.state import list_source_states, set_persisted_source_enabled
from src.telegram.publisher import publish_offer
from src.web.processes import process_manager
from src.web.setup import is_setup_complete, save_operational_settings, save_telegram_setup

app = FastAPI(title='Discount Parser Control Panel', docs_url=None, redoc_url=None)
_parse_lock = threading.Lock()
_parse_state = {'running': False, 'last_error': None, 'last_finished': None, 'city': None, 'region': None}

STYLE = '''
<style>
:root{font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#18212f;background:#f5f7fb}
*{box-sizing:border-box}body{margin:0}.wrap{max-width:1240px;margin:auto;padding:32px}.top{display:flex;justify-content:space-between;align-items:center;gap:16px}.brand h1{margin:0;font-size:28px}.muted{color:#64748b}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin:24px 0}.card{background:white;border:1px solid #e5e7eb;border-radius:16px;padding:18px;box-shadow:0 6px 20px rgba(15,23,42,.05)}.metric{font-size:28px;font-weight:700;margin-top:5px}.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}.btn{display:inline-block;border:0;border-radius:10px;padding:10px 14px;font-weight:600;cursor:pointer;text-decoration:none;background:#111827;color:white}.btn.secondary{background:#e5e7eb;color:#111827}.btn.good{background:#0f766e}.btn.bad{background:#b91c1c}.btn.warn{background:#b45309}.btn:disabled{opacity:.45;cursor:not-allowed}.pill{display:inline-block;padding:5px 9px;border-radius:999px;font-size:12px;font-weight:700}.on{background:#dcfce7;color:#166534}.off{background:#fee2e2;color:#991b1b}.section{margin-top:20px}.source{display:grid;grid-template-columns:1.4fr .65fr .65fr 1fr auto;gap:10px;padding:10px 0;border-bottom:1px solid #eef2f7;align-items:center}.setup{max-width:680px;margin:50px auto;background:white;padding:30px;border-radius:18px;border:1px solid #e5e7eb}.field{margin:16px 0}.field label{display:block;font-weight:650;margin-bottom:6px}.field input,.field select{width:100%;padding:12px;border:1px solid #cbd5e1;border-radius:9px;font-size:15px;background:white}.error{background:#fee2e2;color:#991b1b;padding:12px;border-radius:10px}.ok{background:#dcfce7;color:#166534;padding:12px;border-radius:10px}.queue{display:grid;gap:12px}.offer{border:1px solid #e5e7eb;border-radius:12px;padding:14px}.offer h4{margin:0 0 8px}.offer-meta{display:flex;gap:10px;flex-wrap:wrap;font-size:13px;color:#64748b}.filter-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.filter-grid .field{margin:0}.small{font-size:13px}.flash{margin:18px 0}@media(max-width:800px){.source{grid-template-columns:1fr 1fr}.wrap{padding:18px}.top{align-items:flex-start;flex-direction:column}}
</style>
'''


def _layout(title: str, body: str) -> str:
    return f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>{STYLE}</head><body>{body}</body></html>'


def _metrics() -> dict[str, int]:
    with create_session() as session:
        total = int(session.scalar(select(func.count()).select_from(Offer)) or 0)
        result = {'total': total}
        for status in ('ready', 'needs_review', 'published', 'expired'):
            result[status] = int(session.scalar(select(func.count()).select_from(Offer).where(Offer.status == status)) or 0)
        return result


def _distinct_offer_values(field) -> list[str]:
    with create_session() as session:
        values = session.scalars(select(field).where(field.is_not(None), field != '').distinct().order_by(field).limit(200)).all()
    return [str(value) for value in values if value]


def _run_parse_thread(*, city: str | None = None, region: str | None = None) -> None:
    if not _parse_lock.acquire(blocking=False):
        return
    _parse_state['running'] = True
    _parse_state['last_error'] = None
    _parse_state['city'] = city
    _parse_state['region'] = region
    try:
        from src.modules.source_registry.runner import collect_registered_sources
        run_all(path=get_settings().sources_config_path, city=city, region=region)
        collect_registered_sources()
    except Exception as exc:
        _parse_state['last_error'] = f'{type(exc).__name__}: {exc}'
    finally:
        _parse_state['running'] = False
        _parse_state['last_finished'] = datetime.now().isoformat(timespec='seconds')
        _parse_lock.release()


def _filter_form() -> str:
    settings = get_settings()
    row = get_or_create_default_filter(min_discount_percent=settings.telegram_default_min_discount)
    categories = _distinct_offer_values(Offer.category)
    subcategories = _distinct_offer_values(Offer.subcategory)
    merchants = _distinct_offer_values(Offer.merchant)
    cities = _distinct_offer_values(Offer.city)
    regions = _distinct_offer_values(Offer.region)

    def options(values: list[str], current: str | None, all_label: str) -> str:
        result = [f'<option value="">{html.escape(all_label)}</option>']
        for value in values:
            selected = ' selected' if value == current else ''
            result.append(f'<option value="{html.escape(value)}"{selected}>{html.escape(value)}</option>')
        return ''.join(result)

    enabled = ' checked' if row.enabled else ''
    return f'''<form method="post" action="/filter">
      <div class="filter-grid">
        <div class="field"><label>Минимальная скидка, %</label><input name="min_discount_percent" value="{row.min_discount_percent or 0}" inputmode="decimal"></div>
        <div class="field"><label>Регион</label><select name="region">{options(regions,row.region,'Все регионы')}</select></div>
        <div class="field"><label>Город</label><select name="city">{options(cities,row.city,'Все города')}</select></div>
        <div class="field"><label>Категория</label><select name="category">{options(categories,row.category,'Все категории')}</select></div>
        <div class="field"><label>Подкатегория</label><select name="subcategory">{options(subcategories,row.subcategory,'Все подкатегории')}</select></div>
        <div class="field"><label>Тип</label><select name="offer_type"><option value="">Все типы</option>{''.join(f'<option value="{kind}"{" selected" if row.offer_type == kind else ""}>{kind}</option>' for kind in ('discount','promo','cashback','delivery','other'))}</select></div>
        <div class="field"><label>Магазин</label><select name="merchant">{options(merchants,row.merchant,'Все магазины')}</select></div>
        <div class="field"><label>Постов за цикл</label><input type="number" min="1" max="100" name="max_posts_per_cycle" value="{row.max_posts_per_cycle or 10}"></div>
      </div>
      <div class="row" style="margin-top:14px"><label><input type="checkbox" name="enabled" value="1"{enabled}> Автопостинг включён</label><button class="btn good" type="submit">Сохранить фильтр</button></div>
    </form>'''


def _schedule_form() -> str:
    settings = get_settings()
    return f'''<form method="post" action="/schedule">
      <div class="filter-grid">
        <div class="field"><label>Сбор каждые, минут</label><input type="number" min="1" max="10080" name="collect_interval_minutes" value="{settings.collect_interval_minutes}" required></div>
        <div class="field"><label>Автопост каждые, минут</label><input type="number" min="1" max="10080" name="autopost_interval_minutes" value="{settings.autopost_interval_minutes}" required></div>
        <div class="field"><label>Maintenance, час</label><input type="number" min="0" max="23" name="maintenance_hour" value="{settings.maintenance_hour}" required></div>
        <div class="field"><label>Maintenance, минута</label><input type="number" min="0" max="59" name="maintenance_minute" value="{settings.maintenance_minute}" required></div>
        <div class="field"><label>Без обновления → review, дней</label><input type="number" min="1" max="365" name="stale_after_days" value="{settings.stale_after_days}" required></div>
      </div>
      <div class="row" style="margin-top:14px"><button class="btn good" type="submit">Сохранить расписание</button><span class="muted small">Если scheduler работает, он перезапустится автоматически.</span></div>
    </form>'''


def _queue_html() -> str:
    settings = get_settings()
    if not settings.telegram_channel_id:
        return '<p class="muted">Канал Telegram пока не настроен.</p>'
    row = get_or_create_default_filter(min_discount_percent=settings.telegram_default_min_discount)
    criteria = PublishCriteria.from_filter(row)
    criteria = PublishCriteria(
        min_discount_percent=criteria.min_discount_percent,
        category=criteria.category,
        subcategory=criteria.subcategory,
        offer_type=criteria.offer_type,
        merchant=criteria.merchant,
        source_key=criteria.source_key,
        city=criteria.city,
        region=criteria.region,
        limit=min(max(criteria.limit, 1), 20),
    )
    with create_session() as session:
        offers = list_publish_candidates(session, channel_id=settings.telegram_channel_id, criteria=criteria)
    if not offers:
        return '<p class="muted">По текущему фильтру очередь пуста.</p>'
    chunks: list[str] = []
    for offer in offers:
        benefit = []
        if offer.discount_percent is not None:
            benefit.append(f'{offer.discount_percent:g}%')
        if offer.discount_amount is not None:
            benefit.append(f'−{offer.discount_amount:g} {html.escape(offer.currency or "₽")}')
        if offer.promo_code:
            benefit.append(f'код {html.escape(offer.promo_code)}')
        geo = ', '.join(value for value in (offer.city, offer.region) if value) or 'ГЕО не указано'
        link = f'<a class="btn secondary" target="_blank" rel="noopener" href="{html.escape(offer.canonical_url)}">Открыть</a>' if offer.canonical_url and offer.canonical_url.startswith(('http://','https://')) else ''
        chunks.append(f'''<div class="offer"><h4>{html.escape(offer.display_title or offer.title)}</h4><div class="offer-meta"><span>{html.escape(offer.merchant or '—')}</span><span>📍 {html.escape(geo)}</span><span>{html.escape(offer.category or 'Без категории')}</span><span>{html.escape(offer.offer_type or 'other')}</span><span>{' · '.join(benefit) or 'выгода не указана'}</span></div><div class="row" style="margin-top:12px"><form method="post" action="/publish/{offer.id}"><button class="btn good">Опубликовать</button></form><form method="post" action="/reject/{offer.id}"><button class="btn bad">Отклонить</button></form>{link}</div></div>''')
    return '<div class="queue">' + ''.join(chunks) + '</div>'


def _source_rows() -> str:
    settings = get_settings()
    states = {item.key: item for item in list_source_states(settings.sources_config_path)}
    statuses = {item.source_key: item for item in get_source_run_statuses()}
    rows: list[str] = []
    for state in states.values():
        status = statuses.get(state.key)
        enabled_badge = '<span class="pill on">ВКЛЮЧЁН</span>' if state.enabled else '<span class="pill off">ВЫКЛЮЧЕН</span>'
        action = 'disable' if state.enabled else 'enable'
        action_label = 'Выключить' if state.enabled else 'Включить'
        action_class = 'bad' if state.enabled else 'good'
        rows.append(
            f'<div class="source"><div><b>{html.escape(state.name)}</b><div class="muted">{html.escape(state.key)}</div></div>'
            f'<div>{enabled_badge}<div class="muted small" style="margin-top:5px">{html.escape(status.last_status if status and status.last_status else "never")}</div></div>'
            f'<div>{status.fetched_count if status else 0}</div>'
            f'<div class="muted">{html.escape(str(status.last_finished_at if status and status.last_finished_at else "—"))}</div>'
            f'<div><form method="post" action="/source/{html.escape(state.key)}/{action}"><button class="btn {action_class}">{action_label}</button></form></div></div>'
        )
    return ''.join(rows) or '<p class="muted">Источники не настроены.</p>'


@app.get('/', response_class=HTMLResponse)
def dashboard(message: str | None = None):
    if not is_setup_complete():
        return RedirectResponse('/setup', status_code=303)

    settings = get_settings()
    metrics = _metrics()
    states = process_manager.states()

    def proc_card(name: str, label: str) -> str:
        state = states[name]
        status = '<span class="pill on">РАБОТАЕТ</span>' if state.running else '<span class="pill off">ОСТАНОВЛЕН</span>'
        action = 'stop' if state.running else 'start'
        text = 'Остановить' if state.running else 'Запустить'
        cls = 'bad' if state.running else 'good'
        return f'<div class="card"><b>{label}</b><div style="margin:12px 0">{status}</div><form method="post" action="/process/{name}/{action}"><button class="btn {cls}" type="submit">{text}</button></form></div>'

    parse_status = '<span class="pill on">ИДЁТ СБОР</span>' if _parse_state['running'] else '<span class="pill off">НЕ ЗАПУЩЕН</span>'
    parse_error = f'<div class="error" style="margin-top:10px">{html.escape(str(_parse_state["last_error"]))}</div>' if _parse_state['last_error'] else ''
    last_geo = ', '.join(value for value in (_parse_state.get('city'), _parse_state.get('region')) if value) or 'все регионы'
    flash = f'<div class="ok flash">{html.escape(message)}</div>' if message else ''

    body = f'''<div class="wrap">
    <div class="top"><div class="brand"><h1>Discount Parser</h1><div class="muted">Панель управления парсером и Telegram-ботом</div></div><div class="row"><a class="btn secondary" href="/export">Скачать XLSX</a><a class="btn secondary" href="/setup">Telegram</a></div></div>
    {flash}
    <div class="grid">
      <div class="card"><div class="muted">Всего предложений</div><div class="metric">{metrics['total']}</div></div>
      <div class="card"><div class="muted">Готово</div><div class="metric">{metrics['ready']}</div></div>
      <div class="card"><div class="muted">На проверке</div><div class="metric">{metrics['needs_review']}</div></div>
      <div class="card"><div class="muted">Опубликовано</div><div class="metric">{metrics['published']}</div></div>
      <div class="card"><div class="muted">Истекло</div><div class="metric">{metrics['expired']}</div></div>
    </div>
    <div class="grid">{proc_card('bot','Telegram-бот')}{proc_card('scheduler','Автоматическое расписание')}
      <div class="card"><b>Парсер</b><div style="margin:12px 0">{parse_status}</div><form method="post" action="/parse"><div class="filter-grid"><div class="field"><label>Регион для этого запуска</label><input name="region" placeholder="например, Московская область"></div><div class="field"><label>Город для этого запуска</label><input name="city" placeholder="например, Москва"></div></div><button class="btn good" style="margin-top:12px" {'disabled' if _parse_state['running'] else ''}>Запустить сбор сейчас</button></form><div class="muted small" style="margin-top:8px">Оба поля необязательны. Если ГЕО задано, сохраняются только совпавшие предложения; неизвестное ГЕО не угадывается. В самих Offer город/регион определяются и сохраняются при парсинге.</div><div class="muted" style="margin-top:8px">Последний запуск: {html.escape(str(_parse_state['last_finished'] or '—'))} · ГЕО: {html.escape(last_geo)}</div>{parse_error}</div>
    </div>
    <div class="card section"><div class="row" style="justify-content:space-between"><div><b>Telegram</b><div class="muted">{html.escape(settings.telegram_bot_name or 'Бот')} → {html.escape(settings.telegram_channel_id or '')}</div></div><a class="btn secondary" href="/setup">Изменить</a></div></div>
    <div class="card section"><h3 style="margin-top:0">Расписание</h3><div class="muted small" style="margin-bottom:16px">Все интервалы меняются здесь, без редактирования .env.</div>{_schedule_form()}</div>
    <div class="card section"><div class="row" style="justify-content:space-between"><div><h3 style="margin:0">Источники</h3><div class="muted small">Включайте и выключайте источники; выбор хранится в базе и сохраняется при обновлениях.</div></div></div><div class="source" style="margin-top:14px"><b>Источник</b><b>Состояние</b><b>Получено</b><b>Последний запуск</b><b>Действие</b></div>{_source_rows()}</div>
    <div class="card section"><div class="row" style="justify-content:space-between"><div><h3 style="margin:0">Фильтр публикации</h3><div class="muted small">Одинаковый для веб-панели, Telegram `/queue` и автопостинга. Можно ограничить публикации конкретным регионом и/или городом.</div></div></div><div style="margin-top:16px">{_filter_form()}</div></div>
    <div class="card section"><div class="row" style="justify-content:space-between"><div><h3 style="margin:0">Очередь публикации</h3><div class="muted small">До 20 следующих предложений по текущему фильтру.</div></div></div><div style="margin-top:16px">{_queue_html()}</div></div>
    <div class="card section"><div class="row" style="justify-content:space-between"><div><h3 style="margin:0">XLSX-коррекция</h3><div class="muted small">Скачайте файл, меняйте только category/subcategory и загрузите обратно.</div></div><a class="btn secondary" href="/export">Экспорт</a></div><form method="post" action="/import" enctype="multipart/form-data" class="row" style="margin-top:14px"><input type="file" name="file" accept=".xlsx" required><button class="btn good">Импортировать XLSX</button></form></div>
    </div>'''
    return HTMLResponse(_layout('Discount Parser', body))


@app.get('/setup', response_class=HTMLResponse)
def setup_page(error: str | None = None):
    settings = get_settings()
    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ''
    body = f'''<div class="setup"><h1>Первичная настройка</h1><p class="muted">Эти данные нужны для управления Telegram-ботом и публикации в канал. Их можно изменить позже.</p>{error_html}
    <form method="post" action="/setup">
      <div class="field"><label>Токен Telegram-бота *</label><input type="password" name="bot_token" value="" placeholder="123456789:AA..." required><div class="muted">Получается у @BotFather.</div></div>
      <div class="field"><label>Имя бота</label><input name="bot_name" value="{html.escape(settings.telegram_bot_name or '')}" placeholder="Мой бот скидок"></div>
      <div class="field"><label>Telegram-канал *</label><input name="channel_id" value="{html.escape(settings.telegram_channel_id or '')}" placeholder="@my_channel или -100..." required><div class="muted">Бот должен быть администратором канала с правом публикации.</div></div>
      <div class="field"><label>Ваш Telegram user ID *</label><input name="admin_ids" value="{html.escape(settings.telegram_admin_ids or '')}" placeholder="123456789" required><div class="muted">Этот пользователь сможет управлять ботом. Несколько ID — через запятую.</div></div>
      <button class="btn good" type="submit">Сохранить и открыть панель</button>
      {'<a class="btn secondary" href="/" style="margin-left:8px">Назад</a>' if is_setup_complete() else ''}
    </form></div>'''
    return HTMLResponse(_layout('Настройка Discount Parser', body))


@app.post('/setup')
def setup_save(
    bot_token: str = Form(...),
    bot_name: str = Form(''),
    channel_id: str = Form(...),
    admin_ids: str = Form(...),
):
    try:
        save_telegram_setup(bot_token=bot_token, bot_name=bot_name, channel_id=channel_id, admin_ids=admin_ids)
    except ValueError as exc:
        return setup_page(error=str(exc))
    return RedirectResponse('/?message=Настройки+сохранены', status_code=303)


@app.post('/schedule')
def save_schedule(
    collect_interval_minutes: int = Form(...),
    autopost_interval_minutes: int = Form(...),
    maintenance_hour: int = Form(...),
    maintenance_minute: int = Form(...),
    stale_after_days: int = Form(...),
):
    try:
        save_operational_settings(
            collect_interval_minutes=collect_interval_minutes,
            autopost_interval_minutes=autopost_interval_minutes,
            maintenance_hour=maintenance_hour,
            maintenance_minute=maintenance_minute,
            stale_after_days=stale_after_days,
        )
    except ValueError as exc:
        return RedirectResponse('/?message=' + str(exc).replace(' ', '+'), status_code=303)

    scheduler_running = process_manager.state('scheduler').running
    if scheduler_running:
        process_manager.stop('scheduler')
        process_manager.start('scheduler')
    return RedirectResponse('/?message=Расписание+сохранено', status_code=303)


@app.post('/source/{source_key}/{action}')
def source_action(source_key: str, action: str):
    if action not in {'enable', 'disable'}:
        return HTMLResponse('Unsupported action', status_code=400)
    try:
        set_persisted_source_enabled(
            source_key,
            enabled=action == 'enable',
            path=get_settings().sources_config_path,
        )
    except KeyError:
        return HTMLResponse('Unknown source', status_code=404)
    return RedirectResponse('/?message=Источник+обновлён', status_code=303)


@app.post('/filter')
def save_filter(
    min_discount_percent: str = Form('0'),
    region: str = Form(''),
    city: str = Form(''),
    category: str = Form(''),
    subcategory: str = Form(''),
    offer_type: str = Form(''),
    merchant: str = Form(''),
    max_posts_per_cycle: int = Form(10),
    enabled: str | None = Form(None),
):
    try:
        minimum = Decimal(min_discount_percent.replace(',', '.'))
    except InvalidOperation:
        minimum = Decimal('0')
    update_default_filter(
        enabled=bool(enabled),
        min_discount_percent=max(Decimal('0'), minimum),
        region=region.strip() or None,
        city=city.strip() or None,
        category=category.strip() or None,
        subcategory=subcategory.strip() or None,
        offer_type=offer_type.strip() or None,
        merchant=merchant.strip() or None,
        max_posts_per_cycle=max(1, min(int(max_posts_per_cycle), 100)),
    )
    return RedirectResponse('/?message=Фильтр+сохранён', status_code=303)


@app.post('/parse')
def start_parse(region: str = Form(''), city: str = Form('')):
    if not _parse_state['running']:
        target_region = region.strip() or None
        target_city = city.strip() or None
        threading.Thread(
            target=_run_parse_thread,
            kwargs={'region': target_region, 'city': target_city},
            daemon=True,
        ).start()
    return RedirectResponse('/?message=Парсинг+запущен', status_code=303)


@app.post('/process/{name}/{action}')
def process_action(name: str, action: str):
    if not is_setup_complete():
        return RedirectResponse('/setup', status_code=303)
    try:
        if action == 'start':
            process_manager.start(name)
        elif action == 'stop':
            process_manager.stop(name)
        else:
            return HTMLResponse('Unsupported action', status_code=400)
    except ValueError as exc:
        return HTMLResponse(html.escape(str(exc)), status_code=400)
    return RedirectResponse(f'/?message={name}+{action}', status_code=303)


@app.post('/publish/{offer_id}')
def web_publish(offer_id: int):
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_channel_id:
        return RedirectResponse('/setup', status_code=303)

    async def _publish():
        bot = Bot(token=settings.telegram_bot_token)
        try:
            return await publish_offer(bot, offer_id=offer_id, channel_id=settings.telegram_channel_id)
        finally:
            await bot.session.close()

    result = asyncio.run(_publish())
    return RedirectResponse(f'/?message=Публикация:+{result.status}', status_code=303)


@app.post('/reject/{offer_id}')
def web_reject(offer_id: int):
    with create_session() as session:
        offer = session.get(Offer, offer_id)
        if offer is not None and offer.status in {'new', 'ready', 'needs_review'}:
            offer.status = 'rejected'
            session.commit()
    return RedirectResponse('/?message=Предложение+отклонено', status_code=303)


@app.get('/export')
def web_export():
    tmp_dir = Path(tempfile.mkdtemp(prefix='discount_parser_export_'))
    path = export_offers_xlsx(tmp_dir / 'offers.xlsx')
    return FileResponse(path, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', filename='offers.xlsx')


@app.post('/import')
async def web_import(file: UploadFile = File(...)):
    filename = (file.filename or '').lower()
    if not filename.endswith('.xlsx'):
        return RedirectResponse('/?message=Нужен+файл+.xlsx', status_code=303)
    if file.size is not None and file.size > 20 * 1024 * 1024:
        return RedirectResponse('/?message=Файл+слишком+большой', status_code=303)
    with tempfile.NamedTemporaryFile(prefix='discount_parser_import_', suffix='.xlsx', delete=False) as handle:
        path = Path(handle.name)
        handle.write(await file.read())
    try:
        report = import_offer_corrections(path)
    except Exception as exc:
        return RedirectResponse(f'/?message=Ошибка+импорта:+{type(exc).__name__}', status_code=303)
    finally:
        path.unlink(missing_ok=True)
    message = f'Импорт: изменено {report.rows_changed}, overrides {report.overrides_written}, правил {report.rules_created}, ошибок {len(report.errors)}'
    return RedirectResponse('/?message=' + message.replace(' ', '+'), status_code=303)


@app.on_event('shutdown')
def shutdown_processes() -> None:
    process_manager.stop_all()
