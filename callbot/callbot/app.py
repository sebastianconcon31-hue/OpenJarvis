"""The web server Meta calls whenever a message arrives on the salon's WhatsApp number.

Run it with:  uvicorn callbot.app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Request, Response

from .agenda import make_calendar
from .booking import Booking
from .bot import Receptionist
from .config import load_dotenv, load_salon, load_settings
from .llm import LLM, transcribe
from .reminders import send_due_reminders
from .store import Store
from .whatsapp import WhatsApp, parse_webhook, valid_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("callbot")

load_dotenv()
settings = load_settings()
salon = load_salon(settings.salon_file)
store = Store(settings.db_path)
calendar = make_calendar(settings)
booking = Booking(salon, calendar)
wa = WhatsApp(settings.wa_token, settings.wa_phone_number_id, settings.wa_api_version)
llm = LLM(settings.llm_base_url, settings.llm_api_key, settings.llm_model)


def notify_owner(customer_phone: str, customer_name: str, reason: str) -> None:
    if not salon.owner_phone:
        return
    who = f"{customer_name} (+{customer_phone})" if customer_name else f"+{customer_phone}"
    text = (f"🔔 Chat da gestire: {who}\n{reason}\n\n"
            f"Il bot è in pausa in quella chat. Per riattivarlo scrivi: riprendi {customer_phone}")
    # A normal message only works if the owner wrote to the bot in the last 24h (WhatsApp rule).
    # Otherwise fall back to an approved template.
    if not wa.send_text(salon.owner_phone, text):
        clean = re.sub(r"\s+", " ", reason)[:900]
        wa.send_template(salon.owner_phone, settings.handoff_template, "it",
                         [customer_name or "cliente", f"+{customer_phone}", clean])


receptionist = Receptionist(salon, settings, llm, booking, store, wa.send_text, notify_owner)


async def reminder_loop() -> None:
    while True:
        try:
            n = await asyncio.to_thread(send_due_reminders, salon, settings, calendar, wa.send_template)
            if n:
                log.info("Sent %d reminders", n)
            await asyncio.to_thread(store.cleanup)
        except Exception:  # noqa: BLE001
            log.exception("Reminder run failed")
        await asyncio.sleep(600)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(reminder_loop())
    log.info("CallBot ready for %s, model %s", salon.name, settings.llm_model)
    yield
    task.cancel()


app = FastAPI(title="CallBot", lifespan=lifespan)

if os.environ.get("DEMO_ENABLED", "").lower() in ("1", "true", "yes"):
    from .demo import make_demo_router

    app.include_router(make_demo_router(salon, settings, llm, os.environ.get("DEMO_PASSWORD", "")))


@app.get("/")
def health() -> dict:
    return {"ok": True, "salon": salon.name}


@app.get("/webhook")
def verify(request: Request) -> Response:
    """Meta calls this once when you connect the webhook."""
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and settings.wa_verify_token and \
            q.get("hub.verify_token") == settings.wa_verify_token:
        return Response(q.get("hub.challenge", ""), media_type="text/plain")
    return Response("forbidden", status_code=403)


@app.post("/webhook")
async def incoming(request: Request, background: BackgroundTasks) -> Response:
    body = await request.body()
    if not valid_signature(settings.wa_app_secret, body, request.headers.get("x-hub-signature-256", "")):
        log.warning("Rejected webhook with bad signature")
        return Response("bad signature", status_code=403)
    for ev in parse_webhook(await request.json()):
        background.add_task(process_event, ev)
    return Response("ok")  # answer Meta fast; the real work happens in the background


def process_event(ev: dict) -> None:
    try:
        if ev["kind"] == "owner_echo":
            if ev["to"]:
                receptionist.owner_replied(ev["to"])
            return
        if not store.first_time_seen(ev["id"]):
            return
        wa.mark_read(ev["id"])
        text = ev["text"]
        if ev["type"] == "audio" and ev["media_id"]:
            try:
                audio, mime = wa.download_media(ev["media_id"])
                text = transcribe(audio, "voice.ogg", settings.transcribe_base_url,
                                  settings.transcribe_api_key, settings.transcribe_model)
                text = f"[messaggio vocale] {text}" if text else ""
            except Exception:  # noqa: BLE001
                log.exception("Voice transcription failed")
                text = ""
        if not text:
            text = f"[il cliente ha inviato un messaggio di tipo '{ev['type']}' che non posso leggere]"
        receptionist.handle(ev["from"], text, ev.get("name", ""))
    except Exception:  # noqa: BLE001
        log.exception("Failed to process event")
