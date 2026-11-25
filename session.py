"""Stateless JSON-сессии: один файл sessions/<user_id>.json на пользователя.

Бот не держит профили в глобальной памяти. Любое действие - это цикл
«прочитать файл -> изменить -> записать файл», поэтому:
  * сессии разных пользователей физически разделены (изоляция);
  * после перезапуска бота ничего не теряется;
  * повреждённый файл одного пользователя не влияет на остальных.
"""
import json
import logging
import os

import config

logger = logging.getLogger("bot.session")

# Поля, которые должны быть в каждой сессии, и их значения по умолчанию
_DEFAULTS = {
    "state": "MENU",              # режим диалога (зеркало FSM, см. middlewares.RestoreModeMiddleware)
    "name": None,                 # имя игрока, по которому считается личная статистика
    "subscribed": False,          # подписка на уведомления о новых результатах
    "screen_message_id": None,    # id «сообщения-экрана», которое бот редактирует
    "screen_chat_id": None,       # в каком чате это сообщение (id глобален только внутри чата)
}


def create_new_session(user_id: int) -> dict:
    return {"user_id": int(user_id), **_DEFAULTS}


def _path(user_id: int):
    return config.SESSION_DIR / f"{int(user_id)}.json"      # int(): в имя файла не попадёт ничего лишнего


def load_session(user_id: int) -> dict:
    """Читает сессию. Нет файла или он повреждён -> новая сессия (бот не падает)."""
    path = _path(user_id)
    if not path.exists():
        return create_new_session(user_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning("Сессия %s повреждена (%s), создана новая", user_id, e)
        return create_new_session(user_id)
    except OSError as e:
        logger.error("Не удалось прочитать сессию %s: %s", user_id, e)
        return create_new_session(user_id)
    if not isinstance(data, dict):
        logger.warning("Сессия %s имеет неверный формат, создана новая", user_id)
        return create_new_session(user_id)

    session = create_new_session(user_id)
    session.update(data)                                     # недостающие поля остаются по умолчанию
    if not isinstance(session["name"], str):
        session["name"] = None
    session["subscribed"] = bool(session["subscribed"])
    return session


def save_session(user_id: int, data: dict) -> None:
    """Пишет сессию атомарно: сначала во временный файл, затем os.replace.
    Так при сбое посреди записи остаётся старая целая версия, а не обрезанный JSON."""
    config.SESSION_DIR.mkdir(parents=True, exist_ok=True)
    path = _path(user_id)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def update_session(user_id: int, **fields) -> dict:
    """Прочитать -> изменить поля -> записать одним синхронным вызовом.
    Между чтением и записью нет await, поэтому параллельные апдейты не затирают друг друга."""
    session = load_session(user_id)
    session.update(fields)
    save_session(user_id, session)
    return session


def all_sessions():
    """Генератор (user_id, сессия) по всем файлам sessions/*.json."""
    if not config.SESSION_DIR.exists():
        return
    for path in sorted(config.SESSION_DIR.glob("*.json")):
        if path.stem.isdigit():
            uid = int(path.stem)
            yield uid, load_session(uid)


def subscriber_ids() -> list:
    """Список подписчиков собирается из файлов сессий (отдельной базы подписок нет)."""
    return [uid for uid, s in all_sessions() if s.get("subscribed")]
