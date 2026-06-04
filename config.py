from dataclasses import dataclass
import os

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    gigachat_credentials: str
    gigachat_scope: str
    gigachat_model: str
    gigachat_verify_ssl_certs: bool
    supabase_url: str
    supabase_key: str


def _get_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Не заполнена переменная окружения {name}")
    return value


def load_settings() -> Settings:
    return Settings(
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or _get_required("BOT_TOKEN"),
        gigachat_credentials=_get_required("GIGACHAT_CREDENTIALS"),
        gigachat_scope=os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
        gigachat_model=os.getenv("GIGACHAT_MODEL", "GigaChat"),
        gigachat_verify_ssl_certs=os.getenv(
            "GIGACHAT_VERIFY_SSL_CERTS",
            "false",
        ).lower() == "true",
        supabase_url=_get_required("SUPABASE_URL"),
        supabase_key=os.getenv("SUPABASE_SERVICE_ROLE_KEY") or _get_required("SUPABASE_KEY"),
    )
