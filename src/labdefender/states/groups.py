from aiogram.fsm.state import State, StatesGroup


class GroupCreation(StatesGroup):
    waiting_for_name = State()
    waiting_for_confirmation = State()
