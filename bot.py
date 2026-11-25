"""Telegram-бот «Chronos Light: аналитик». Точка входа, хендлеры, FSM.

Бот - сервис-помощник для игры, а НЕ копия игры в чате: игровая логика остаётся
в Pygame-приложении, а бот анализирует выгрузку игры (records.csv), которую
присылает пользователь: таблица лидеров, личная статистика, ачивки, уведомления.
"""
import asyncio
import html
import io
import logging
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError,
                                TelegramRetryAfter, TelegramServerError, TelegramUnauthorizedError)
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Document, ErrorEvent, Message

import analytics
import config
import keyboards
import session
from middlewares import RestoreModeMiddleware, ThrottleMiddleware, UpdateLogMiddleware
from states import BotState, mode_name

logger = logging.getLogger("bot")
dp = Dispatcher(storage=MemoryStorage())

ALLOWED_EXT = (".csv", ".txt")
ALLOWED_MIME = {"text/csv", "text/plain", "application/csv", "application/vnd.ms-excel"}

HELP_TEXT = (
    "<b>Chronos Light: аналитик</b>\n"
    "Бот анализирует результаты вашей игры. Саму игру он не запускает.\n\n"
    "1. Сыграйте в игру: она допишет результат в <code>records.csv</code>.\n"
    "2. Нажмите «Загрузить результаты» и пришлите этот файл документом.\n"
    "3. Смотрите таблицу лидеров, свою статистику и достижения.\n\n"
    "Команды: /start - меню, /help - справка, /cancel - выйти из режима ввода."
)
UPLOAD_TEXT = (
    "📤 Пришлите файл <code>records.csv</code> из папки игры (как документ, до 1 МБ).\n"
    "Нужные колонки: <code>Имя, Статус, Время, с, Сдвигов стен, Дата</code>."
)
NAME_TEXT = f"✏️ Введите имя игрока так, как оно записано в игре (до {config.MAX_NAME_LEN} символов)."
BROADCAST_TEXT = "📣 Введите текст рассылки. Он уйдёт всем подписчикам."


# ============================================================ вспомогательные функции

def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def ctx(callback: CallbackQuery) -> Optional[tuple]:
    """(user_id, chat_id) из callback. Сообщение с кнопкой может быть недоступно -> None."""
    msg = callback.message
    if msg is None:
        return None
    return callback.from_user.id, msg.chat.id


async def safe_answer(callback: CallbackQuery, text: Optional[str] = None, show_alert: bool = False) -> None:
    """callback.answer() обязателен (иначе на кнопке «часики»), но просроченный запрос
    («query is too old») не должен ронять обработчик."""
    try:
        await callback.answer(text, show_alert=show_alert)
    except (TelegramBadRequest, TelegramNetworkError) as e:
        logger.info("callback.answer не выполнен: %s", e)


async def try_delete(message: Message) -> None:
    """Убрать сообщение пользователя (имя, файл), чтобы чат оставался чистым. Не получилось - не страшно."""
    try:
        await message.delete()
    except Exception:
        pass


async def set_mode(state: FSMContext, user_id: int, mode: State) -> None:
    """Режим диалога хранится и в FSM (что бот ждёт сейчас), и в JSON-сессии (переживает перезапуск)."""
    await state.set_state(mode)
    session.update_session(user_id, state=mode_name(mode))


async def reset_mode(state: FSMContext, user_id: int) -> None:
    """Любая кнопка меню выводит пользователя из режима ожидания файла/имени."""
    if await state.get_state() not in (None, BotState.MENU.state):
        await set_mode(state, user_id, BotState.MENU)


async def send_with_retry(bot: Bot, chat_id: int, text: str, keyboard=None):
    try:
        return await bot.send_message(chat_id, text, reply_markup=keyboard)
    except (TelegramNetworkError, TelegramServerError) as e:
        logger.warning("Сбой отправки (%s), повторяем", e)
        await asyncio.sleep(1)
        return await bot.send_message(chat_id, text, reply_markup=keyboard)


