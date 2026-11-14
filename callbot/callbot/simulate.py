"""Chat with the bot in your terminal, without WhatsApp. Uses a throwaway calendar.

    python -m callbot.simulate

Needs only an AI key in .env (for example GROQ_API_KEY). Type 'esci' to quit.
"""

from __future__ import annotations

import logging
import sys

from .agenda import LocalCalendar
from .booking import Booking
from .bot import Receptionist
from .config import load_dotenv, load_salon, load_settings
from .llm import LLM
from .store import Store


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    load_dotenv()
    settings = load_settings()
    salon_file = sys.argv[1] if len(sys.argv) > 1 else settings.salon_file
    salon = load_salon(salon_file)
    cal = LocalCalendar()
    booking = Booking(salon, cal)
    llm = LLM(settings.llm_base_url, settings.llm_api_key, settings.llm_model)

    def send(phone: str, text: str) -> None:
        print(f"\n🤖 {text}\n")

    def notify_owner(phone: str, name: str, reason: str) -> None:
        print(f"\n📣 [al titolare] Chat da gestire: {name} +{phone}: {reason}\n")

    bot = Receptionist(salon, settings, llm, booking, Store(), send, notify_owner)
    phone = "393400000000"
    print(f"Simulazione di {salon.name} con il modello {settings.llm_model}. Scrivi 'esci' per uscire.\n")
    while True:
        try:
            text = input("👤 ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in ("esci", "exit", "quit"):
            break
        if text:
            bot.handle(phone, text, "Cliente Prova")
    print("\nAppuntamenti creati:")
    for a in booking.upcoming_for(phone):
        print(f"  {a.start:%a %d/%m %H:%M}  {a.service}  {a.customer_name}")


if __name__ == "__main__":
    main()
