from aiogram.fsm.state import State, StatesGroup


class TeacherRegistration(StatesGroup):
    waiting_for_code = State()
