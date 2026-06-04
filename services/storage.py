from __future__ import annotations

from typing import Any

from supabase import Client, create_client

from config import Settings


class Storage:
    def __init__(self, settings: Settings) -> None:
        self._client: Client = create_client(settings.supabase_url, settings.supabase_key)

    def upsert_user(
        self,
        user_id: int,
        username: str | None,
        full_name: str | None,
        room: str | None = None,
        repair_type: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "id": user_id,
            "username": username,
            "full_name": full_name,
        }
        if room is not None:
            payload["current_room"] = room
        if repair_type is not None:
            payload["current_repair_type"] = repair_type

        self._client.table("users").upsert(payload).execute()

    def create_request(
        self,
        user_id: int,
        room: str,
        repair_type: str,
        topic: str,
        answer: str,
    ) -> int | None:
        payload = {
            "user_id": user_id,
            "room": room,
            "repair_type": repair_type,
            "topic": topic,
            "materials": self._extract_materials(answer),
            "summary": answer[:500],
        }
        result = self._client.table("requests").insert(payload).execute()
        if result.data:
            return int(result.data[0]["id"])
        return None

    def add_history(
        self,
        user_id: int,
        request_id: int | None,
        user_message: str,
        bot_answer: str,
    ) -> None:
        self._client.table("history").insert(
            {
                "user_id": user_id,
                "request_id": request_id,
                "user_message": user_message,
                "bot_answer": bot_answer,
            }
        ).execute()

    def get_history(self, user_id: int, limit: int = 5) -> list[dict[str, Any]]:
        result = (
            self._client.table("history")
            .select("user_message, bot_answer, created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return list(result.data or [])

    def _extract_materials(self, answer: str) -> str:
        marker = "материал"
        lines = [line.strip() for line in answer.splitlines() if marker in line.lower()]
        return "\n".join(lines[:10]) if lines else ""

