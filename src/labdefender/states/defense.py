from aiogram.fsm.state import State, StatesGroup


class DefenseSlotCreation(StatesGroup):
    date = State()
    start_time = State()
    end_time = State()
    capacity = State()
    confirmation = State()


class QueueChange(StatesGroup):
    confirmation = State()
