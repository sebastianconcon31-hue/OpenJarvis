import hashlib
import hmac
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from callbot.agenda import ALL_BARBERS, Appointment, LocalCalendar
from callbot.booking import Booking, BookingError
from callbot.bot import Receptionist, ai_notice, handoff_reply
from callbot.config import Settings, load_salon
from callbot.reminders import send_due_reminders
from callbot.store import Store
from callbot.whatsapp import parse_webhook, valid_signature

ROOT = Path(__file__).resolve().parents[1]
SALON = load_salon(ROOT / "salon.example.yaml")
TZ = SALON.tz
# Tuesday 17 Nov 2026, 10:00 in Vicenza
NOW = datetime(2026, 11, 17, 10, 0, tzinfo=TZ)


def make_booking(salon=SALON, now=NOW):
    return Booking(salon, LocalCalendar(), now=lambda: now)


# ---------------- booking rules ----------------

def test_closed_on_monday_and_holidays():
    b = make_booking()
    assert b.free_slots("Taglio uomo", date(2026, 11, 23)) == []  # Monday
    assert b.free_slots("Taglio uomo", date(2026, 12, 25)) == []  # Christmas


def test_slots_respect_hours_notice_and_lunch():
    b = make_booking()
    slots = [s.strftime("%H:%M") for s in b.free_slots("Taglio uomo", NOW.date())]
    assert slots[0] == "11:00"            # 60 min notice from 10:00
    assert "12:00" in slots and "12:15" not in slots  # 30 min must end by 12:30
    assert "15:00" in slots and "19:00" in slots and "19:15" not in slots


def test_no_double_booking():
    b = make_booking()
    start = datetime(2026, 11, 18, 9, 0, tzinfo=TZ)
    b.book("Taglio uomo", start, "Luca", "391")
    with pytest.raises(BookingError):
        b.book("Barba", start + timedelta(minutes=15), "Anna", "392")
    times = [s.strftime("%H:%M") for s in b.free_slots("Barba", start.date())]
    assert "09:00" not in times and "09:15" not in times and "09:30" in times


def test_two_barbers_allow_parallel_and_choice():
    salon = load_salon(ROOT / "salon.example.yaml")
    salon.barbers = ["Marco", "Gianni"]
    b = make_booking(salon)
    start = datetime(2026, 11, 18, 9, 0, tzinfo=TZ)
    a1 = b.book("Taglio uomo", start, "Luca", "391", barber="Marco")
    a2 = b.book("Taglio uomo", start, "Anna", "392")
    assert {a1.barber, a2.barber} == {"Marco", "Gianni"}
    with pytest.raises(BookingError):
        b.book("Taglio uomo", start, "Pia", "393")


def test_all_day_block_and_rules():
    b = make_booking()
    d = datetime(2026, 11, 19, 0, 0, tzinfo=TZ)
    b.cal.create(Appointment("", d, d + timedelta(days=1), "Ferie", "", "", ALL_BARBERS, True))
    assert b.free_slots("Taglio uomo", d.date()) == []
    with pytest.raises(BookingError):
        b.book("Taglio uomo", NOW + timedelta(minutes=15), "X", "1")  # too soon
    with pytest.raises(BookingError):
        b.book("Taglio uomo", NOW + timedelta(days=40), "X", "1")  # too far
    with pytest.raises(BookingError):
        b.book("Taglio uomo", datetime(2026, 11, 18, 13, 0, tzinfo=TZ), "X", "1")  # lunch


def test_cancel_only_own():
    b = make_booking()
    a = b.book("Taglio uomo", datetime(2026, 11, 18, 9, 0, tzinfo=TZ), "Luca", "391")
    with pytest.raises(BookingError):
        b.cancel("999", a.id)
    b.cancel("391", a.id)
    assert b.upcoming_for("391") == []


# ---------------- the bot, with a scripted fake AI ----------------

