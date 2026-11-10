from aiogram.fsm.state import State, StatesGroup


class DefenseDayCreation(StatesGroup):
    date = State()
    capacity = State()
    confirmation = State()


class QueueChange(StatesGroup):
    confirmation = State()
