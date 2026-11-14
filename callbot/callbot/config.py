"""Settings: secrets come from environment variables (.env), salon facts from salon.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Ready-made model choices. Pick one with LLM_PROVIDER, or set LLM_BASE_URL/LLM_MODEL yourself.
# All of these speak the same "OpenAI-compatible" language, so switching is one line in .env.
PROVIDERS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "openai/gpt-oss-120b",
        "key_env": "GROQ_API_KEY",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "key_env": "DEEPSEEK_API_KEY",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-oss-120b",
        "key_env": "OPENROUTER_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4.1-mini",
        "key_env": "OPENAI_API_KEY",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "model": "gpt-oss:20b",
        "key_env": "",
    },
}


@dataclass
class Service:
    name: str
    price: float
    minutes: int


@dataclass
class Salon:
    name: str
    address: str
    tz: ZoneInfo
    owner_phone: str
    formal: bool
    languages: list[str]
    services: list[Service]
    barbers: list[str]
    hours: dict[int, list[tuple[time, time]]]  # weekday (0=Mon) -> open ranges
    closed_dates: set[date]
    max_days_ahead: int
    min_notice_minutes: int
    slot_step_minutes: int
    allow_cancel: bool
    allow_reschedule: bool
    extra_info: str = ""

    def service(self, name: str) -> Service | None:
        wanted = name.strip().lower()
        for s in self.services:
            if s.name.lower() == wanted:
                return s
        for s in self.services:  # forgiving match ("taglio" -> "Taglio uomo")
            if wanted in s.name.lower() or s.name.lower() in wanted:
                return s
        return None


def _parse_time(s: str) -> time:
    h, m = s.strip().split(":")
    return time(int(h), int(m))


def load_salon(path: str | Path) -> Salon:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    hours: dict[int, list[tuple[time, time]]] = {}
    for i, day in enumerate(WEEKDAYS):
        raw = (data.get("hours") or {}).get(day, "closed")
        ranges = []
        if raw and raw != "closed":
            for r in raw:
                a, b = r.split("-")
                ranges.append((_parse_time(a), _parse_time(b)))
        hours[i] = ranges
    booking = data.get("booking") or {}
    return Salon(
        name=data["name"],
        address=data.get("address", ""),
        tz=ZoneInfo(data.get("timezone", "Europe/Rome")),
        owner_phone=str(data.get("owner_phone", "")),
        formal=bool(data.get("formal", True)),
        languages=list(data.get("languages", ["it"])),
        services=[Service(s["name"], float(s["price"]), int(s["minutes"])) for s in data["services"]],
        barbers=list(data.get("barbers") or ["Titolare"]),
        hours=hours,
        closed_dates={date.fromisoformat(str(d)) for d in data.get("closed_dates") or []},
        max_days_ahead=int(booking.get("max_days_ahead", 30)),
        min_notice_minutes=int(booking.get("min_notice_minutes", 60)),
        slot_step_minutes=int(booking.get("slot_step_minutes", 15)),
        allow_cancel=bool(booking.get("allow_cancel", True)),
        allow_reschedule=bool(booking.get("allow_reschedule", True)),
        extra_info=data.get("extra_info", "") or "",
    )


@dataclass
class Settings:
    # WhatsApp Cloud API
    wa_token: str = ""
    wa_phone_number_id: str = ""
    wa_verify_token: str = ""
    wa_app_secret: str = ""
    wa_api_version: str = "v21.0"
    reminder_template: str = "promemoria_appuntamento"
    reminder_template_lang: str = "it"
    handoff_template: str = "avviso_titolare"
    # AI model
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    transcribe_model: str = "whisper-large-v3-turbo"
    transcribe_base_url: str = ""
    transcribe_api_key: str = ""
    # Calendar
    calendar_backend: str = "local"  # "google" or "local"
    google_calendar_id: str = ""
    google_credentials_file: str = ""
    # Behaviour
    salon_file: str = "salon.yaml"
    db_path: str = "data/callbot.db"
    handoff_pause_hours: float = 12
    new_chat_after_hours: float = 24  # show the AI notice again after this much silence
    reminder_hour: int = 18  # send tomorrow's reminders at this hour (shop time)
    history_messages: int = 20
    extra: dict = field(default_factory=dict)


def load_settings() -> Settings:
    env = os.environ.get
    provider = env("LLM_PROVIDER", "groq").lower()
    preset = PROVIDERS.get(provider, {})
    key_env = preset.get("key_env", "")
    llm_key = env("LLM_API_KEY") or (env(key_env, "") if key_env else "") or ("ollama" if provider == "ollama" else "")
    base = env("LLM_BASE_URL") or preset.get("base_url", "")
    s = Settings(
        wa_token=env("WHATSAPP_ACCESS_TOKEN", ""),
        wa_phone_number_id=env("WHATSAPP_PHONE_NUMBER_ID", ""),
        wa_verify_token=env("WHATSAPP_VERIFY_TOKEN", ""),
        wa_app_secret=env("WHATSAPP_APP_SECRET", ""),
        wa_api_version=env("WHATSAPP_API_VERSION", "v21.0"),
        reminder_template=env("REMINDER_TEMPLATE", "promemoria_appuntamento"),
        reminder_template_lang=env("REMINDER_TEMPLATE_LANG", "it"),
        handoff_template=env("HANDOFF_TEMPLATE", "avviso_titolare"),
        llm_base_url=base,
        llm_api_key=llm_key,
        llm_model=env("LLM_MODEL") or preset.get("model", ""),
        transcribe_model=env("TRANSCRIBE_MODEL", "whisper-large-v3-turbo"),
        # Voice notes are transcribed with Groq's Whisper by default (same free key).
        transcribe_base_url=env("TRANSCRIBE_BASE_URL", PROVIDERS["groq"]["base_url"]),
        transcribe_api_key=env("TRANSCRIBE_API_KEY") or env("GROQ_API_KEY", ""),
        calendar_backend=env("CALENDAR_BACKEND", "local").lower(),
        google_calendar_id=env("GOOGLE_CALENDAR_ID", ""),
        google_credentials_file=env("GOOGLE_CREDENTIALS_FILE", "google-credentials.json"),
        salon_file=env("SALON_FILE", "salon.yaml"),
        db_path=env("DB_PATH", "data/callbot.db"),
        handoff_pause_hours=float(env("HANDOFF_PAUSE_HOURS", "12")),
        new_chat_after_hours=float(env("NEW_CHAT_AFTER_HOURS", "24")),
        reminder_hour=int(env("REMINDER_HOUR", "18")),
        history_messages=int(env("HISTORY_MESSAGES", "20")),
    )
    return s


def load_dotenv(path: str = ".env") -> None:
    """Tiny .env reader so we don't need another dependency."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