class FakeLLM:
    """Plays back a script of AI responses and records what it was sent."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append(messages)
        step = self.script.pop(0)
        if isinstance(step, str):
            return {"role": "assistant", "content": step}
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
            for i, (n, a) in enumerate(step)]}


def make_bot(script, salon=SALON):
    sent, owner = [], []
    booking = make_booking(salon)
    llm = FakeLLM(script)
    bot = Receptionist(salon, Settings(), llm, booking, Store(),
                       lambda p, t: sent.append((p, t)),
                       lambda p, n, r: owner.append((p, n, r)))
    return bot, llm, booking, sent, owner


def test_first_message_shows_ai_notice_once():
    bot, *_ , sent, _ = make_bot(["Certo, come posso aiutarla?", "Siamo aperti dalle 9."])
    bot.handle("391", "Ciao", "Luca")
    bot.handle("391", "Che orari fate?", "Luca")
    texts = [t for _, t in sent]
    assert texts[0] == ai_notice(SALON)
    assert texts.count(ai_notice(SALON)) == 1
    assert texts[-1] == "Siamo aperti dalle 9."


def test_full_booking_flow_uses_tools():
    script = [
        [("check_availability", {"service": "Taglio uomo", "date": "2026-11-18"})],
        "Domani ho libero alle 9:00, 9:30 o 10:00. Quale preferisce?",
        [("book_appointment", {"service": "Taglio uomo", "date": "2026-11-18", "time": "09:30", "customer_name": "Luca Rossi"})],
        "Perfetto Luca, prenotato mercoledì 18 novembre alle 9:30!",
    ]
    bot, llm, booking, sent, _ = make_bot(script)
    bot.handle("391", "Vorrei un taglio domani mattina", "Luca")
    tool_result = json.loads(llm.calls[1][-1]["content"])
    assert "09:30" in tool_result["free_times"]
    bot.handle("391", "Alle 9:30, sono Luca Rossi", "Luca")
    appts = booking.upcoming_for("391")
    assert len(appts) == 1 and appts[0].start.hour == 9 and appts[0].start.minute == 30
    assert "prenotato" in sent[-1][1]


def test_booking_error_is_reported_to_ai_not_crash():
    script = [
        [("book_appointment", {"service": "Taglio uomo", "date": "2026-11-18", "time": "13:00", "customer_name": "Luca"})],
        "Mi spiace, a quell'ora siamo chiusi.",
    ]
    bot, llm, booking, sent, _ = make_bot(script)
    bot.handle("391", "Prenota alle 13", "Luca")
    assert json.loads(llm.calls[1][-1]["content"])["ok"] is False
    assert booking.upcoming_for("391") == []


def test_human_request_hands_off_and_pauses():
    bot, llm, _, sent, owner = make_bot(["non dovrebbe servire"])
    bot.handle("391", "voglio parlare con un operatore", "Luca")
    assert owner and owner[0][0] == "391"
    assert sent[-1][1] == handoff_reply(SALON)
    n = len(sent)
    bot.handle("391", "ci sei?", "Luca")
    assert len(sent) == n and llm.calls == []  # paused: bot stays silent


def test_ai_handoff_tool_and_owner_resume():
    script = [[("handoff_to_owner", {"reason": "Chiede un prezzo per sposi"})], "Ho avvisato il titolare!"]
    bot, _, _, sent, owner = make_bot(script)
    bot.handle("391", "Fate pacchetti per matrimoni?", "Luca")
    assert owner[0][2] == "Chiede un prezzo per sposi"
    assert bot.store.is_paused("391")
    bot.handle(SALON.owner_phone, "riprendi 391")
    assert not bot.store.is_paused("391")


def test_owner_echo_pauses_bot():
    bot, *_ = make_bot([])
    bot.owner_replied("391")
    assert bot.store.is_paused("391")


def test_ai_failure_gets_polite_reply_and_owner_alert():
    class Broken:
        def chat(self, *a, **k):
            raise RuntimeError("down")
    bot, _, _, sent, owner = make_bot([])
    bot.llm = Broken()
    bot.handle("391", "Ciao", "Luca")
    assert "problema tecnico" in sent[-1][1] and owner


def test_system_prompt_has_real_dates():
    bot, llm, *_ = make_bot(["ok"])
    bot.handle("391", "ciao", "Luca")
    prompt = llm.calls[0][0]["content"]
    assert "martedì 17 novembre = 2026-11-17 (oggi)" in prompt
    assert "mercoledì 18 novembre = 2026-11-18 (domani)" in prompt
    assert "Taglio uomo: €15" in prompt


# ---------------- reminders ----------------

def test_reminders_sent_once_after_hour():
    cal = LocalCalendar()
    b = Booking(SALON, cal, now=lambda: NOW)
    b.book("Taglio uomo", datetime(2026, 11, 18, 9, 0, tzinfo=TZ), "Luca Rossi", "391")
    b.book("Barba", datetime(2026, 11, 20, 9, 0, tzinfo=TZ), "Anna", "392")  # not tomorrow
    sent = []
    tpl = lambda *a: sent.append(a) or True  # noqa: E731
    assert send_due_reminders(SALON, Settings(), cal, tpl, now=NOW) == 0  # 10:00 is before 18:00
    evening = NOW.replace(hour=18, minute=5)
    assert send_due_reminders(SALON, Settings(), cal, tpl, now=evening) == 1
    assert sent[0][0] == "391" and sent[0][3] == ["Luca", "Taglio uomo", "09:00", SALON.name]
    assert send_due_reminders(SALON, Settings(), cal, tpl, now=evening) == 0


# ---------------- WhatsApp webhook parsing ----------------

def test_parse_webhook_and_signature():
    payload = {"entry": [{"changes": [
        {"field": "messages", "value": {
            "contacts": [{"wa_id": "391", "profile": {"name": "Luca"}}],
            "messages": [{"id": "m1", "from": "391", "type": "text", "text": {"body": "Ciao"}},
                         {"id": "m2", "from": "391", "type": "audio", "audio": {"id": "media9"}}]}},
        {"field": "smb_message_echoes", "value": {"message_echoes": [{"to": "392"}]}},
    ]}]}
    evs = parse_webhook(payload)
    assert evs[0] == {"kind": "message", "id": "m1", "from": "391", "name": "Luca", "type": "text",
                      "text": "Ciao", "media_id": ""}
    assert evs[1]["media_id"] == "media9"
    assert evs[2] == {"kind": "owner_echo", "to": "392"}
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert valid_signature("secret", body, sig)
    assert not valid_signature("secret", body, "sha256=bad")
    assert not valid_signature("", body, sig)


def test_store_dedupes_messages():
    s = Store()
    assert s.first_time_seen("m1") and not s.first_time_seen("m1")


# ---------------- web demo ----------------

def test_demo_chat_books_into_practice_calendar():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from callbot.config import Settings
    from callbot.demo import make_demo_router

    tomorrow = (datetime.now(TZ) + timedelta(days=1))
    while tomorrow.weekday() in (0, 6):  # example salon is closed Sun/Mon
        tomorrow += timedelta(days=1)
    d = tomorrow.date().isoformat()
    llm = FakeLLM([
        [("book_appointment", {"service": "Barba", "date": d, "time": "10:00", "customer_name": "Demo"})],
        "Prenotato!",
    ])
    app = FastAPI()
    app.include_router(make_demo_router(SALON, Settings(), llm, password="pw"))
    c = TestClient(app)
    assert c.post("/demo/chat", json={"session": "123", "text": "ciao", "password": "no"}).status_code == 401
    r = c.post("/demo/chat", json={"session": "123", "text": "barba alle 10", "password": "pw"}).json()
    assert r["replies"][0] == ai_notice(SALON) and r["replies"][-1] == "Prenotato!"
    agenda = c.get("/demo/agenda", params={"password": "pw"}).json()["appointments"]
    assert len(agenda) == 1 and agenda[0]["service"] == "Barba"
    assert "Barberia" in c.get("/demo").text
    c.post("/demo/reset", json={"session": "123", "text": "", "password": "pw"})
    assert c.get("/demo/agenda", params={"password": "pw"}).json()["appointments"] == []


# ---------------- the 150 common questions ----------------

def test_faq_has_150_unique_bilingual_entries_with_known_placeholders():
    import re as _re

    from callbot.faq import AUTO, load_faq

    entries = load_faq(ROOT / "faq.yaml")
    assert len(entries) == 150
    assert len({e.id for e in entries}) == 150
    for e in entries:
        assert e.q_it and e.q_en and e.a_it and e.a_en, e.id
        for text in (e.a_it, e.a_en):
            for ph in _re.findall(r"\{(\w+)\}", text):
                assert ph in AUTO or ph in e.needs, (e.id, ph)
        # Every fact an answer uses is listed in the example salon file
        for k in e.needs:
            assert k in SALON.facts, (e.id, k)


def test_faq_search_finds_its_own_questions():
    from callbot.faq import FAQ, load_faq

    entries = load_faq(ROOT / "faq.yaml")
    faq = FAQ(entries, SALON)
    total = found = 0
    for e in entries:
        for q in e.q_it:
            total += 1
            found += e.id in [r["id"] for r in faq.search(q, "it")]
    assert found / total > 0.97


def test_faq_fills_salon_facts_and_never_guesses_missing_ones():
    from callbot.faq import FAQ, load_faq

    faq = FAQ(load_faq(ROOT / "faq.yaml"), SALON)
    prices = faq.search("quanto costa un taglio")[0]
    assert prices["id"] == "prezzi" and "Taglio uomo €15" in prices["answer"]
    park = faq.search("is there parking", "en")[0]
    assert park["id"] == "parcheggio" and "car park" in park["answer"]
    wifi = faq.search("avete il wifi?")[0]
    assert wifi["id"] == "wifi" and wifi["answer"] is None and wifi["missing_info"] == ["wifi"]
    assert "{" not in prices["answer"]


def test_bot_uses_faq_tool():
    from callbot.faq import FAQ, load_faq

    script = [[("search_faq", {"query": "dove parcheggio?"})], "C'è un parcheggio a 2 minuti a piedi."]
    sent, owner = [], []
    llm = FakeLLM(script)
    bot = Receptionist(SALON, Settings(), llm, make_booking(), Store(),
                       lambda p, t: sent.append(t), lambda *a: owner.append(a),
                       FAQ(load_faq(ROOT / "faq.yaml"), SALON))
    bot.handle("391", "dove posso parcheggiare?", "Luca")
    result = json.loads(llm.calls[1][-1]["content"])
    assert result["results"][0]["id"] == "parcheggio"
    assert sent[-1].startswith("C'è un parcheggio")


def test_opening_message_matches_customer_language():
    from callbot.bot import detect_lang

    assert detect_lang("Hey, what exact hours are you open?", SALON) == "en"
    assert detect_lang("Ciao, che orari fate?", SALON) == "it"
    assert detect_lang("Buongiorno, vorrei prenotare un taglio", SALON) == "it"
    bot, *_, sent, _ = make_bot(["We're open Tuesday to Saturday."])
    bot.handle("391", "Hey, what exact hours are you open?", "Tom")
    assert sent[0][1] == ai_notice(SALON, "en") and sent[0][1].startswith("Hi!")
    bot2, *_, sent2, _ = make_bot(["non serve"])
    bot2.handle("392", "I want to talk to a real person", "Tom")
    assert sent2[-1][1] == handoff_reply(SALON, "en")


def test_broken_ai_reply_is_retried_and_english_is_requested():
    from callbot.bot import looks_broken

    garbage = "Ecco il listino:\n" + "\n\n... \n\n... ".join(["Sorry…"] * 20)
    assert looks_broken(garbage)
    assert not looks_broken("Our prices: men's cut €15, beard €10. Want to book?")
    bot, llm, *_, sent, _ = make_bot([garbage, "Our men's cut is €15."])
    bot.handle("391", "ok, what are your prices?", "Tom")
    assert sent[-1][1] == "Our men's cut is €15."
    assert "rispondi in inglese" in llm.calls[0][0]["content"]


# ---------------- owner's page ----------------

def test_owner_page_shows_day_and_manages_bookings():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from callbot.owner import make_owner_router

    b = make_booking()
    b.book("Taglio uomo", datetime(2026, 11, 18, 9, 0, tzinfo=TZ), "Luca Rossi", "391")
    app = FastAPI()
    app.include_router(make_owner_router(b, "segreta"))
    c = TestClient(app)
    H = {"X-Owner-Password": "segreta"}
    assert c.get("/titolare/api/day?d=2026-11-18").status_code == 401
    day = c.get("/titolare/api/day?d=2026-11-18", headers=H).json()
    assert day["open"] and day["count"] == 1 and day["items"][0]["name"] == "Luca Rossi"
    assert 0 < day["full"] < 10 and day["free"][0] == "09:30-12:30"
    # walk-in booked by the owner right now, despite the 60-minute notice rule
    r = c.post("/titolare/api/book", headers=H, json={"date": "2026-11-17", "time": "10:15",
                                                     "service": "Barba", "name": "Walk-in"})
    assert r.status_code == 200, r.text
    # block the afternoon: the bot can't book there any more
    c.post("/titolare/api/block", headers=H, json={"date": "2026-11-18", "start": "15:00", "end": "19:30"})
    assert not [s for s in b.free_slots("Taglio uomo", date(2026, 11, 18)) if s.hour >= 15]
    # cancel
    c.post("/titolare/api/cancel", headers=H, json={"id": day["items"][0]["id"]})
    assert c.get("/titolare/api/day?d=2026-11-18", headers=H).json()["count"] == 0
    week = c.get("/titolare/api/week?d=2026-11-16", headers=H).json()["days"]
    assert len(week) == 7 and week[0]["open"] is False  # Monday closed
    assert "Agenda" in c.get("/titolare").text