async def show_screen(bot: Bot, chat_id: int, user_id: int, text: str, keyboard, new: bool = False) -> bool:
    """Один «экран» на пользователя: правим существующее сообщение, а не шлём новое
    (антифлуд, чистый чат). id сообщения и чата лежат в JSON-сессии.
    Возвращает False, если текст не изменился (message is not modified)."""
    sess = session.load_session(user_id)
    screen_id, screen_chat = sess.get("screen_message_id"), sess.get("screen_chat_id")
    if not new and screen_id and screen_chat == chat_id:
        try:
            await bot.edit_message_text(text=text, chat_id=chat_id, message_id=screen_id, reply_markup=keyboard)
            return True
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                return False                                    # нечего менять - это норма
            logger.info("Экран %s недоступен (%s), отправляем новый", screen_id, e)
        except Exception as e:
            logger.info("Не удалось отредактировать экран %s: %s", screen_id, e)
    msg = await send_with_retry(bot, chat_id, text, keyboard)
    session.update_session(user_id, screen_message_id=msg.message_id, screen_chat_id=chat_id)
    return True


async def show_menu(bot: Bot, chat_id: int, user_id: int, note: str = "", new: bool = False) -> bool:
    sess = session.load_session(user_id)
    name = html.escape(sess["name"]) if sess.get("name") else "не задано"
    text = (f"<b>Chronos Light: аналитик</b>\n"
            f"Игрок: <b>{name}</b>\n"
            f"Уведомления: {'включены' if sess.get('subscribed') else 'выключены'}\n")
    if note:
        text += f"\n{note}\n"
    text += "\nВыберите действие кнопками."
    return await show_screen(bot, chat_id, user_id, text, keyboards.menu_keyboard(
        bool(sess.get("subscribed")), is_admin(user_id)), new=new)


async def render_personal(bot: Bot, chat_id: int, user_id: int, kind: str) -> bool:
    """Личная статистика (kind='stats') или достижения (kind='ach') по имени из сессии."""
    name = session.load_session(user_id)["name"]
    records = await asyncio.to_thread(analytics.load_records)
    if kind == "stats":
        text = analytics.format_stats(analytics.player_stats(records, name), name)
    else:
        text = analytics.format_achievements(analytics.achievements(records, name), name)
    return await show_screen(bot, chat_id, user_id, text, keyboards.back_keyboard())


async def ask_name(bot: Bot, chat_id: int, user_id: int, state: FSMContext, then: str, warning: str = "") -> None:
    await set_mode(state, user_id, BotState.WAIT_NAME)
    await state.update_data(then=then)                          # куда вернуться после ввода имени
    await show_screen(bot, chat_id, user_id, (warning + "\n\n" if warning else "") + NAME_TEXT,
                      keyboards.cancel_keyboard())


def fit_pre(raw_lines: list, limit: int = 3500) -> str:
    """Экранированный текст для <pre>, укороченный с начала, чтобы влезть в лимит Telegram (4096)."""
    lines = [ln[:300] for ln in raw_lines]
    body = html.escape("\n".join(lines))
    while len(body) > limit and len(lines) > 1:
        lines = lines[1:]
        body = html.escape("\n".join(lines))
    return body[-limit:] if len(body) > limit else body


async def show_log(bot: Bot, chat_id: int, user_id: int, new: bool = False) -> bool:
    raw = await asyncio.to_thread(analytics.tail_text, config.LOG_FILE, 20)
    body = fit_pre(raw.splitlines()) if raw else "лог пуст"
    return await show_screen(bot, chat_id, user_id,
                             f"<b>📜 bot_flight.log</b> (последние строки)\n<pre>{body}</pre>",
                             keyboards.log_keyboard(), new=new)


