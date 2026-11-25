"""Состояния конечного автомата (FSM): что бот сейчас ждёт от пользователя."""
from aiogram.fsm.state import State, StatesGroup


class BotState(StatesGroup):
    MENU = State()        # свободный выбор в меню
    UPLOAD = State()      # ждём файл с результатами
    WAIT_NAME = State()   # ждём имя игрока
    BROADCAST = State()   # админ вводит текст рассылки


# имя режима в JSON-сессии -> состояние FSM (нужно для восстановления после перезапуска)
STATE_BY_NAME = {
    "MENU": BotState.MENU,
    "UPLOAD": BotState.UPLOAD,
    "WAIT_NAME": BotState.WAIT_NAME,
    "BROADCAST": BotState.BROADCAST,
}


def mode_name(state: State) -> str:
    """BotState.UPLOAD -> "UPLOAD" (в aiogram state.state выглядит как "BotState:UPLOAD")."""
    return state.state.split(":")[-1]
