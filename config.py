"""Настройки бота: токен и параметры из .env, пути к файлам, лимиты."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _read_env_file(path: Path) -> dict:
    """Мини-парсер .env: строки KEY=VALUE, комментарии (#) и пустые строки пропускаются."""
    values = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:                       # файла .env нет - не ошибка, возьмём переменные окружения
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


_ENV_FILE = _read_env_file(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    """Реальная переменная окружения важнее значения из .env."""
    return os.environ.get(name) or _ENV_FILE.get(name) or default


def _parse_ids(raw: str) -> frozenset:
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return frozenset(ids)


def _parse_float(raw: str, default: float) -> float:
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


# --- секреты и доступ ---
BOT_TOKEN = _env("BOT_TOKEN")                       # токен ТОЛЬКО из .env / окружения
ADMIN_IDS = _parse_ids(_env("ADMIN_IDS"))           # id администраторов через запятую

# --- пути ---
SESSION_DIR = BASE_DIR / "sessions"                 # JSON-сессии: одна на пользователя
DATA_DIR = BASE_DIR / "data"
SCORES_CSV = DATA_DIR / "scores.csv"                # общая база результатов (копится из загрузок)
GAME_RECORDS = Path(_env("GAME_RECORDS", str(BASE_DIR.parent / "records.csv")))   # файл, который пишет сама игра
LOG_FILE = BASE_DIR / "bot_flight.log"

# --- параметры игры и проверки данных ---
GAME_TIME_LIMIT = _parse_float(_env("GAME_TIME_LIMIT", "90"), 90.0)   # лимит времени в игре, с
MAX_NAME_LEN = 16                                   # как NAME_MAX_LEN в игре
MAX_SHIFTS = 100_000                                # верхняя граница «правдоподобного» числа сдвигов
MAX_UPLOAD_BYTES = 1_000_000                        # лимит размера загружаемого файла
LEADERBOARD_LIMIT = 10
SPRINTER_TIME = 45.0                                # порог ачивки «Скороход», с


class ConfigError(Exception):
    """Ошибка настройки (например, не задан токен): показываем текст без трассировки."""


def require_token() -> str:
    if not BOT_TOKEN:
        raise ConfigError(
            "Не задан BOT_TOKEN. Создайте файл py_bot/.env по образцу .env.example "
            "и впишите токен от @BotFather.")
    return BOT_TOKEN