async def notify_subscribers(bot: Bot, text: str, exclude: Optional[int] = None) -> tuple:
    """Рассылка подписчикам (их список собирается из файлов сессий). Возвращает (отправлено, ошибок).
    Пауза 0.05 с держит скорость ~20 сообщений/с, ниже лимита Telegram (~30/с)."""
    sent = failed = 0
    for uid in await asyncio.to_thread(session.subscriber_ids):
        if uid == exclude:
            continue
        try:
            try:
                await bot.send_message(uid, text)
            except TelegramRetryAfter as e:                     # 429: ждём сколько просит Telegram
                await asyncio.sleep(e.retry_after)
                await bot.send_message(uid, text)
            sent += 1
        except TelegramForbiddenError:                          # пользователь заблокировал бота
            session.update_session(uid, subscribed=False)
            logger.info("user=%s заблокировал бота, подписка снята", uid)
            failed += 1
        except Exception as e:
            logger.warning("Не удалось отправить уведомление user=%s: %s", uid, e)
            failed += 1
        await asyncio.sleep(0.05)
    return sent, failed


def check_document(doc: Document) -> Optional[str]:
    """Проверка файла ДО скачивания: размер, расширение, mime-тип. None = файл допустим."""
    if doc.file_size is not None and doc.file_size > config.MAX_UPLOAD_BYTES:
        return f"Файл слишком большой (максимум {config.MAX_UPLOAD_BYTES // 1000} КБ)."
    if not (doc.file_name or "").lower().endswith(ALLOWED_EXT):
        return "Нужен текстовый файл .csv (records.csv из папки игры)."
    mime = doc.mime_type or ""
    if mime and mime not in ALLOWED_MIME and not mime.startswith("text/"):
        return f"Неподходящий тип файла ({html.escape(mime)})."
    return None


# ============================================================ команды (без StateFilter: работают из любого состояния)

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    if message.from_user is None:
        return
    uid = message.from_user.id
    await set_mode(state, uid, BotState.MENU)
    await show_menu(bot, message.chat.id, uid, new=True)


@dp.message(Command("help"))
async def cmd_help(message: Message, bot: Bot):
    if message.from_user is None:
        return
    await show_screen(bot, message.chat.id, message.from_user.id, HELP_TEXT, keyboards.back_keyboard(), new=True)


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, bot: Bot):
    if message.from_user is None:
        return
    uid = message.from_user.id
    await set_mode(state, uid, BotState.MENU)
    await show_menu(bot, message.chat.id, uid, note="Действие отменено.", new=True)


@dp.message(Command("log"))
async def cmd_log(message: Message, bot: Bot):
    if message.from_user is None:
        return
    if not is_admin(message.from_user.id):
        logger.warning("user=%s попытался открыть лог без прав", message.from_user.id)
        await message.answer("⛔ Нет прав.")
        return
    await show_log(bot, message.chat.id, message.from_user.id, new=True)


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext, bot: Bot):
    if message.from_user is None:
        return
    uid = message.from_user.id
    if not is_admin(uid):
        logger.warning("user=%s попытался сделать рассылку без прав", uid)
        await message.answer("⛔ Нет прав.")
        return
    await set_mode(state, uid, BotState.BROADCAST)
    await show_screen(bot, message.chat.id, uid, BROADCAST_TEXT, keyboards.cancel_keyboard(), new=True)


# ============================================================ кнопки (callback)

@dp.callback_query(F.data.in_({"menu", "cancel"}))
async def cb_menu(callback: CallbackQuery, state: FSMContext, bot: Bot):
    c = ctx(callback)
    if c is None:
        await safe_answer(callback, "Сообщение недоступно. Отправьте /start", show_alert=True)
        return
    uid, chat_id = c
    await set_mode(state, uid, BotState.MENU)
    await show_menu(bot, chat_id, uid)
    await safe_answer(callback)


@dp.callback_query(F.data.in_({"top", "top:refresh"}))
async def cb_top(callback: CallbackQuery, state: FSMContext, bot: Bot):
    c = ctx(callback)
    if c is None:
        await safe_answer(callback, "Сообщение недоступно. Отправьте /start", show_alert=True)
        return
    uid, chat_id = c
    await reset_mode(state, uid)
    records = await asyncio.to_thread(analytics.load_records)         # файловый ввод-вывод не в event loop
    text = analytics.format_leaderboard(analytics.leaderboard(records), highlight=session.load_session(uid)["name"])
    info = await asyncio.to_thread(analytics.sources_info)
    text += f"\n\n<i>{html.escape(info)}</i>"                      # откуда взяты данные: сразу видно, найден ли файл игры
    changed = await show_screen(bot, chat_id, uid, text, keyboards.top_keyboard())
    await safe_answer(callback, None if changed else "Таблица не изменилась")


