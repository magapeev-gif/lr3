"""Инлайн-клавиатуры.

callback_data - короткий код действия по схеме «экран» или «экран:действие»
(лимит Telegram - 64 байта). Никаких данных в нём не передаём: всё нужное
бот берёт из JSON-сессии.
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def menu_keyboard(subscribed: bool, is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [_btn("🏆 Топ игроков", "top")],
        [_btn("📊 Моя статистика", "stats"), _btn("🎖 Достижения", "ach")],
        [_btn("📤 Загрузить результаты", "upload")],
        [_btn("👤 Сменить имя", "name")],
        [_btn("🔕 Отписаться от уведомлений" if subscribed else "🔔 Подписаться на уведомления", "subscribe")],
    ]
    if is_admin:
        rows.append([_btn("📣 Рассылка", "admin:broadcast"), _btn("📜 Лог бота", "admin:log")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ В меню", "menu")]])


def cancel_keyboard() -> InlineKeyboardMarkup:
    """Выход из любого режима ввода - иначе пользователь «залипнет» в состоянии."""
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("❌ Отмена", "cancel")]])


def top_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("🔄 Обновить", "top:refresh"), _btn("⬅️ В меню", "menu")]])


def log_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("🔄 Обновить", "admin:log"), _btn("⬅️ В меню", "menu")]])
