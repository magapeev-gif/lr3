"""Аналитика выгрузки игры (records.csv): разбор, проверка, рекорды, статистика, ачивки."""
import csv
import html
import io
import logging
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import config

logger = logging.getLogger("bot.analytics")

STATUS_WIN = "Победа"
STATUS_LOSE = "Поражение"
STATUSES = (STATUS_WIN, STATUS_LOSE)

# Заголовок CSV, который создаёт игра (ЛР 2, storage.py)
HEADER = ["Имя", "Статус", "Время, с", "Сдвигов стен", "Дата"]
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_file_lock = threading.Lock()     # запись в общую базу идёт из потоков (asyncio.to_thread)


@dataclass(frozen=True)           # frozen -> запись хешируема: удобно искать дубликаты в set
class Record:
    name: str
    status: str
    time_sec: float
    shifts: int
    date: str


@dataclass
class ImportReport:
    added: int = 0
    duplicates: int = 0
    errors: list = field(default_factory=list)


# ------------------------------------------------------------------ разбор и проверка

def verify_record(r: Record) -> list:
    """Проверка записи на непротиворечивость. Возвращает список проблем (пустой = всё хорошо)."""
    issues = []
    if not r.name:
        issues.append("пустое имя игрока")
    elif len(r.name) > config.MAX_NAME_LEN or not r.name.isprintable():
        issues.append(f"некорректное имя (до {config.MAX_NAME_LEN} печатных символов)")
    if r.status not in STATUSES:
        issues.append(f"недопустимый статус «{r.status}»")
    if not math.isfinite(r.time_sec) or r.time_sec <= 0 or r.time_sec > config.GAME_TIME_LIMIT * 2:
        issues.append("неправдоподобное время партии")
    if r.shifts < 0 or r.shifts > config.MAX_SHIFTS:
        issues.append("неправдоподобное число сдвигов стен")
    try:
        datetime.strptime(r.date, DATE_FORMAT)
    except ValueError:
        issues.append("неверная дата (нужен формат ГГГГ-ММ-ДД чч:мм:сс)")
    return issues


def parse_scores_text(text: str):
    """Разбирает текст CSV. Возвращает (записи, ошибки). Битые строки попадают в ошибки,
    но не ломают разбор остальных и не роняют бота."""
    records, errors = [], []
    reader = csv.reader(io.StringIO(text))
    header_seen = False
    try:
        for row in reader:
            line_no = reader.line_num
            if not any(cell.strip() for cell in row):
                continue                                         # пустая строка
            if not header_seen:
                header_seen = True
                got = [c.strip().lstrip("\ufeff").casefold() for c in row]
                if got != [h.casefold() for h in HEADER]:
                    errors.append(f"Строка {line_no}: неверный заголовок, ожидается "
                                  f"{', '.join(HEADER)}")
                    return [], errors                            # без верного заголовка файл не наш
                continue
            if len(row) != len(HEADER):
                errors.append(f"Строка {line_no}: ожидается {len(HEADER)} полей, найдено {len(row)}")
                continue
            name, status, time_raw, shifts_raw, date = (c.strip() for c in row)
            try:
                record = Record(name, status, float(time_raw.replace(",", ".")), int(shifts_raw), date)
            except ValueError:
                errors.append(f"Строка {line_no}: нечисловые значения времени или сдвигов")
                continue
            issues = verify_record(record)
            if issues:
                errors.append(f"Строка {line_no}: " + "; ".join(issues))
            else:
                records.append(record)
    except csv.Error as e:
        errors.append(f"Файл не является корректным CSV: {e}")
    if not header_seen and not errors:
        errors.append("Файл пуст")
    return records, errors


# ------------------------------------------------------------------ общая база (CSV)

def _read_records(path) -> list:
    """Читает один CSV-файл с результатами. Нет файла / ошибка чтения -> []."""
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        logger.error("Не удалось прочитать %s: %s", path.name, e)
        return []
    records, errors = parse_scores_text(text)
    if errors:
        logger.warning("В файле %s пропущено %d строк", path.name, len(errors))
    return records


