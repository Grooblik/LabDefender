from aiogram.fsm.state import State, StatesGroup


class AssignmentCreation(StatesGroup):
    title = State()
    description = State()
    confirmation = State()


class SolutionSubmission(StatesGroup):
    payload = State()
    confirmation = State()


class SubmissionReview(StatesGroup):
    comment = State()
