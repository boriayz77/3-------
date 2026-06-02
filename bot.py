import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    TelegramObject,
)
from dotenv import load_dotenv
from gigachat import GigaChat
from supabase import Client, create_client


load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS")
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat")
GIGACHAT_VERIFY_SSL_CERTS = os.getenv("GIGACHAT_VERIFY_SSL_CERTS", "false").lower() == "true"
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
MAX_HISTORY_MESSAGES = int(
    os.getenv("MAX_HISTORY_MESSAGES") or os.getenv("HISTORY_LIMIT") or "12"
)
STREAM_EDIT_INTERVAL = float(os.getenv("STREAM_EDIT_INTERVAL", "0.8"))
DRAFT_STREAM_INTERVAL = float(os.getenv("DRAFT_STREAM_INTERVAL", "0.35"))
TELEGRAM_MESSAGE_LIMIT = 4096
DEBUG_DUMP_CHUNK_LIMIT = 3500
DEBUG_DUMP_CHAT_ID = os.getenv("DEBUG_DUMP_CHAT_ID")
DEFAULT_AGENT_CHAT_ID = 1
MAX_AGENT_CHATS = int(os.getenv("MAX_AGENT_CHATS", "8"))
CUSTOM_EMOJI_ENTITY_TYPE = "custom_emoji"
LLM_LOADING_EMOJI = "🪩"
LLM_LOADING_EMOJI_ID = "5375407018418904583"

SYSTEM_PROMPT = (
    "Ты опытный сантехник и общаешься с пользователем как практикующий мастер. "
    "Отвечай по-русски, если пользователь не попросил другой язык. "
    "Помогай разбираться с протечками, засорами, смесителями, кранами, сифонами, "
    "унитазами, бойлерами, фильтрами, трубами и радиаторами. "
    "Если данных не хватает, сначала задай короткий уточняющий вопрос. "
    "Давай пошаговые, практичные и понятные советы без лишней теории. "
    "Если есть риск затопления, ожога, удара током или повреждения имущества, "
    "сразу предупреждай об этом и советуй перекрыть воду, отключить прибор или вызвать мастера. "
    "Не выдумывай факты и прямо говори, когда по описанию нельзя точно определить причину. "
    "Форматируй ответ как обычный текст для Telegram: без Markdown, без ##, без **жирного**, "
    "без таблиц. Для списков используй простую нумерацию вида '1. Текст'."
)

if not BOT_TOKEN or BOT_TOKEN == "put_your_bot_token_here":
    raise RuntimeError("Add your Telegram bot token to TELEGRAM_BOT_TOKEN in .env")

if not GIGACHAT_CREDENTIALS or GIGACHAT_CREDENTIALS == "put_your_gigachat_auth_key_here":
    raise RuntimeError("Add your GigaChat authorization key to GIGACHAT_CREDENTIALS in .env")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Add SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY to .env")


dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def fetch_one(table: str, **filters: Any) -> dict[str, Any] | None:
    query = supabase.table(table).select("*")
    for column, value in filters.items():
        query = query.eq(column, value)
    data = query.limit(1).execute().data
    return data[0] if data else None


def ensure_agent_chat(user_id: int, agent_chat_id: int) -> dict[str, Any]:
    chat = fetch_one(
        "agent_chats",
        telegram_user_id=user_id,
        agent_chat_id=agent_chat_id,
    )
    if chat:
        return chat

    return (
        supabase.table("agent_chats")
        .insert(
            {
                "telegram_user_id": user_id,
                "agent_chat_id": agent_chat_id,
                "title": f"Чат {agent_chat_id}",
            }
        )
        .execute()
        .data[0]
    )


def ensure_user(user_id: int) -> dict[str, Any]:
    user = fetch_one("bot_users", telegram_user_id=user_id)
    if not user:
        user = (
            supabase.table("bot_users")
            .insert(
                {
                    "telegram_user_id": user_id,
                    "active_agent_chat_id": DEFAULT_AGENT_CHAT_ID,
                    "next_agent_chat_id": DEFAULT_AGENT_CHAT_ID + 1,
                }
            )
            .execute()
            .data[0]
        )

    ensure_agent_chat(user_id, DEFAULT_AGENT_CHAT_ID)
    return user


def get_agent_chat(user_id: int, agent_chat_id: int) -> dict[str, Any] | None:
    ensure_user(user_id)
    return fetch_one(
        "agent_chats",
        telegram_user_id=user_id,
        agent_chat_id=agent_chat_id,
    )


