"""
Telegram-бот для теста «Воспроизводимость, интеллект и логика».

Тест состоит из двух полностью раздельных блоков:
  1. «Воспроизведение» — своя инструкция, свои вопросы, свой лимит времени.
  2. «Интеллект и логика» — свои вопросы, свой лимит времени.

Между блоками кандидат видит экран паузы с кнопкой "Начать блок 2" —
можно отдохнуть сколько нужно, время в этот момент не идёт.

Каждый отвеченный (или "протухший" по тайм-ауту) вопрос удаляется из
чата, чтобы кандидат не мог прокрутить назад и увидеть его снова.

По завершении теста (или по истечении лимита времени блока) бот
считает баллы и отправляет отчёт в чат/канал рекрутера.

Запуск:
    set BOT_TOKEN=...
    set RECRUITER_CHAT_ID=...
    py -3.13 bot.py
"""
import asyncio
import logging
import socket
import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from config import (
    BOT_TOKEN,
    LOGIC_BLOCK_TIME_LIMIT,
    READING_TIME_LIMIT,
    RECRUITER_CHAT_ID,
    REPRODUCTION_BLOCK_TIME_LIMIT,
    WEBHOOK_HOST,
)
from questions import LOGIC_QUESTIONS, REPRODUCTION_PASSAGE, REPRODUCTION_QUESTIONS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()

# Сессии кандидатов в памяти. Для прототипа этого достаточно;
# для продакшена лучше вынести в Redis/БД, чтобы не терять
# прогресс при перезапуске бота.
sessions: dict[int, dict] = {}
question_timer_tasks: dict[int, asyncio.Task] = {}
block_timer_tasks: dict[int, asyncio.Task] = {}

REP_TOTAL = len(REPRODUCTION_QUESTIONS)
LOGIC_TOTAL = len(LOGIC_QUESTIONS)


def _new_session(candidate_name: str) -> dict:
    return {
        "candidate_name": candidate_name,
        "role": None,
        # not_started -> awaiting_name -> awaiting_role -> reading ->
        # reproduction -> reproduction_done -> logic -> done
        "stage": "not_started",
        "index": 0,
        "last_activity": time.monotonic(),
        "reproduction_answers": [],
        "logic_answers": [],
        "question_started_at": None,
        "answered_current": False,
        "last_message_id": None,
        "last_chat_id": None,
        "passage_message_id": None,
        "reproduction_stopped_by_timeout": False,
        "logic_stopped_by_timeout": False,
    }


def _cancel_user_tasks(user_id: int):
    """Отменяет все фоновые таймеры пользователя — вызывается перед
    стартом нового блока/нового прогона теста, чтобы старые таймеры
    не "выстрелили" параллельно с новыми."""
    current = asyncio.current_task()
    for tasks_dict in (question_timer_tasks, block_timer_tasks):
        task = tasks_dict.get(user_id)
        if task and task is not current:
            task.cancel()


def _cancel_block_timer(user_id: int):
    """Отменяет таймер блока, но не отменяет сам себя, если вызвана
    изнутри этого же таймера (иначе код обрывается на первом же await,
    так и не успев отправить сообщение о завершении блока)."""
    current = asyncio.current_task()
    task = block_timer_tasks.get(user_id)
    if task and task is not current:
        task.cancel()


def _options_keyboard(options: list, prefix: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=opt, callback_data=f"{prefix}:{i}")]
        for i, opt in enumerate(options)
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _delete_message(bot_instance: Bot, chat_id: int, message_id):
    """Удаляет сообщение из чата, если оно ещё существует. Используется,
    чтобы кандидат не мог прокрутить назад и перечитать уже отвеченные
    вопросы или текст инструкции."""
    if not message_id:
        return
    try:
        await bot_instance.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass  # сообщение могло быть уже удалено — не критично


