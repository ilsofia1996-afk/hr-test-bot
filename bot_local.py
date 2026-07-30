"""
Временный локальный запуск бота на ПК — пока разбираешься с поддержкой
Timeweb. Отключает вебхук (чтобы Telegram не путался, кому слать
обновления) и переключает бота на обычный опрос (polling).

ВАЖНО: пока этот скрипт запущен на твоём ПК, облачная версия (та, что
на Timeweb) отвечать не будет — они не могут работать одновременно
(Telegram разрешает только один способ получения сообщений).

Когда захочешь вернуться к облачной версии — просто закрой это окно
(Ctrl+C) и перезапусти приложение на Timeweb (кнопка паузы/старта в
панели) — оно само заново включит вебхук при старте.

Запуск (в папке test_bot):
    set BOT_TOKEN=...
    set RECRUITER_CHAT_ID=...
    set WEBHOOK_HOST=https://ilsofia1996-afk-hr-test-bot-0a5e.twc1.net
    py -3.13 bot_local.py
"""
import asyncio
import logging
import socket

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession

from bot import _retry_network_errors, router
from config import BOT_TOKEN

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    session = AiohttpSession(timeout=10)
    session._connector_init["family"] = socket.AF_INET
    session.middleware()(_retry_network_errors)

    bot = Bot(
        token=BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(protect_content=True),
    )
    dp = Dispatcher()
    if router.parent_router is None:
        dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Вебхук отключён — бот переключён на локальный опрос (polling).")

    while True:
        try:
            await dp.start_polling(bot)
        except Exception:
            logger.exception("Бот упал с ошибкой, перезапуск через 5 секунд")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