def get_agent_chats(user_id: int) -> list[dict[str, Any]]:
    ensure_user(user_id)
    return (
        supabase.table("agent_chats")
        .select("*")
        .eq("telegram_user_id", user_id)
        .order("agent_chat_id")
        .execute()
        .data
    )



def get_user_id(message: Message) -> int:
    if message.from_user:
        return message.from_user.id

    return message.chat.id


def get_active_agent_chat_id(user_id: int) -> int:
    user = ensure_user(user_id)
    chat_id = user["active_agent_chat_id"]
    ensure_agent_chat(user_id, chat_id)
    return chat_id


def get_history_key(user_id: int, agent_chat_id: int | None = None) -> tuple[int, int]:
    return user_id, agent_chat_id or get_active_agent_chat_id(user_id)


def build_agent_chat_title(user_text: str) -> str:
    title = normalize_answer_text(user_text).replace("\n", " ").strip()
    if not title:
        return "Новый чат"

    return title[:37] + "..." if len(title) > 40 else title


def maybe_update_agent_chat_title(user_id: int, agent_chat_id: int, user_text: str) -> None:
    chat = get_agent_chat(user_id, agent_chat_id)
    current_title = chat["title"] if chat else ""
    if current_title and not current_title.startswith("Чат "):
        return

    supabase.table("agent_chats").update(
        {"title": build_agent_chat_title(user_text)}
    ).eq("telegram_user_id", user_id).eq("agent_chat_id", agent_chat_id).execute()


def create_agent_chat(user_id: int) -> int:
    user = ensure_user(user_id)
    chats = get_agent_chats(user_id)
    if len(chats) >= MAX_AGENT_CHATS:
        return user["active_agent_chat_id"]

    agent_chat_id = user["next_agent_chat_id"]
    ensure_agent_chat(user_id, agent_chat_id)
    supabase.table("bot_users").update(
        {
            "active_agent_chat_id": agent_chat_id,
            "next_agent_chat_id": agent_chat_id + 1,
        }
    ).eq("telegram_user_id", user_id).execute()
    return agent_chat_id


