"""Промежуточные обработчики: журнал апдейтов, восстановление режима, антифлуд."""
import logging
import time

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import CallbackQuery, Update

import session
from states import STATE_BY_NAME

logger = logging.getLogger("bot.updates")


class UpdateLogMiddleware(BaseMiddleware):
    """Пишет в лог каждый входящий апдейт: кто и что прислал (команда, кнопка, файл)."""

    async def __call__(self, handler, event: Update, data):
        try:
            if event.message is not None:
                m = event.message
                what = f"документ {m.document.file_name}" if m.document else f"сообщение {(m.text or '')[:40]!r}"
                logger.info("user=%s %s", m.from_user.id if m.from_user else "?", what)
            elif event.callback_query is not None:
                c = event.callback_query
                logger.info("user=%s кнопка %s", c.from_user.id, c.data)
        except Exception:                                  # журнал не должен ломать обработку
            logger.exception("Не удалось записать апдейт в лог")
        return await handler(event, data)


class RestoreModeMiddleware(BaseMiddleware):
    """FSM в MemoryStorage теряется при перезапуске бота, а режим диалога сохранён в JSON-сессии.
    Если FSM пуст, а в сессии записан режим (например UPLOAD), возвращаем его в FSM.
    Так FSM и JSON-сессии работают в паре."""

    async def __call__(self, handler, event: Update, data):
        user = data.get("event_from_user")
        state = data.get("state")
        if user is not None and state is not None and data.get("raw_state") is None:
            try:
                mode = session.load_session(user.id).get("state", "MENU")
                target = STATE_BY_NAME.get(mode)
                if target is not None and mode != "MENU":
                    await state.set_state(target)
                    data["raw_state"] = target.state       # StateFilter читает именно raw_state
                    logger.info("user=%s режим %s восстановлен из сессии", user.id, mode)
            except Exception:
                logger.exception("Не удалось восстановить режим из сессии")
        return await handler(event, data)


class ThrottleMiddleware(BaseMiddleware):
    """Антифлуд: нажатия кнопок чаще, чем раз в `interval` секунд, одним пользователем игнорируются.
    Это только временные метки (не профиль): после перезапуска они сбрасываются без последствий."""

    def __init__(self, interval: float = 0.6):
        self.interval = interval
        self._last = {}

    async def __call__(self, handler, event: CallbackQuery, data):
        uid = event.from_user.id
        now = time.monotonic()
        if now - self._last.get(uid, 0.0) < self.interval:
            try:
                await event.answer("Не так быстро 🙂")
            except (TelegramBadRequest, TelegramNetworkError):
                pass
            return None
        self._last[uid] = now
        if len(self._last) > 1000:                         # чистим старые записи, чтобы словарь не рос
            self._last = {u: t for u, t in self._last.items() if now - t < 60}
        return await handler(event, data)
