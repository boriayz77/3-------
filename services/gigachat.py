from __future__ import annotations

import asyncio

from gigachat import GigaChat
from gigachat.exceptions import AuthenticationError

from config import Settings


class GigaChatClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _ask_sync(self, prompt: str) -> str:
        try:
            with GigaChat(
                credentials=self._settings.gigachat_credentials,
                scope=self._settings.gigachat_scope,
                model=self._settings.gigachat_model,
                verify_ssl_certs=self._settings.gigachat_verify_ssl_certs,
            ) as giga:
                response = giga.chat(prompt)
        except AuthenticationError as error:
            raise RuntimeError(
                "GigaChat не принял GIGACHAT_CREDENTIALS. "
                "Проверьте, что в .env указан актуальный authorization key "
                "из личного кабинета GigaChat API, а не access token."
            ) from error

        return self._normalize_answer(response.choices[0].message.content)

    async def ask(self, prompt: str) -> str:
        return await asyncio.to_thread(self._ask_sync, prompt)

    async def build_consultation(
        self,
        room: str,
        repair_type: str,
        question: str,
    ) -> str:
        prompt = (
            "Системная инструкция: ты помощник по домашнему ремонту для новичков. "
            "Отвечай по-русски, простым языком и без сложных терминов. "
            "Давай практичный порядок работ и список материалов. "
            "Если работа связана с газом, сложной электрикой или риском затопления, "
            "предупреди пользователя и посоветуй обратиться к специалисту. "
            "Не используй Markdown, таблицы, ## и жирный текст.\n\n"
            "Пользователь делает ремонт и просит консультацию.\n"
            f"Комната: {room}\n"
            f"Тип ремонта: {repair_type}\n"
            f"Вопрос: {question}\n\n"
            "Составь ответ в структуре:\n"
            "1. Короткий вывод.\n"
            "2. Порядок работ по шагам.\n"
            "3. Какие материалы могут понадобиться.\n"
            "4. На что обратить внимание.\n"
            "Ответ должен быть понятным для новичка."
        )
        return await self.ask(prompt)

    def _normalize_answer(self, text: str) -> str:
        return text.replace("**", "").replace("##", "").strip()