def get_agent_chats_keyboard(user_id: int) -> InlineKeyboardMarkup:
    active_chat_id = get_active_agent_chat_id(user_id)
    rows: list[list[InlineKeyboardButton]] = []

    for chat in get_agent_chats(user_id):
        agent_chat_id = chat["agent_chat_id"]
        title = chat["title"]
        prefix = "✓ " if agent_chat_id == active_chat_id else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{prefix}{title}",
                    callback_data=f"agent_chat:switch:{agent_chat_id}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(text="Новый чат", callback_data="agent_chat:new"),
            InlineKeyboardButton(text="Очистить текущий", callback_data="agent_chat:reset"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_agent_chats_text(user_id: int) -> str:
    active_chat_id = get_active_agent_chat_id(user_id)
    active_chat = get_agent_chat(user_id, active_chat_id)
    active_title = active_chat["title"] if active_chat else f"Чат {active_chat_id}"
    return (
        f"Активный AI-чат: {active_title}\n\n"
        "Выберите чат или создайте новый."
    )


def get_chat_history(history_key: tuple[int, int]) -> list[tuple[str, str]]:
    user_id, agent_chat_id = history_key
    ensure_agent_chat(user_id, agent_chat_id)
    messages = (
        supabase.table("chat_messages")
        .select("role, content")
        .eq("telegram_user_id", user_id)
        .eq("agent_chat_id", agent_chat_id)
        .order("id", desc=True)
        .limit(MAX_HISTORY_MESSAGES)
        .execute()
        .data
    )
    return [(message["role"], message["content"]) for message in reversed(messages)]


def build_prompt(history_key: tuple[int, int], user_text: str) -> str:
    history = get_chat_history(history_key)
    parts = [f"Системная инструкция: {SYSTEM_PROMPT}", ""]

    if history:
        parts.append("Контекст предыдущего диалога:")
        for role, content in history:
            label = "Пользователь" if role == "user" else "Ассистент"
            parts.append(f"{label}: {content}")
        parts.append("")

    parts.append(f"Пользователь: {user_text}")
    parts.append("Ассистент:")
    return "\n".join(parts)


def ask_gigachat(prompt: str) -> str:
    with GigaChat(
        credentials=GIGACHAT_CREDENTIALS,
        scope=GIGACHAT_SCOPE,
        model=GIGACHAT_MODEL,
        verify_ssl_certs=GIGACHAT_VERIFY_SSL_CERTS,
    ) as giga:
        response = giga.chat(prompt)

    return normalize_answer_text(response.choices[0].message.content)


async def stream_gigachat(prompt: str):
    async with GigaChat(
        credentials=GIGACHAT_CREDENTIALS,
        scope=GIGACHAT_SCOPE,
        model=GIGACHAT_MODEL,
        verify_ssl_certs=GIGACHAT_VERIFY_SSL_CERTS,
    ) as giga:
        async for chunk in giga.astream(prompt):
            if not chunk.choices:
                continue

            content = chunk.choices[0].delta.content
            if content:
                yield content


async def generate_answer(history_key: tuple[int, int], user_text: str) -> str:
    prompt = build_prompt(history_key, user_text)
    answer = await asyncio.to_thread(ask_gigachat, prompt)

    save_answer_to_history(history_key, user_text, answer)
    return answer


def save_answer_to_history(history_key: tuple[int, int], user_text: str, answer: str) -> None:
    user_id, agent_chat_id = history_key
    ensure_agent_chat(user_id, agent_chat_id)
    supabase.table("chat_messages").insert(
        [
            {
                "telegram_user_id": user_id,
                "agent_chat_id": agent_chat_id,
                "role": "user",
                "content": user_text,
            },
            {
                "telegram_user_id": user_id,
                "agent_chat_id": agent_chat_id,
                "role": "assistant",
                "content": answer,
            },
        ]
    ).execute()
    prune_chat_history(history_key)


def prune_chat_history(history_key: tuple[int, int]) -> None:
    user_id, agent_chat_id = history_key
    messages = (
        supabase.table("chat_messages")
        .select("id")
        .eq("telegram_user_id", user_id)
        .eq("agent_chat_id", agent_chat_id)
        .order("id", desc=True)
        .execute()
        .data
    )
    old_ids = [message["id"] for message in messages[MAX_HISTORY_MESSAGES:]]
    if old_ids:
        supabase.table("chat_messages").delete().in_("id", old_ids).execute()


def reset_chat_history(history_key: tuple[int, int]) -> None:
    user_id, agent_chat_id = history_key
    supabase.table("chat_messages").delete().eq("telegram_user_id", user_id).eq(
        "agent_chat_id", agent_chat_id
    ).execute()


def set_active_agent_chat(user_id: int, agent_chat_id: int) -> None:
    ensure_agent_chat(user_id, agent_chat_id)
    supabase.table("bot_users").update({"active_agent_chat_id": agent_chat_id}).eq(
        "telegram_user_id", user_id
    ).execute()


def split_telegram_text(text: str) -> list[str]:
    return [
        text[i : i + TELEGRAM_MESSAGE_LIMIT]
        for i in range(0, len(text), TELEGRAM_MESSAGE_LIMIT)
    ] or [""]


def split_debug_dump(text: str) -> list[str]:
    return [
        text[i : i + DEBUG_DUMP_CHUNK_LIMIT]
        for i in range(0, len(text), DEBUG_DUMP_CHUNK_LIMIT)
    ] or [""]


def build_message_debug_dump(message: Message) -> str:
    data = message.model_dump(mode="json", by_alias=True, exclude_none=False)
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


async def send_message_debug_dump(bot: Bot, message: Message) -> None:
    dump_parts = split_debug_dump(build_message_debug_dump(message))
    total_parts = len(dump_parts)
    target_chat_id = DEBUG_DUMP_CHAT_ID or message.chat.id

    for index, dump_part in enumerate(dump_parts, start=1):
        await bot.send_message(
            chat_id=target_chat_id,
            text=f"Поля входящего Message ({index}/{total_parts}):\n{dump_part}",
        )


class MessageDebugDumpMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.from_user and not event.from_user.is_bot:
            try:
                await send_message_debug_dump(data["bot"], event)
            except Exception:
                logging.exception("Failed to send incoming message debug dump")

        return await handler(event, data)


dp.message.outer_middleware(MessageDebugDumpMiddleware())


def normalize_answer_text(text: str) -> str:
    text = re.sub(r"```(?:[\w+-]+)?\n?([\s\S]*?)```", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"(?m)^\s*#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s*>\s?", "", text)
    text = re.sub(r"(?m)^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$", "", text)
    text = re.sub(r"(?m)^\s*[-*_]{3,}\s*$", "", text)
    text = re.sub(r"(?m)^\s*[-*+]\s+", "", text)
    text = re.sub(r"(?m)^(\s*\d+)\)\s+", r"\1. ", text)
    text = text.translate(str.maketrans("", "", "#*_`~|"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def stream_preview(text: str) -> str:
    if len(text) <= TELEGRAM_MESSAGE_LIMIT:
        return text

    return text[: TELEGRAM_MESSAGE_LIMIT - 3] + "..."


def make_draft_id() -> int:
    return int(time.time() * 1000) % 2147483647 or 1


def count_streamed_words(text: str) -> int:
    return len(text.split())


def ends_on_word_boundary(text: str) -> bool:
    return bool(text) and (text[-1].isspace() or text[-1] in ".,!?;:)\n")


async def send_stream_draft(
    bot: Bot,
    chat_id: int,
    draft_id: int,
    text: str,
    message_thread_id: int | None = None,
) -> None:
    await bot.send_message_draft(
        chat_id=chat_id,
        draft_id=draft_id,
        text=stream_preview(text),
        message_thread_id=message_thread_id,
    )


async def send_llm_loading_emoji(message: Message) -> Message | None:
    try:
        stickers = await message.bot.get_custom_emoji_stickers(
            custom_emoji_ids=[LLM_LOADING_EMOJI_ID]
        )
        if stickers:
            return await message.answer_sticker(stickers[0].file_id)

        return await message.answer(
            LLM_LOADING_EMOJI,
            entities=[
                MessageEntity(
                    type=CUSTOM_EMOJI_ENTITY_TYPE,
                    offset=0,
                    length=2,
                    custom_emoji_id=LLM_LOADING_EMOJI_ID,
                )
            ],
        )
    except TelegramBadRequest:
        logging.exception("Failed to send temporary loading emoji")
        return None


async def delete_message_safely(message: Message | None) -> None:
    if not message:
        return

    try:
        await message.delete()
    except TelegramBadRequest:
        logging.exception("Failed to delete temporary message")


async def edit_stream_message(message: Message, text: str) -> None:
    if not text:
        return

    try:
        await message.edit_text(stream_preview(text))
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise


async def edit_message_text_safely(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise


@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    user_id = get_user_id(message)
    get_active_agent_chat_id(user_id)
    await message.answer(
        "Привет. Я сантехник-ассистент на базе GigaChat.\n"
        "Опиши проблему с водой, трубами, краном, унитазом, бойлером или отоплением, "
        "и я помогу разобраться по шагам.\n\n"
        "Команды: /help, /chats, /newchat, /reset"
    )


@dp.message(Command("help"))
async def help_handler(message: Message) -> None:
    await message.answer(
        "Доступные команды:\n"
        "/start - запустить бота\n"
        "/help - показать помощь\n"
        "/chats - выбрать AI-чат\n"
        "/newchat - создать новый AI-чат\n"
        "/reset - очистить память активного AI-чата\n\n"
        "Просто опиши сантехническую проблему текстом. История сохраняется отдельно для каждого AI-чата."
    )


@dp.message(Command("reset"))
async def reset_handler(message: Message) -> None:
    user_id = get_user_id(message)
    agent_chat_id = get_active_agent_chat_id(user_id)
    reset_chat_history(get_history_key(user_id, agent_chat_id))
    chat = get_agent_chat(user_id, agent_chat_id)
    title = chat["title"] if chat else f"Чат {agent_chat_id}"
    await message.answer(f"Контекст активного AI-чата очищен: {title}")


@dp.message(Command("newchat"))
async def new_chat_handler(message: Message) -> None:
    user_id = get_user_id(message)
    before_count = len(get_agent_chats(user_id))
    agent_chat_id = create_agent_chat(user_id)
    after_count = len(get_agent_chats(user_id))

    if after_count == before_count and before_count >= MAX_AGENT_CHATS:
        await message.answer(
            f"Достигнут лимит AI-чатов: {MAX_AGENT_CHATS}. "
            "Очистите текущий чат или переключитесь на существующий через /chats.",
            reply_markup=get_agent_chats_keyboard(user_id),
        )
        return

    chat = get_agent_chat(user_id, agent_chat_id)
    title = chat["title"] if chat else f"Чат {agent_chat_id}"
    await message.answer(
        f"Создан и выбран новый AI-чат: {title}",
        reply_markup=get_agent_chats_keyboard(user_id),
    )


@dp.message(Command("chats"))
async def chats_handler(message: Message) -> None:
    user_id = get_user_id(message)
    await message.answer(
        build_agent_chats_text(user_id),
        reply_markup=get_agent_chats_keyboard(user_id),
    )


@dp.callback_query(F.data.startswith("agent_chat:"))
async def agent_chat_callback_handler(callback: CallbackQuery) -> None:
    if not callback.message:
        await callback.answer()
        return

    user_id = callback.from_user.id
    data = callback.data or ""
    action = data.split(":")
    response_text = "Готово."

    if data == "agent_chat:new":
        before_count = len(get_agent_chats(user_id))
        agent_chat_id = create_agent_chat(user_id)
        after_count = len(get_agent_chats(user_id))
        if after_count == before_count and before_count >= MAX_AGENT_CHATS:
            response_text = f"Лимит AI-чатов: {MAX_AGENT_CHATS}."
        else:
            chat = get_agent_chat(user_id, agent_chat_id)
            title = chat["title"] if chat else f"Чат {agent_chat_id}"
            response_text = f"Выбран новый чат: {title}"
    elif data == "agent_chat:reset":
        agent_chat_id = get_active_agent_chat_id(user_id)
        reset_chat_history(get_history_key(user_id, agent_chat_id))
        response_text = "Активный AI-чат очищен."
    elif len(action) == 3 and action[1] == "switch":
        try:
            agent_chat_id = int(action[2])
        except ValueError:
            await callback.answer("Не удалось выбрать чат.", show_alert=True)
            return

        chat = get_agent_chat(user_id, agent_chat_id)
        if not chat:
            await callback.answer("Этот чат не найден.", show_alert=True)
            return

        set_active_agent_chat(user_id, agent_chat_id)
        response_text = f"Выбран чат: {chat['title']}"

    await callback.answer(response_text)
    await edit_message_text_safely(
        callback.message,
        build_agent_chats_text(user_id),
        reply_markup=get_agent_chats_keyboard(user_id),
    )


@dp.message(F.text)
async def ai_handler(message: Message) -> None:
    user_text = message.text.strip()
    if not user_text:
        await message.answer("Пришли текстовое описание проблемы или вопрос.")
        return

    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
    user_id = get_user_id(message)
    agent_chat_id = get_active_agent_chat_id(user_id)
    history_key = get_history_key(user_id, agent_chat_id)
    use_message_draft = message.chat.type == "private"
    response_message: Message | None = None
    loading_message: Message | None = None
    draft_id = make_draft_id()

    try:
        loading_message = await send_llm_loading_emoji(message)

        if use_message_draft:
            await send_stream_draft(
                message.bot,
                message.chat.id,
                draft_id,
                "",
                message.message_thread_id,
            )
        else:
            response_message = await message.answer("Пишу ответ...")

        prompt = build_prompt(history_key, user_text)
        answer_parts: list[str] = []
        last_edit_at = 0.0
        last_draft_words = 0

        async for chunk in stream_gigachat(prompt):
            answer_parts.append(chunk)
            now = asyncio.get_running_loop().time()
            answer_so_far = "".join(answer_parts)
            display_answer = normalize_answer_text(answer_so_far)

            if use_message_draft:
                streamed_words = count_streamed_words(display_answer)
                if (
                    streamed_words > last_draft_words
                    and ends_on_word_boundary(answer_so_far)
                    and now - last_edit_at >= DRAFT_STREAM_INTERVAL
                ):
                    await send_stream_draft(
                        message.bot,
                        message.chat.id,
                        draft_id,
                        display_answer,
                        message.message_thread_id,
                    )
                    last_draft_words = streamed_words
                    last_edit_at = now
            elif response_message and now - last_edit_at >= STREAM_EDIT_INTERVAL:
                await edit_stream_message(response_message, display_answer)
                last_edit_at = now

        answer = normalize_answer_text("".join(answer_parts))
        maybe_update_agent_chat_title(user_id, agent_chat_id, user_text)
        save_answer_to_history(history_key, user_text, answer)
    except Exception:
        logging.exception("GigaChat request failed")
        await delete_message_safely(loading_message)
        error_text = "Сейчас я не могу ответить. Попробуйте еще раз чуть позже."
        if response_message:
            await edit_stream_message(response_message, error_text)
        else:
            await message.answer(error_text)
        return

    await delete_message_safely(loading_message)

    if not answer:
        empty_answer_text = "Не получилось получить ответ."
        if response_message:
            await edit_stream_message(response_message, empty_answer_text)
        else:
            await message.answer(empty_answer_text)
        return

    answer_messages = split_telegram_text(answer)
    if use_message_draft:
        await send_stream_draft(
            message.bot,
            message.chat.id,
            draft_id,
            answer_messages[0],
            message.message_thread_id,
        )
        await message.answer(answer_messages[0])
    elif response_message:
        await edit_stream_message(response_message, answer_messages[0])

    for answer_part in answer_messages[1:]:
        await message.answer(answer_part)


@dp.message()
async def unknown_handler(message: Message) -> None:
    await message.answer("Пока я умею работать только с текстовыми сообщениями.")
    
async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