async def _safe_answer(callback: CallbackQuery, *args, **kwargs):
    """Отвечает на нажатие кнопки, но не роняет всю остальную логику,
    если Telegram скажет, что запрос уже "устарел" (например, если сеть
    подтормозила ещё до того, как мы успели ответить), или если сама
    попытка ответить упёрлась в сетевую ошибку даже после всех повторов."""
    try:
        await callback.answer(*args, **kwargs)
    except (TelegramBadRequest, TelegramNetworkError):
        pass


async def _safe(coro):
    """Выполняет любое действие с сообщением (удаление, снятие кнопок и
    т.д.), но не даёт сетевой ошибке оборвать остальную логику вопроса —
    иначе бот "зависает" и не переходит к следующему шагу."""
    try:
        return await coro
    except (TelegramBadRequest, TelegramNetworkError):
        return None


async def _delete_last_question(bot_instance: Bot, session: dict):
    await _delete_message(bot_instance, session["last_chat_id"], session["last_message_id"])


# ---------------------------------------------------------------------------
# Старт, имя, начало блока 1
# ---------------------------------------------------------------------------

STALE_SESSION_SECONDS = 5 * 60  # если сессия "зависла" дольше этого — разрешаем начать заново


@router.message(F.text == "/reset")
async def cmd_reset(message: Message):
    """Принудительно сбрасывает сессию, не дожидаясь тайм-аута "зависшей"
    сессии — полезно при тестировании самим собой."""
    user_id = message.from_user.id
    _cancel_user_tasks(user_id)
    sessions.pop(user_id, None)
    await message.answer("Сессия сброшена. Напиши /start, чтобы начать заново.")


@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id

    existing = sessions.get(user_id)
    if existing:
        idle = time.monotonic() - existing.get("last_activity", 0)
        if idle < STALE_SESSION_SECONDS:
            # Тест уже идёт — игнорируем повторный /start, чтобы не создать
            # второй параллельный поток вопросов поверх текущего.
            await message.answer(
                "Тест уже начат — продолжай отвечать на текущий вопрос выше. "
                "Если что-то зависло, напиши /reset, чтобы начать заново."
            )
            return

    _cancel_user_tasks(user_id)  # на случай "зависших" таймеров от прошлой попытки

    candidate_name = message.from_user.full_name or str(user_id)
    sessions[user_id] = _new_session(candidate_name)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="▶️ Начать", callback_data="start_test")]]
    )
    await message.answer(
        "Привет! Тест состоит из двух отдельных блоков:\n\n"
        "1. Блок «Воспроизведение» — короткая инструкция и вопросы по её деталям.\n"
        "2. Блок «Интеллект и логика» — задачи на мышление.\n\n"
        "Между блоками будет пауза — сможешь отдохнуть перед вторым блоком, "
        "время в этот момент не идёт. У каждого блока свой лимит времени.\n\n"
        "Когда будешь готов(а) — нажми кнопку ниже.",
        reply_markup=keyboard,
    )


@router.callback_query(F.data == "start_test")
async def on_start_test(callback: CallbackQuery):
    user_id = callback.from_user.id
    session = sessions.get(user_id)
    if not session or session["stage"] != "not_started":
        await _safe_answer(callback)
        return

    chat_id = callback.message.chat.id
    bot_instance = callback.bot

    await _safe_answer(callback)
    await _safe(callback.message.edit_reply_markup(reply_markup=None))

    session["stage"] = "awaiting_name"
    await bot_instance.send_message(chat_id, "Как тебя зовут? Напиши имя и фамилию.")


