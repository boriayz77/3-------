from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from config import load_settings
from services.gigachat import GigaChatClient
from services.storage import Storage
from states import ConsultationState


HELP_TEXT = (
    "Я помогу разобраться с ремонтом: составить порядок работ и подобрать материалы.\n\n"
    "Команды:\n"
    "/new - новая консультация\n"
    "/materials - подбор материалов\n"
    "/plan - план работ\n"
    "/history - история\n"
    "/reset - сброс текущей консультации"
)


def build_user_full_name(message: Message) -> str:
    user = message.from_user
    if not user:
        return ""
    parts = [user.first_name, user.last_name]
    return " ".join(part for part in parts if part)


async def save_user(message: Message, storage: Storage) -> None:
    if not message.from_user:
        return
    storage.upsert_user(
        user_id=message.from_user.id,
        username=message.from_user.username,
        full_name=build_user_full_name(message),
    )


async def start_consultation(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ConsultationState.room)
    await message.answer(
        "Начнем новую консультацию. Для какой комнаты нужен совет? "
        "Например: кухня, ванная, спальня, коридор."
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    settings = load_settings()
    bot = Bot(token=settings.bot_token)
    dp = Dispatcher()
    gigachat = GigaChatClient(settings)
    storage = Storage(settings)

    @dp.message(Command("start"))
    async def cmd_start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await save_user(message, storage)
        await message.answer(
            "Привет! Я ремонтный консультант. Помогу составить план работ "
            "и подобрать материалы для ремонта.\n\n"
            "Чтобы начать, отправь /new."
        )
        await message.answer("Я работаю на bothost.")

    @dp.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(HELP_TEXT)

    @dp.message(Command("new"))
    async def cmd_new(message: Message, state: FSMContext) -> None:
        await save_user(message, storage)
        await start_consultation(message, state)

    @dp.message(Command("materials"))
    async def cmd_materials(message: Message, state: FSMContext) -> None:
        await save_user(message, storage)
        await state.update_data(mode="materials")
        await start_consultation(message, state)

    @dp.message(Command("plan"))
    async def cmd_plan(message: Message, state: FSMContext) -> None:
        await save_user(message, storage)
        await state.update_data(mode="plan")
        await start_consultation(message, state)

    @dp.message(Command("history"))
    async def cmd_history(message: Message) -> None:
        if not message.from_user:
            return
        rows = storage.get_history(message.from_user.id)
        if not rows:
            await message.answer("История пока пустая. Начни консультацию командой /new.")
            return

        parts = []
        for index, row in enumerate(rows, start=1):
            question = row["user_message"][:120]
            answer = row["bot_answer"][:300]
            parts.append(f"{index}. Вопрос: {question}\nОтвет: {answer}")
        await message.answer("\n\n".join(parts))

    @dp.message(Command("reset"))
    async def cmd_reset(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Текущая консультация сброшена. Чтобы начать заново, отправь /new.")

    @dp.message(ConsultationState.room, F.text)
    async def process_room(message: Message, state: FSMContext) -> None:
        await state.update_data(room=message.text.strip())
        await state.set_state(ConsultationState.repair_type)
        await message.answer(
            "Какой тип ремонта планируется? Например: косметический, капитальный, "
            "замена отделки, подготовка к покраске."
        )

    @dp.message(ConsultationState.repair_type, F.text)
    async def process_repair_type(message: Message, state: FSMContext) -> None:
        await state.update_data(repair_type=message.text.strip())
        await state.set_state(ConsultationState.question)
        await message.answer(
            "Напиши, что именно ты хочешь сделать в этой комнате или какой совет нужен. "
            "Например: \"хочу поклеить обои\", \"нужно выбрать материал для пола\" "
            "или \"не знаю, с чего начать ремонт\"."
        )

    @dp.message(ConsultationState.question, F.text)
    async def process_question(message: Message, state: FSMContext) -> None:
        if not message.from_user:
            return

        data = await state.get_data()
        room = data["room"]
        repair_type = data["repair_type"]
        question = message.text.strip()

        storage.upsert_user(
            user_id=message.from_user.id,
            username=message.from_user.username,
            full_name=build_user_full_name(message),
            room=room,
            repair_type=repair_type,
        )

        await message.answer("Готовлю консультацию. Это может занять несколько секунд.")
        try:
            answer = await gigachat.build_consultation(room, repair_type, question)
        except Exception:
            logging.exception("GigaChat request failed")
            await message.answer("Не получилось получить ответ от GigaChat. Попробуй позже.")
            return

        request_id = storage.create_request(
            user_id=message.from_user.id,
            room=room,
            repair_type=repair_type,
            topic=question,
            answer=answer,
        )
        storage.add_history(
            user_id=message.from_user.id,
            request_id=request_id,
            user_message=question,
            bot_answer=answer,
        )

        await state.clear()
        await message.answer(answer)

    @dp.message()
    async def fallback(message: Message) -> None:
        await message.answer("Я не понял сообщение. Отправь /new, чтобы начать консультацию.")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