def load_records(path=None) -> list:
    """Все результаты для аналитики.
    Без аргумента объединяет два источника: общую базу бота (то, что прислали файлами) и
    records.csv самой игры (его игра пишет сразу после партии, если бот запущен на том же компьютере).
    Одинаковые записи учитываются один раз. С аргументом path читает только этот файл."""
    if path is not None:
        return _read_records(path)
    merged, seen = [], set()
    for source in (config.SCORES_CSV, config.GAME_RECORDS):
        for r in _read_records(source):
            if r not in seen:
                seen.add(r)
                merged.append(r)
    return merged


def sources_info() -> str:
    """Короткая сводка «откуда бот берёт данные» - показывается под таблицей лидеров."""
    parts = []
    for label, p in (("игра", config.GAME_RECORDS), ("загрузки", config.SCORES_CSV)):
        parts.append(f"{label}: {len(_read_records(p))} зап." if p.exists() else f"{label}: файла нет")
    return " | ".join(parts) + f"\nФайл игры: {config.GAME_RECORDS}"


def append_records(records, path=None):
    """Дописывает в базу только НОВЫЕ записи. Возвращает (добавлено, дубликатов).
    Дубликат - полностью совпадающая запись: повторная загрузка того же файла не раздувает базу."""
    path = path or config.SCORES_CSV
    with _file_lock:
        seen = set(load_records(path))
        fresh, duplicates = [], 0
        for r in records:
            if r in seen:
                duplicates += 1
            else:
                seen.add(r)
                fresh.append(r)
        if fresh:
            path.parent.mkdir(parents=True, exist_ok=True)
            is_new = not path.exists() or path.stat().st_size == 0
            with open(path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if is_new:
                    writer.writerow(HEADER)
                for r in fresh:
                    writer.writerow([r.name, r.status, r.time_sec, r.shifts, r.date])
    return len(fresh), duplicates


def import_scores_text(text: str, path=None) -> ImportReport:
    """Полный цикл загрузки: разбор -> проверка -> дописывание в базу."""
    records, errors = parse_scores_text(text)
    added, duplicates = append_records(records, path) if records else (0, 0)
    return ImportReport(added, duplicates, errors)


# ------------------------------------------------------------------ таблица лидеров

def _win_key(r: Record):
    # критерии: 1) меньше время, 2) меньше сдвигов стен, 3) раньше по дате
    return (r.time_sec, r.shifts, r.date)


def best_results(records) -> list:
    """Лучшая ПОБЕДА каждого игрока (имена сравниваются без учёта регистра)."""
    best = {}
    for r in records:
        if r.status != STATUS_WIN:
            continue
        key = r.name.casefold()
        if key not in best or _win_key(r) < _win_key(best[key]):
            best[key] = r
    return list(best.values())


def leaderboard(records, limit: Optional[int] = None) -> list:
    limit = limit or config.LEADERBOARD_LIMIT
    return sorted(best_results(records), key=_win_key)[:limit]


def format_leaderboard(rows, highlight: Optional[str] = None) -> str:
    if not rows:
        return "<b>🏆 Таблица лидеров</b>\nПока нет побед. Загрузите <code>records.csv</code> из игры."
    lines = [f"{'#':<3}{'Игрок':<17}{'Время':>7}{'Сдвиги':>8}"]
    for i, r in enumerate(rows, start=1):
        mine = highlight and r.name.casefold() == highlight.casefold()
        lines.append(f"{i:<2}{'▶' if mine else ' '}{r.name[:16]:<17}{r.time_sec:>6.1f}с{r.shifts:>8}")
    table = html.escape("\n".join(lines))                       # имена из файла - недоверенные данные
    return f"<b>🏆 Таблица лидеров</b>\n<pre>{table}</pre>"


# ------------------------------------------------------------------ личная статистика

@dataclass
class PlayerStats:
    games: int
    wins: int
    losses: int
    win_rate: float
    best_time: Optional[float]
    avg_win_time: Optional[float]
    total_shifts: int
    rank: Optional[int]


def _mine(records, name: str) -> list:
    key = name.casefold()
    return [r for r in records if r.name.casefold() == key]


def player_stats(records, name: str) -> Optional[PlayerStats]:
    mine = _mine(records, name)
    if not mine:
        return None
    wins = [r for r in mine if r.status == STATUS_WIN]
    rank = None
    if wins:
        ordered = sorted(best_results(records), key=_win_key)
        rank = next(i for i, r in enumerate(ordered, start=1) if r.name.casefold() == name.casefold())
    return PlayerStats(
        games=len(mine),
        wins=len(wins),
        losses=len(mine) - len(wins),
        win_rate=100.0 * len(wins) / len(mine),
        best_time=min((r.time_sec for r in wins), default=None),
        avg_win_time=(sum(r.time_sec for r in wins) / len(wins)) if wins else None,
        total_shifts=sum(r.shifts for r in mine),
        rank=rank,
    )


def format_stats(stats: Optional[PlayerStats], name: str) -> str:
    safe = html.escape(name)
    if stats is None:
        return (f"<b>📊 Статистика: {safe}</b>\nЗаписей по этому имени нет. "
                f"Загрузите результаты или смените имя.")
    best = f"{stats.best_time:.1f} с" if stats.best_time is not None else "—"
    avg = f"{stats.avg_win_time:.1f} с" if stats.avg_win_time is not None else "—"
    rank = f"#{stats.rank}" if stats.rank else "—"
    return (f"<b>📊 Статистика: {safe}</b>\n"
            f"Партий: {stats.games} (побед {stats.wins}, поражений {stats.losses})\n"
            f"Доля побед: {stats.win_rate:.0f}%\n"
            f"Лучшее время: {best}\n"
            f"Среднее время побед: {avg}\n"
            f"Сдвигов стен всего: {stats.total_shifts}\n"
            f"Место в таблице: {rank}")


# ------------------------------------------------------------------ ачивки

@dataclass(frozen=True)
class Achievement:
    code: str
    title: str
    description: str
    unlocked: bool


def _best_win_streak(games) -> int:
    best = cur = 0
    for r in games:
        cur = cur + 1 if r.status == STATUS_WIN else 0
        best = max(best, cur)
    return best


def achievements(records, name: str) -> list:
    games = sorted(_mine(records, name), key=lambda r: r.date)   # по времени партий
    wins = [r for r in games if r.status == STATUS_WIN]
    return [
        Achievement("first_win", "Первый выход", "Хотя бы одна победа", bool(wins)),
        Achievement("sprinter", "Скороход", f"Победа быстрее {config.SPRINTER_TIME:g} секунд",
                    any(r.time_sec < config.SPRINTER_TIME for r in wins)),
        Achievement("persistent", "Упорный", "Сыграно 5 партий и больше", len(games) >= 5),
        Achievement("veteran", "Ветеран", "3 победы и больше", len(wins) >= 3),
        Achievement("streak", "Без осечек", "3 победы подряд", _best_win_streak(games) >= 3),
    ]


def format_achievements(items, name: str) -> str:
    done = sum(a.unlocked for a in items)
    lines = [f"<b>🎖 Достижения: {html.escape(name)}</b> ({done}/{len(items)})"]
    for a in items:
        lines.append(f"{'✅' if a.unlocked else '🔒'} <b>{a.title}</b> — {a.description}")
    return "\n".join(lines)


# ------------------------------------------------------------------ лог бота (для админа)

def tail_text(path, lines: int = 20, max_bytes: int = 16384) -> str:
    """Последние строки файла. Читаем только хвост, а не весь (лог может быть большим)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            chunk = f.read()
            truncated = size > max_bytes
    except OSError:
        return ""
    rows = chunk.decode("utf-8", errors="replace").splitlines()
    if truncated and rows:
        rows = rows[1:]                       # первая строка хвоста может быть обрезана
    return "\n".join(rows[-lines:])
