from aiogram.fsm.state import State, StatesGroup


class ConsultationState(StatesGroup):
    room = State()
    repair_type = State()
    question = State()

