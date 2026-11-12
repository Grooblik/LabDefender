from aiogram.fsm.state import State, StatesGroup


class TeacherRegistration(StatesGroup):
    waiting_for_code = State()


class TeacherGrant(StatesGroup):
    waiting_for_id = State()


class DisplayName(StatesGroup):
    waiting_for_name = State()
