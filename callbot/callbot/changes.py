"""When the owner moves or cancels a booking from her page, the bot tells the customer.

Moving: the new time is held for the customer and they're asked "is this OK?". If they say
yes, the old booking is removed; if no, the new one is removed, the old one stays, and the
owner gets an alert. Cancelling: the customer gets a polite message right away.

Every booking already has the customer's WhatsApp number, so nothing else is needed.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

from .agenda import Appointment
from .booking import Booking, BookingError
from .store import Store

GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def when_text(dt: datetime, lang: str) -> str:
    if lang == "en":
        return f"{DAYS[dt.weekday()]} {dt.day}/{dt.month} at {dt:%H:%M}"
    return f"{GIORNI[dt.weekday()]} {dt.day}/{dt.month} alle {dt:%H:%M}"


def _key(phone: str) -> str:
    return f"proposal:{phone}"


def get_proposal(store: Store, phone: str) -> dict | None:
    raw = store.get(_key(phone))
    return json.loads(raw) if raw else None


def all_proposals(store: Store) -> list[dict]:
    return [json.loads(v) for v in store.values_with_prefix("proposal:")]


def add_owner_alert(store: Store, text: str) -> None:
    alerts = json.loads(store.get("owner_alerts") or "[]")
    alerts.append({"t": time.time(), "text": text})
    store.set("owner_alerts", json.dumps(alerts[-20:], ensure_ascii=False))


def owner_alerts(store: Store, max_age_hours: float = 48) -> list[dict]:
    cutoff = time.time() - max_age_hours * 3600
    return [a for a in json.loads(store.get("owner_alerts") or "[]") if a["t"] >= cutoff]


def customer_lang(store: Store, phone: str) -> str:
    return store.get(f"lang:{phone}") or "it"


def propose_move(booking: Booking, store: Store, message, appt_id: str, new_start: datetime,
                 salon_name: str) -> dict:
    """Hold the new time and ask the customer. message(phone, text, template_params) -> bool."""
    tz = booking.salon.tz
    old = next((a for a in booking.cal.list(booking.now() - timedelta(hours=12),
                                            booking.now() + timedelta(days=400)) if a.id == appt_id), None)
    if not old:
        raise BookingError("Appuntamento non trovato.")
    if not old.phone:
        raise BookingError("Questo appuntamento non ha un numero di telefono: avvisa il cliente a voce.")
    if get_proposal(store, old.phone):
        raise BookingError("C'è già una proposta in attesa di risposta per questo cliente.")
    new = booking.book(old.service, new_start, old.customer_name, old.phone,
                       old.barber if len(booking.salon.barbers) > 1 else None, ignore_notice=True)
    lang = customer_lang(store, old.phone)
    old_w, new_w = when_text(old.start.astimezone(tz), lang), when_text(new.start.astimezone(tz), lang)
    first = (old.customer_name or "").split(" ")[0]
    if lang == "en":
        text = (f"Hi {first}! {salon_name} needs to move your {old.service} appointment from {old_w} "
                f"to {new_w}. Is that OK? Just reply yes or no.")
    else:
        text = (f"Ciao {first}! {salon_name} ha bisogno di spostare il tuo appuntamento ({old.service}) da "
                f"{old_w} a {new_w}. Ti va bene? Rispondi semplicemente sì o no.")
    proposal = {"phone": old.phone, "name": old.customer_name, "service": old.service,
                "old_id": old.id, "new_id": new.id, "old_when": old_w, "new_when": new_w, "t": time.time()}
    store.set(_key(old.phone), json.dumps(proposal, ensure_ascii=False))
    store.add(old.phone, "assistant", text)  # so the AI knows what it asked
    if not message(old.phone, text, [first or "cliente", old.service, old_w, new_w, salon_name]):
        # Couldn't reach them: undo, so nothing changes behind the customer's back.
        booking.cal.delete(new.id)
        store.set(_key(old.phone), "")
        raise BookingError("Non sono riuscito a mandare il messaggio al cliente. Niente è stato spostato.")
    return proposal


def resolve(booking: Booking, store: Store, phone: str, accept: bool) -> dict:
    """The customer answered the proposal."""
    p = get_proposal(store, phone)
    if not p:
        return {"ok": False, "error": "Nessuna proposta in sospeso."}
    store.set(_key(phone), "")
    if accept:
        booking.cal.delete(p["old_id"])
        add_owner_alert(store, f"✅ {p['name']} ha accettato: {p['service']} spostato a {p['new_when']}.")
        return {"ok": True, "result": "accepted", "new_when": p["new_when"]}
    booking.cal.delete(p["new_id"])
    add_owner_alert(store, f"❌ {p['name']} non può {p['new_when']}: resta {p['old_when']}.")
    return {"ok": True, "result": "declined", "kept": p["old_when"]}


def notify_cancel(booking: Booking, store: Store, message, appt: Appointment, salon_name: str,
                  reason: str = "") -> bool:
    if not appt.phone:
        return False
    lang = customer_lang(store, appt.phone)
    w = when_text(appt.start.astimezone(booking.salon.tz), lang)
    first = (appt.customer_name or "").split(" ")[0]
    if lang == "en":
        text = (f"Hi {first}, we're sorry: {salon_name} had to cancel your {appt.service} appointment on {w}"
                + (f" ({reason})" if reason else "") + ". Would you like me to find you another time?")
    else:
        text = (f"Ciao {first}, ci dispiace: {salon_name} ha dovuto cancellare il tuo appuntamento "
                f"({appt.service}) di {w}" + (f" ({reason})" if reason else "")
                + ". Vuoi che ti trovi un altro orario?")
    store.add(appt.phone, "assistant", text)
    # If they had a pending move proposal for this booking, it's no longer relevant.
    p = get_proposal(store, appt.phone)
    if p and appt.id in (p["old_id"], p["new_id"]):
        other = p["new_id"] if appt.id == p["old_id"] else p["old_id"]
        booking.cal.delete(other)
        store.set(_key(appt.phone), "")
    return message(appt.phone, text, [first or "cliente", appt.service, w, salon_name])


# ---------------- Approve mode: the owner accepts each new booking ----------------

def mode(store: Store) -> str:
    """'auto' (the bot books by itself) or 'approve' (the owner accepts each request)."""
    return store.get("booking_mode") or "auto"


def set_mode(store: Store, value: str) -> None:
    store.set("booking_mode", "approve" if value == "approve" else "auto")


def add_request(store: Store, appt: Appointment, when: str) -> None:
    store.set(f"request:{appt.id}", json.dumps({"id": appt.id, "phone": appt.phone, "name": appt.customer_name,
                                                 "service": appt.service, "when": when, "t": time.time()},
                                                ensure_ascii=False))


def pending_requests(store: Store) -> list[dict]:
    return [json.loads(v) for v in store.values_with_prefix("request:")]


def answer_request(booking: Booking, store: Store, message, appt_id: str, accept: bool, salon_name: str,
                   reason: str = "") -> dict:
    raw = store.get(f"request:{appt_id}")
    if not raw:
        raise BookingError("Richiesta non trovata (forse è già stata gestita).")
    r = json.loads(raw)
    store.set(f"request:{appt_id}", "")
    lang = customer_lang(store, r["phone"])
    first = (r["name"] or "").split(" ")[0]
    if accept:
        text = (f"Great news {first}! Your {r['service']} appointment on {r['when']} is confirmed. See you soon!"
                if lang == "en" else
                f"Ottime notizie {first}! Il tuo appuntamento ({r['service']}) di {r['when']} è confermato. A presto!")
    else:
        booking.cal.delete(appt_id)
        extra = f" ({reason})" if reason else ""
        text = (f"Sorry {first}, {salon_name} can't do {r['when']}{extra}. Would you like me to find you another time?"
                if lang == "en" else
                f"Ci dispiace {first}, {salon_name} non può {r['when']}{extra}. Vuoi che ti trovi un altro orario?")
    store.add(r["phone"], "assistant", text)
    message(r["phone"], text, [first or "cliente", r["service"], r["when"], salon_name])
    return {"ok": True}