@router.message(F.text & ~F.text.startswith("/"))
async def on_name_input(message: Message):
    user_id = message.from_user.id
    session = sessions.get(user_id)
    if not session or session["stage"] not in ("awaiting_name", "awaiting_role_other"):
        return  # игнорируем случайные сообщения вне контекста теста

    if session["stage"] == "awaiting_name":
        candidate_name = message.text.strip()[:100]
        if not candidate_name:
            await message.answer("Пожалуйста, напиши своё имя текстом.")
            return

        session["candidate_name"] = candidate_name
        chat_id = message.chat.id
        bot_instance = message.bot

        await bot_instance.send_message(chat_id, f"Спасибо, {candidate_name}!")
        await _ask_role(user_id, chat_id, bot_instance)
        return

    # session["stage"] == "awaiting_role_other" — кандидат вписал свою должность вручную
    role_text = message.text.strip()[:100]
    if not role_text:
        await message.answer("Пожалуйста, напиши название должности текстом.")
        return

    session["role"] = role_text
    chat_id = message.chat.id
    bot_instance = message.bot
    await bot_instance.send_message(chat_id, f"Принято: {role_text}. Начинаем блок 1.")
    await _start_reading(user_id, chat_id, bot_instance)


async def _ask_role(user_id: int, chat_id: int, bot_instance: Bot):
    session = sessions[user_id]
    session["stage"] = "awaiting_role"
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Организатор", callback_data="role:Организатор")],
            [InlineKeyboardButton(text="Менеджер по продажам", callback_data="role:Менеджер по продажам")],
            [InlineKeyboardButton(text="SMM", callback_data="role:SMM")],
            [InlineKeyboardButton(text="Другое", callback_data="role:other")],
        ]
    )
    await bot_instance.send_message(chat_id, "На какую должность вы претендуете?", reply_markup=keyboard)


@router.callback_query(F.data.startswith("role:"))
async def on_role_chosen(callback: CallbackQuery):
    user_id = callback.from_user.id
    session = sessions.get(user_id)
    if not session or session["stage"] != "awaiting_role":
        await _safe_answer(callback)
        return

    chat_id = callback.message.chat.id
    bot_instance = callback.bot
    value = callback.data.split("role:", 1)[1]

    await _safe_answer(callback)
    await _safe(callback.message.edit_reply_markup(reply_markup=None))

    if value == "other":
        session["stage"] = "awaiting_role_other"
        await bot_instance.send_message(chat_id, "Напиши название должности текстом.")
        return

    session["role"] = value
    await bot_instance.send_message(chat_id, f"Принято: {value}. Начинаем блок 1.")
    await _start_reading(user_id, chat_id, bot_instance)


# ---------------------------------------------------------------------------
# Блок 1: чтение инструкции
# ---------------------------------------------------------------------------

async def _start_reading(user_id: int, chat_id: int, bot_instance: Bot):
    session = sessions[user_id]
    session["stage"] = "reading"

    passage_msg = await bot_instance.send_message(chat_id, REPRODUCTION_PASSAGE)
    session["passage_message_id"] = passage_msg.message_id

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Я прочитал(а), дальше →", callback_data="reading_done")]]
    )
    msg = await bot_instance.send_message(
        chat_id,
        f"На чтение даётся {READING_TIME_LIMIT} секунд, после этого бот продолжит автоматически.",
        reply_markup=keyboard,
    )
    session["last_message_id"] = msg.message_id
    session["last_chat_id"] = chat_id

    async def reading_timeout():
        await asyncio.sleep(READING_TIME_LIMIT)
        if sessions.get(user_id, {}).get("stage") == "reading":
            await _delete_last_question(bot_instance, session)
            await _delete_message(bot_instance, chat_id, session["passage_message_id"])
            await _start_reproduction_block(user_id, chat_id, bot_instance)

    question_timer_tasks[user_id] = asyncio.create_task(reading_timeout())

    # Таймер блока 1 — покрывает и чтение, и вопросы блока
    async def block_timeout():
        await asyncio.sleep(REPRODUCTION_BLOCK_TIME_LIMIT)
        if sessions.get(user_id, {}).get("stage") in ("reading", "reproduction"):
            await _finish_reproduction_block(user_id, chat_id, bot_instance, stopped_by_timeout=True)

    block_timer_tasks[user_id] = asyncio.create_task(block_timeout())


