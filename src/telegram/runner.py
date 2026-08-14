from __future__ import annotations

import asyncio
import logging

from aiogram import Dispatcher

from src.shared.config import get_settings
from src.shared.logging import configure_logging
from src.shared.network import NetworkRouteError
from src.telegram.bot import router as control_router
from src.telegram.client import build_bot
from src.telegram.xlsx_handlers import router as xlsx_router

logger = logging.getLogger("src.telegram.runner")


def build_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.include_router(control_router)
    dispatcher.include_router(xlsx_router)
    return dispatcher


async def run_bot_async() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError("DP_TELEGRAM_BOT_TOKEN is not configured")
    if not settings.telegram_admin_id_set:
        raise RuntimeError("DP_TELEGRAM_ADMIN_IDS is not configured")
    
    max_network_retries = 3
    retry_delay = 5.0

    for attempt in range(1, max_network_retries + 1):
        bot = None
        try:
            bot = build_bot(settings.telegram_bot_token)
            dispatcher = build_dispatcher()
            logger.info("telegram_bot_starting_polling")
            await dispatcher.start_polling(bot)
            break
        except (NetworkRouteError, OSError) as exc:
            logger.error(f"telegram_bot_network_error attempt={attempt}/{max_network_retries}: {type(exc).__name__}: {exc}")
            if attempt >= max_network_retries:
                logger.error("telegram_bot_network_unreachable_stopping_gracefully")
                break
            await asyncio.sleep(retry_delay)
        finally:
            if bot is not None and bot.session is not None:
                await bot.session.close()


def run_bot() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, log_format=settings.log_format, component="telegram-bot", enable_file=False)
    asyncio.run(run_bot_async())