@dp.callback_query(F.data.in_({"stats", "ach"}))
async def cb_personal(callback: CallbackQuery, state: FSMContext, bot: Bot):
    c = ctx(callback)
    if c is None:
        await safe_answer(callback, "Сообщение недоступно. Отправьте /start", show_alert=True)
        return
    uid, chat_id = c
    if not session.load_session(uid)["name"]:                         # имя ещё не задано -> запрашиваем
        await ask_name(bot, chat_id, uid, state, then=callback.data)
    else:
        await reset_mode(state, uid)
        await render_personal(bot, chat_id, uid, callback.data)
    await safe_answer(callback)


@dp.callback_query(F.data == "name")
async def cb_name(callback: CallbackQuery, state: FSMContext, bot: Bot):
    c = ctx(callback)
    if c is None:
        await safe_answer(callback, "Сообщение недоступно. Отправьте /start", show_alert=True)
        return
    await ask_name(bot, c[1], c[0], state, then="menu")
    await safe_answer(callback)


@dp.callback_query(F.data == "upload")
async def cb_upload(callback: CallbackQuery, state: FSMContext, bot: Bot):
    c = ctx(callback)
    if c is None:
        await safe_answer(callback, "Сообщение недоступно. Отправьте /start", show_alert=True)
        return
    uid, chat_id = c
    await set_mode(state, uid, BotState.UPLOAD)
    await show_screen(bot, chat_id, uid, UPLOAD_TEXT, keyboards.cancel_keyboard())
    await safe_answer(callback)


@dp.callback_query(F.data == "subscribe")
async def cb_subscribe(callback: CallbackQuery, state: FSMContext, bot: Bot):
    c = ctx(callback)
    if c is None:
        await safe_answer(callback, "Сообщение недоступно. Отправьте /start", show_alert=True)
        return
    uid, chat_id = c
    await reset_mode(state, uid)
    now = not session.load_session(uid).get("subscribed", False)
    session.update_session(uid, subscribed=now)
    logger.info("user=%s подписка: %s", uid, "вкл" if now else "выкл")
    await show_menu(bot, chat_id, uid)
    await safe_answer(callback, "Подписка включена" if now else "Подписка отключена")


@dp.callback_query(F.data == "admin:log")
async def cb_admin_log(callback: CallbackQuery, state: FSMContext, bot: Bot):
    c = ctx(callback)
    if c is None:
        await safe_answer(callback, "Сообщение недоступно. Отправьте /start", show_alert=True)
        return
    uid, chat_id = c
    if not is_admin(uid):                                              # callback_data можно подделать - проверяем права
        logger.warning("user=%s нажал admin:log без прав", uid)
        await safe_answer(callback, "Нет прав", show_alert=True)
        return
    await reset_mode(state, uid)
    changed = await show_log(bot, chat_id, uid)
    await safe_answer(callback, None if changed else "Новых записей нет")


@dp.callback_query(F.data == "admin:broadcast")
async def cb_admin_broadcast(callback: CallbackQuery, state: FSMContext, bot: Bot):
    c = ctx(callback)
    if c is None:
        await safe_answer(callback, "Сообщение недоступно. Отправьте /start", show_alert=True)
        return
    uid, chat_id = c
    if not is_admin(uid):
        logger.warning("user=%s нажал admin:broadcast без прав", uid)
        await safe_answer(callback, "Нет прав", show_alert=True)
        return
    await set_mode(state, uid, BotState.BROADCAST)
    await show_screen(bot, chat_id, uid, BROADCAST_TEXT, keyboards.cancel_keyboard())
    await safe_answer(callback)


# ============================================================ сообщения в состояниях FSM