@router.callback_query(F.data == "reading_done")
async def on_reading_done(callback: CallbackQuery):
    user_id = callback.from_user.id
    session = sessions.get(user_id)
    if not session or session["stage"] != "reading":
        await _safe_answer(callback)
        return

    chat_id = callback.message.chat.id
    bot_instance = callback.bot

    if user_id in question_timer_tasks:
        question_timer_tasks[user_id].cancel()

    await _safe_answer(callback)
    await _delete_last_question(bot_instance, session)
    await _delete_message(bot_instance, chat_id, session["passage_message_id"])
    await _start_reproduction_block(user_id, chat_id, bot_instance)


# ---------------------------------------------------------------------------
# Общая логика вопросов (используется и в блоке 1, и в блоке 2)
# ---------------------------------------------------------------------------

async def _start_reproduction_block(user_id: int, chat_id: int, bot_instance: Bot):
    session = sessions[user_id]
    if session["stage"] != "reading":
        return  # уже запущено другим путём (кнопка/таймер сработали почти одновременно)
    session["stage"] = "reproduction"
    session["index"] = 0
    await bot_instance.send_message(chat_id, "Блок 1: вопросы по тексту выше.")
    await _ask_question(user_id, chat_id, bot_instance)


async def _start_logic_block(user_id: int, chat_id: int, bot_instance: Bot):
    session = sessions[user_id]
    if session["stage"] != "reproduction_done":
        return  # уже запущено другим путём
    session["stage"] = "logic"
    session["index"] = 0
    await bot_instance.send_message(chat_id, "Блок 2: логические задачи. Каждая — на время.")
    await _ask_question(user_id, chat_id, bot_instance)

    async def block_timeout():
        await asyncio.sleep(LOGIC_BLOCK_TIME_LIMIT)
        if sessions.get(user_id, {}).get("stage") == "logic":
            await _finish_logic_block(user_id, chat_id, bot_instance, stopped_by_timeout=True)

    block_timer_tasks[user_id] = asyncio.create_task(block_timeout())


def _current_bank(session: dict) -> list:
    return REPRODUCTION_QUESTIONS if session["stage"] == "reproduction" else LOGIC_QUESTIONS


def _block_total(session: dict) -> int:
    return REP_TOTAL if session["stage"] == "reproduction" else LOGIC_TOTAL


async def _ask_question(user_id: int, chat_id: int, bot_instance: Bot):
    session = sessions[user_id]
    bank = _current_bank(session)
    q = bank[session["index"]]

    session["answered_current"] = False
    session["question_started_at"] = time.monotonic()
    session["last_activity"] = time.monotonic()

    prefix = "rep_ans" if session["stage"] == "reproduction" else "log_ans"
    keyboard = _options_keyboard(q["options"], prefix)

    block_label = "Блок 1" if session["stage"] == "reproduction" else "Блок 2"
    question_num = session["index"] + 1
    total = _block_total(session)
    caption = f"{block_label} — Вопрос {question_num}/{total}\n\n{q['text']}"

    if q.get("image"):
        photo = FSInputFile(q["image"])
        msg = await bot_instance.send_photo(chat_id, photo=photo, caption=caption, reply_markup=keyboard)
    else:
        msg = await bot_instance.send_message(chat_id, caption, reply_markup=keyboard)

    session["last_message_id"] = msg.message_id
    session["last_chat_id"] = chat_id
    # Таймера на отдельный вопрос больше нет — только на блок целиком
    # (см. REPRODUCTION_BLOCK_TIME_LIMIT / LOGIC_BLOCK_TIME_LIMIT в config.py).


def _record_answer(session: dict, q: dict, chosen_index, timed_out: bool):
    session["last_activity"] = time.monotonic()
    time_taken = time.monotonic() - session["question_started_at"]
    is_correct = (chosen_index is not None) and (chosen_index == q["correct_index"])

    record = {
        "question": q["text"],
        "type": q.get("type", "воспроизведение"),
        "correct": is_correct,
        "time_taken": time_taken,
        "time_limit": q["time_limit"],
        "timed_out": timed_out,
    }

    if session["stage"] == "reproduction":
        session["reproduction_answers"].append(record)
    else:
        session["logic_answers"].append(record)


async def _advance(user_id: int, chat_id: int, bot_instance: Bot):
    session = sessions[user_id]
    if session["stage"] not in ("reproduction", "logic"):
        return  # блок уже завершён по тайм-ауту

    session["index"] += 1
    bank = _current_bank(session)

    if session["index"] < len(bank):
        await _ask_question(user_id, chat_id, bot_instance)
        return

    if session["stage"] == "reproduction":
        await _finish_reproduction_block(user_id, chat_id, bot_instance, stopped_by_timeout=False)
    else:
        await _finish_logic_block(user_id, chat_id, bot_instance, stopped_by_timeout=False)


@router.callback_query(F.data.startswith("rep_ans:") | F.data.startswith("log_ans:"))
async def on_answer(callback: CallbackQuery):
    user_id = callback.from_user.id
    session = sessions.get(user_id)
    if not session or session["stage"] not in ("reproduction", "logic"):
        await _safe_answer(callback)
        return

    if session["answered_current"]:
        await _safe_answer(callback, "Уже учтено.")
        return

    session["answered_current"] = True
    if user_id in question_timer_tasks:
        question_timer_tasks[user_id].cancel()

    chosen_index = int(callback.data.split(":")[1])
    bank = _current_bank(session)
    q = bank[session["index"]]
    _record_answer(session, q, chosen_index=chosen_index, timed_out=False)

    await _safe_answer(callback)
    await _safe(callback.message.delete())
    await _advance(user_id, callback.message.chat.id, callback.bot)


# ---------------------------------------------------------------------------
# Завершение блока 1 -> экран паузы -> старт блока 2
# ---------------------------------------------------------------------------

async def _finish_reproduction_block(user_id: int, chat_id: int, bot_instance: Bot, stopped_by_timeout: bool):
    session = sessions.get(user_id)
    if not session or session["stage"] not in ("reading", "reproduction"):
        return  # блок уже был завершён (например, кнопкой и тайм-аутом одновременно)

    if user_id in question_timer_tasks:
        question_timer_tasks[user_id].cancel()
    _cancel_block_timer(user_id)
    session["stage"] = "reproduction_done"

    if stopped_by_timeout:
        await _delete_last_question(bot_instance, session)
        await bot_instance.send_message(chat_id, "⏰ Время на блок 1 закончилось.")

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="▶️ Начать блок 2", callback_data="start_block2")]]
    )
    await bot_instance.send_message(
        chat_id,
        "✅ Блок 1 завершён! Можешь отдохнуть — время не идёт, пока не нажмёшь кнопку ниже.",
        reply_markup=keyboard,
    )


@router.callback_query(F.data == "start_block2")
async def on_start_block2(callback: CallbackQuery):
    user_id = callback.from_user.id
    session = sessions.get(user_id)
    if not session or session["stage"] != "reproduction_done":
        await _safe_answer(callback)
        return

    chat_id = callback.message.chat.id
    bot_instance = callback.bot

    await _safe_answer(callback)
    await _safe(callback.message.edit_reply_markup(reply_markup=None))
    await _start_logic_block(user_id, chat_id, bot_instance)


# ---------------------------------------------------------------------------
# Завершение блока 2 -> финальный отчёт
# ---------------------------------------------------------------------------