@dp.message(StateFilter(BotState.WAIT_NAME), F.text)
async def msg_name(message: Message, state: FSMContext, bot: Bot):
    if message.from_user is None:
        return
    uid, chat_id = message.from_user.id, message.chat.id
    name = " ".join(message.text.split())                              # лишние пробелы и переводы строк -> один пробел
    await try_delete(message)
    then = (await state.get_data()).get("then", "menu")
    if not name or len(name) > config.MAX_NAME_LEN or not name.isprintable():
        await ask_name(bot, chat_id, uid, state, then=then,
                       warning=f"⚠️ Имя должно быть от 1 до {config.MAX_NAME_LEN} символов.")
        return
    session.update_session(uid, name=name)
    logger.info("user=%s задал имя %r", uid, name)
    await set_mode(state, uid, BotState.MENU)
    if then in ("stats", "ach"):
        await render_personal(bot, chat_id, uid, then)
    else:
        await show_menu(bot, chat_id, uid, note="Имя сохранено.")


@dp.message(StateFilter(BotState.UPLOAD), F.document)
async def msg_upload(message: Message, state: FSMContext, bot: Bot):
    if message.from_user is None or message.document is None:
        return
    uid, chat_id = message.from_user.id, message.chat.id
    doc = message.document
    await try_delete(message)

    problem = check_document(doc)
    if problem:
        logger.warning("user=%s файл %s отклонён: %s", uid, doc.file_name, problem)
        await show_screen(bot, chat_id, uid, f"⚠️ {problem}\n\n{UPLOAD_TEXT}", keyboards.cancel_keyboard())
        return

    try:
        buffer = io.BytesIO()
        await bot.download(doc, destination=buffer)
        text = buffer.getvalue().decode("utf-8-sig")
    except UnicodeDecodeError:
        await show_screen(bot, chat_id, uid, f"⚠️ Файл не в кодировке UTF-8.\n\n{UPLOAD_TEXT}", keyboards.cancel_keyboard())
        return
    except (TelegramBadRequest, TelegramNetworkError, TelegramServerError) as e:
        logger.error("user=%s не удалось скачать файл: %s", uid, e)
        await show_screen(bot, chat_id, uid, f"⚠️ Не удалось скачать файл. Попробуйте ещё раз.\n\n{UPLOAD_TEXT}",
                          keyboards.cancel_keyboard())
        return

    try:
        report = await asyncio.to_thread(analytics.import_scores_text, text)    # разбор + проверка + запись
    except OSError as e:
        logger.error("user=%s не удалось сохранить результаты: %s", uid, e)
        await set_mode(state, uid, BotState.MENU)
        await show_menu(bot, chat_id, uid, note="⚠️ Не удалось сохранить результаты в базу.")
        return

    logger.info("user=%s загрузил %s: добавлено %d, дубликатов %d, отклонено %d",
                uid, doc.file_name, report.added, report.duplicates, len(report.errors))
    note = (f"✅ Файл обработан\nДобавлено записей: {report.added}\n"
            f"Дубликатов пропущено: {report.duplicates}\nОтклонено строк: {len(report.errors)}")
    if report.errors:
        note += "\n<pre>" + fit_pre(report.errors[:5], limit=1500) + "</pre>"
        if len(report.errors) > 5:
            note += f"…и ещё {len(report.errors) - 5}"
    await set_mode(state, uid, BotState.MENU)
    await show_menu(bot, chat_id, uid, note=note)

    if report.added:                                                    # уведомление подписчикам о новых результатах
        who = html.escape(session.load_session(uid)["name"] or message.from_user.full_name)
        sent, failed = await notify_subscribers(
            bot, f"🔔 В базу добавлено новых результатов: {report.added} (загрузил {who}).", exclude=uid)
        logger.info("Уведомления о новых результатах: отправлено %d, ошибок %d", sent, failed)


@dp.message(StateFilter(BotState.BROADCAST), F.text)
async def msg_broadcast(message: Message, state: FSMContext, bot: Bot):
    if message.from_user is None:
        return
    uid, chat_id = message.from_user.id, message.chat.id
    if not is_admin(uid):                                              # состояние могло остаться от прошлой сессии
        await set_mode(state, uid, BotState.MENU)
        return
    text = message.text
    await try_delete(message)
    sent, failed = await notify_subscribers(bot, f"📣 {html.escape(text)}")
    logger.info("Рассылка админа %s: отправлено %d, ошибок %d", uid, sent, failed)
    await set_mode(state, uid, BotState.MENU)
    await show_menu(bot, chat_id, uid, note=f"📣 Рассылка завершена: отправлено {sent}, ошибок {failed}.")