async def _finish_logic_block(user_id: int, chat_id: int, bot_instance: Bot, stopped_by_timeout: bool):
    session = sessions.get(user_id)
    if not session or session["stage"] != "logic":
        return

    if user_id in question_timer_tasks:
        question_timer_tasks[user_id].cancel()
    _cancel_block_timer(user_id)

    session["logic_stopped_by_timeout"] = stopped_by_timeout
    session["stage"] = "done"

    if stopped_by_timeout:
        await _delete_last_question(bot_instance, session)
        await bot_instance.send_message(chat_id, "⏰ Время на блок 2 закончилось.")

    await bot_instance.send_message(
        chat_id, "Готово, спасибо! Результаты переданы рекрутеру. Хорошего дня 🙂"
    )

    # Простой подсчёт баллов — без нейросети, без затрат.
    rep_correct = sum(1 for a in session["reproduction_answers"] if a["correct"])
    rep_answered = len(session["reproduction_answers"])
    rep_left = REP_TOTAL - rep_answered
    log_correct = sum(1 for a in session["logic_answers"] if a["correct"])
    log_answered = len(session["logic_answers"])
    log_left = LOGIC_TOTAL - log_answered
    total_correct = rep_correct + log_correct
    total_questions = REP_TOTAL + LOGIC_TOTAL

    rep_line = f"Воспроизведение: {rep_correct}/{REP_TOTAL} верно"
    log_line = f"Интеллект и логика: {log_correct}/{LOGIC_TOTAL} верно"

    percent = round(100 * total_correct / total_questions) if total_questions else 0
    report = (
        f"📋 Результаты теста\n"
        f"Кандидат: {session['candidate_name']}\n"
        f"Должность: {session.get('role') or 'не указана'}\n\n"
        f"{rep_line}\n"
        f"{log_line}\n\n"
        f"Итого: {total_correct}/{total_questions} верно ({percent}%)"
    )

    warnings = []
    if session["reproduction_stopped_by_timeout"] and rep_left:
        warnings.append(f"⚠️ Не успел(а) ответить на {rep_left} вопрос(а/ов) блока «Воспроизведение» — закончилось время")
    if session["logic_stopped_by_timeout"] and log_left:
        warnings.append(f"⚠️ Не успел(а) ответить на {log_left} вопрос(а/ов) блока «Интеллект и логика» — закончилось время")

    if warnings:
        report += "\n\n" + "\n".join(warnings)

    await bot_instance.send_message(RECRUITER_CHAT_ID, report)

    question_timer_tasks.pop(user_id, None)
    block_timer_tasks.pop(user_id, None)
    sessions.pop(user_id, None)


async def _retry_network_errors(make_request, bot, method):
    """Если конкретный запрос к Telegram (отправка сообщения, кнопки и т.д.)
    столкнётся с кратковременным сетевым сбоем — тихо повторяем его
    несколько раз с небольшой (короткой!) паузой, вместо того чтобы сразу
    терять сообщение. Паузы намеренно короткие: у Telegram есть узкое окно
    времени на ответ по нажатой кнопке (callback query), и если тратить
    на повторы слишком много секунд, это окно истечёт само по себе."""
    delays = (0.3, 0.7, 1.5)
    attempts = len(delays) + 1
    for attempt in range(1, attempts + 1):
        try:
            return await make_request(bot, method)
        except TelegramNetworkError:
            if attempt == attempts:
                raise
            delay = delays[attempt - 1]
            logger.warning(
                "Сетевая ошибка при вызове %s (попытка %s/%s), повтор через %.1f сек",
                type(method).__name__, attempt, attempts, delay,
            )
            await asyncio.sleep(delay)


WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"


async def on_startup(bot: Bot):
    webhook_url = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
    await bot.set_webhook(webhook_url)
    logger.info("Webhook установлен: %s", webhook_url)


def main():
    # Многие облачные серверы имеют "сломанный" (не работающий, но и не
    # сразу отказывающий) IPv6-маршрут до внешних серверов. aiohttp по
    # умолчанию может пытаться идти именно через IPv6 и виснуть по
    # тайм-ауту, хотя обычный IPv4 при этом работает нормально.
    # Принудительно используем только IPv4, чтобы это исключить.
    session = AiohttpSession(timeout=10)
    session._connector_init["family"] = socket.AF_INET
    session.middleware()(_retry_network_errors)

    bot = Bot(
        token=BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(protect_content=True),
    )
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)

    # Вебхук вместо long polling: Telegram сам присылает боту обновления
    # по HTTPS, а не бот их постоянно "выпрашивает". Это не зависит от
    # той же нестабильной исходящей сети, которая мешала long polling.
    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    asyncio.run(main())