# Сообщение «не того типа» в режиме ожидания: подсказываем, что нужно (до общего обработчика!)

@dp.message(StateFilter(BotState.UPLOAD))
async def msg_upload_wrong(message: Message, bot: Bot):
    if message.from_user is None:
        return
    await try_delete(message)
    await show_screen(bot, message.chat.id, message.from_user.id,
                      "⚠️ Это не файл. Пришлите <code>records.csv</code> документом или нажмите «Отмена».",
                      keyboards.cancel_keyboard())


@dp.message(StateFilter(BotState.WAIT_NAME))
async def msg_name_wrong(message: Message, bot: Bot):
    if message.from_user is None:
        return
    await try_delete(message)
    await show_screen(bot, message.chat.id, message.from_user.id,
                      "⚠️ Имя нужно прислать текстом.\n\n" + NAME_TEXT, keyboards.cancel_keyboard())


@dp.message(StateFilter(BotState.BROADCAST))
async def msg_broadcast_wrong(message: Message, bot: Bot):
    if message.from_user is None:
        return
    await show_screen(bot, message.chat.id, message.from_user.id,
                      "⚠️ Рассылка поддерживает только текст.\n\n" + BROADCAST_TEXT, keyboards.cancel_keyboard())


# Всё остальное (стикеры, текст в меню, документы вне режима загрузки). Регистрируется ПОСЛЕДНИМ.

@dp.message()
async def msg_fallback(message: Message, bot: Bot):
    if message.from_user is None:
        return
    await show_menu(bot, message.chat.id, message.from_user.id,
                    note="Используйте кнопки. Чтобы загрузить файл, нажмите «Загрузить результаты».", new=True)


# ============================================================ глобальный обработчик ошибок

@dp.errors()
async def on_error(event: ErrorEvent, bot: Bot) -> bool:
    """Страховка для всего остального: пишем traceback в лог и отвечаем пользователю, а не молчим."""
    logger.error("Ошибка апдейта: %s", event.exception, exc_info=event.exception)
    upd = event.update
    chat_id = None
    if upd.message is not None:
        chat_id = upd.message.chat.id
    elif upd.callback_query is not None:
        await safe_answer(upd.callback_query)
        if upd.callback_query.message is not None:
            chat_id = upd.callback_query.message.chat.id
    if chat_id is not None:
        try:
            await bot.send_message(chat_id, "⚠️ Временный сбой. Попробуйте /start")
        except Exception:
            logger.exception("Не удалось сообщить пользователю об ошибке")
    return True


# ============================================================ запуск

def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    file_handler = logging.FileHandler(config.LOG_FILE, encoding="utf-8")     # режим "a": файл дописывается
    console = logging.StreamHandler()
    for handler in (file_handler, console):
        handler.setFormatter(fmt)
        root.addHandler(handler)
    logging.getLogger("aiogram").setLevel(logging.WARNING)                      # свои события важнее шума библиотеки


async def main() -> None:
    setup_logging()
    token = config.require_token()
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    # Порядок: FSM-middleware aiogram регистрирует сам при создании Dispatcher, наши идут после него
    dp.update.outer_middleware(UpdateLogMiddleware())
    dp.update.outer_middleware(RestoreModeMiddleware())
    dp.callback_query.outer_middleware(ThrottleMiddleware(interval=0.6))

    try:
        me = await bot.get_me()
        logger.info("Бот запущен: @%s (администраторов: %d)", me.username, len(config.ADMIN_IDS))
        await dp.start_polling(bot)
    except TelegramUnauthorizedError:
        logger.error("Неверный BOT_TOKEN: Telegram отклонил токен")
        print("Неверный BOT_TOKEN. Проверьте py_bot/.env")
    finally:
        await bot.session.close()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
    except config.ConfigError as e:              # не задан токен: понятное сообщение без трассировки
        print(e)
