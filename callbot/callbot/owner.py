"""The owner's page: today's bookings, how full each day is, and buttons to add a booking,
block time or cancel. Open https://YOUR-ADDRESS/titolare on a phone and "Add to home screen".

Turned on by setting OWNER_PASSWORD. Works with both calendars (local or Google).
"""

from __future__ import annotations

import hmac
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Header
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .agenda import ALL_BARBERS, Appointment
from .booking import Booking, BookingError
from . import changes

GIORNI = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]


class BookIn(BaseModel):
    date: str
    time: str
    service: str
    name: str
    phone: str = ""
    barber: str = ""


class BlockIn(BaseModel):
    date: str
    start: str
    end: str
    reason: str = "Bloccato"


class CancelIn(BaseModel):
    id: str
    notify: bool = True
    reason: str = ""


class MoveIn(BaseModel):
    id: str
    date: str
    time: str


class AnswerIn(BaseModel):
    id: str
    accept: bool
    reason: str = ""


class ModeIn(BaseModel):
    mode: str


def _open_minutes(booking: Booking, d: date) -> int:
    if not booking.is_open_day(d):
        return 0
    return sum((datetime.combine(d, b) - datetime.combine(d, a)).seconds // 60
               for a, b in booking.salon.hours[d.weekday()])


def day_summary(booking: Booking, d: date, store=None) -> dict:
    """Bookings, free time and how full the day is (0-100%)."""
    salon = booking.salon
    tz = salon.tz
    start = datetime.combine(d, datetime.min.time(), tz)
    events = booking.cal.list(start, start + timedelta(days=1))
    chairs = max(1, len(salon.barbers))
    capacity = _open_minutes(booking, d) * chairs
    busy = 0
    for a, b in salon.hours[d.weekday()] if booking.is_open_day(d) else []:
        o1, o2 = datetime.combine(d, a, tz), datetime.combine(d, b, tz)
        for e in events:
            overlap = (min(e.end, o2) - max(e.start, o1)).total_seconds() / 60
            if overlap > 0:
                busy += overlap * (chairs if e.barber == ALL_BARBERS else 1)
    full = 100 if capacity and busy >= capacity else round(100 * busy / capacity) if capacity else 0

    # Free time, shown as ranges ("09:00-10:30"): any moment at least one chair is free.
    free_ranges: list[str] = []
    if capacity:
        now = booking.now()
        tick = timedelta(minutes=5)
        for a, b in salon.hours[d.weekday()]:
            t, close = datetime.combine(d, a, tz), datetime.combine(d, b, tz)
            run = None
            while t < close:
                free = t >= now - tick and booking._free_barber(t, t + tick, list(salon.barbers), events)
                if free and run is None:
                    run = t
                if not free and run is not None:
                    if t - run >= timedelta(minutes=15):
                        free_ranges.append(f"{run:%H:%M}-{t:%H:%M}")
                    run = None
                t += tick
            if run is not None and close - run >= timedelta(minutes=15):
                free_ranges.append(f"{run:%H:%M}-{close:%H:%M}")

    requests = {r["id"] for r in changes.pending_requests(store)} if store else set()
    moving_from, moving_to = set(), set()
    if store:
        for p in changes.all_proposals(store):
            moving_from.add(p["old_id"])
            moving_to.add(p["new_id"])
    items = []
    for e in sorted(events, key=lambda e: e.start):
        blocked = not e.phone and not e.customer_name
        items.append({
            "id": e.id,
            "start": e.start.astimezone(tz).strftime("%H:%M"),
            "end": e.end.astimezone(tz).strftime("%H:%M"),
            "service": e.service,
            "name": e.customer_name,
            "phone": e.phone,
            "barber": "" if e.barber == ALL_BARBERS or chairs == 1 else e.barber,
            "blocked": blocked,
            "all_day": e.barber == ALL_BARBERS and (e.end - e.start) >= timedelta(hours=23),
            "status": ("request" if e.id in requests else "moving_from" if e.id in moving_from
                       else "moving_to" if e.id in moving_to else ""),
        })
    hours = salon.hours[d.weekday()] if booking.is_open_day(d) else []
    return {
        "date": d.isoformat(),
        "label": f"{GIORNI[d.weekday()]} {d.day}/{d.month}",
        "open": bool(hours),
        "hours": ", ".join(f"{a:%H:%M}-{b:%H:%M}" for a, b in hours),
        "full": full,
        "count": sum(1 for i in items if not i["blocked"]),
        "items": items,
        "free": free_ranges,
    }


def make_owner_router(booking: Booking, password: str, store=None, message=None) -> APIRouter:
    """store: the bot's memory (for modes, requests, answers). message(phone, text, params) -> bool
    sends a WhatsApp message to a customer."""
    router = APIRouter()
    salon = booking.salon
    message = message or (lambda *a: False)

    def ok(pw: str | None) -> bool:
        return bool(password) and hmac.compare_digest((pw or "").encode(), password.encode())

    def denied():
        return JSONResponse({"error": "password"}, status_code=401)

    @router.get("/titolare", response_class=HTMLResponse)
    def page() -> str:
        services = "".join(f'<option>{s.name}</option>' for s in salon.services)
        return PAGE.replace("{{SALON}}", salon.name).replace("{{SERVICES}}", services)

    @router.get("/titolare/api/day")
    def day(d: str = "", x_owner_password: str | None = Header(default=None)):
        if not ok(x_owner_password):
            return denied()
        when = date.fromisoformat(d) if d else booking.now().date()
        out = day_summary(booking, when, store)
        if store:
            out["mode"] = changes.mode(store)
            out["requests"] = sorted(changes.pending_requests(store), key=lambda r: r["t"])
            out["alerts"] = list(reversed(changes.owner_alerts(store)))[:5]
        return out

    @router.get("/titolare/api/week")
    def week(d: str = "", x_owner_password: str | None = Header(default=None)):
        if not ok(x_owner_password):
            return denied()
        first = date.fromisoformat(d) if d else booking.now().date()
        out = []
        for i in range(7):
            s = day_summary(booking, first + timedelta(days=i), store)
            out.append({k: s[k] for k in ("date", "label", "open", "full", "count")})
        return {"days": out}

    @router.post("/titolare/api/book")
    def book(body: BookIn, x_owner_password: str | None = Header(default=None)):
        if not ok(x_owner_password):
            return denied()
        start = datetime.fromisoformat(f"{body.date}T{body.time}").replace(tzinfo=salon.tz)
        phone = "".join(c for c in body.phone if c.isdigit())
        try:
            # The owner can book someone in right now (e.g. a walk-in), ignoring the notice rule.
            a = booking.book(body.service, start, body.name or "Cliente", phone, body.barber or None,
                             ignore_notice=True)
        except (BookingError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if not phone:
            booking.cal.mark_reminded(a.id)  # no number, no reminder
        return {"ok": True, "id": a.id}

    @router.post("/titolare/api/block")
    def block(body: BlockIn, x_owner_password: str | None = Header(default=None)):
        if not ok(x_owner_password):
            return denied()
        s = datetime.fromisoformat(f"{body.date}T{body.start}").replace(tzinfo=salon.tz)
        e = datetime.fromisoformat(f"{body.date}T{body.end}").replace(tzinfo=salon.tz)
        if e <= s:
            return JSONResponse({"error": "L'ora di fine deve essere dopo l'inizio."}, status_code=400)
        a = booking.cal.create(Appointment("", s, e, body.reason or "Bloccato", "", "", ALL_BARBERS, True))
        return {"ok": True, "id": a.id}

    def find(appt_id: str) -> Appointment | None:
        now = booking.now()
        return next((a for a in booking.cal.list(now - timedelta(days=2), now + timedelta(days=400))
                     if a.id == appt_id), None)

    @router.post("/titolare/api/cancel")
    def cancel(body: CancelIn, x_owner_password: str | None = Header(default=None)):
        if not ok(x_owner_password):
            return denied()
        appt = find(body.id)
        told = False
        if appt and store is not None:
            store.set(f"request:{appt.id}", "")
            if body.notify and appt.phone and appt.start > booking.now():
                told = changes.notify_cancel(booking, store, message, appt, salon.name, body.reason)
        booking.cal.delete(body.id)
        return {"ok": True, "customer_told": told}

    @router.post("/titolare/api/move")
    def move(body: MoveIn, x_owner_password: str | None = Header(default=None)):
        if not ok(x_owner_password):
            return denied()
        if store is None:
            return JSONResponse({"error": "Non disponibile."}, status_code=400)
        try:
            new_start = datetime.fromisoformat(f"{body.date}T{body.time}").replace(tzinfo=salon.tz)
            p = changes.propose_move(booking, store, message, body.id, new_start, salon.name)
        except (BookingError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"ok": True, "asked": p["new_when"]}

    @router.post("/titolare/api/answer")
    def answer(body: AnswerIn, x_owner_password: str | None = Header(default=None)):
        if not ok(x_owner_password):
            return denied()
        try:
            return changes.answer_request(booking, store, message, body.id, body.accept, salon.name, body.reason)
        except BookingError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @router.post("/titolare/api/mode")
    def set_mode(body: ModeIn, x_owner_password: str | None = Header(default=None)):
        if not ok(x_owner_password):
            return denied()
        if store is None:
            return JSONResponse({"error": "Non disponibile."}, status_code=400)
        changes.set_mode(store, body.mode)
        return {"ok": True, "mode": changes.mode(store)}

    return router


PAGE = r"""<!doctype html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>{{SALON}} · Agenda</title>
<style>
:root{--bg:#f5f4f0;--card:#fff;--text:#1d1d1b;--muted:#6f6e69;--line:#e4e2dc;--accent:#0f7b5f;--accent-soft:#dff3ec;--warn:#c2410c;--warn-soft:#fdeadf;--block:#ecebe6}
@media (prefers-color-scheme: dark){:root{--bg:#121211;--card:#1d1d1b;--text:#f1f0ec;--muted:#a3a29c;--line:#2e2e2b;--accent:#3cc59a;--accent-soft:#163a2f;--warn:#fb923c;--warn-soft:#3b2416;--block:#2a2a27}}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--text)}
.wrap{max-width:640px;margin:0 auto;padding:16px 16px 96px}
header{display:flex;align-items:center;gap:8px;margin-bottom:12px}
header h1{font-size:18px;margin:0;flex:1}
button{font:inherit;cursor:pointer}
.nav{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:10px;width:40px;height:40px;font-size:18px}
.today-btn{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:10px;height:40px;padding:0 12px}
.week{display:grid;grid-template-columns:repeat(7,1fr);gap:6px;margin-bottom:14px}
.wd{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px 2px;text-align:center;font-size:12px;color:var(--muted)}
.wd b{display:block;color:var(--text);font-size:14px}.wd.sel{outline:2px solid var(--accent)}
.wd .bar{height:4px;border-radius:2px;background:var(--line);margin:5px 6px 0;overflow:hidden}.wd .bar i{display:block;height:100%;background:var(--accent)}
.wd.closed{opacity:.5}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:12px}
.big{display:flex;align-items:baseline;gap:10px}.big .pct{font-size:34px;font-weight:700}
.meter{height:10px;border-radius:5px;background:var(--line);overflow:hidden;margin:8px 0 4px}.meter i{display:block;height:100%;background:var(--accent);transition:width .3s}
.meter.hot i{background:var(--warn)}
.muted{color:var(--muted);font-size:13px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}.chip{background:var(--accent-soft);color:var(--accent);border-radius:999px;padding:3px 10px;font-size:13px;font-weight:600}
.item{display:flex;gap:12px;padding:10px 0;border-top:1px solid var(--line);align-items:flex-start}.item:first-child{border-top:0}
.item .t{font-weight:700;min-width:92px;font-variant-numeric:tabular-nums}
.item .who{flex:1}.item .who b{display:block}
.item.blocked .who b{color:var(--muted)}.item.blocked{background:var(--block);margin:0 -14px;padding:10px 14px}
.item a{color:var(--accent);text-decoration:none;font-size:13px}
.x{border:0;background:transparent;color:var(--muted);font-size:18px;padding:0 4px}
.empty{color:var(--muted);text-align:center;padding:18px 0}
.fab{position:fixed;left:0;right:0;bottom:0;display:flex;gap:8px;padding:12px 16px calc(12px + env(safe-area-inset-bottom));background:linear-gradient(transparent,var(--bg) 30%);max-width:640px;margin:0 auto}
.fab button{flex:1;height:48px;border-radius:12px;border:0;font-weight:600}
.primary{background:var(--accent);color:#fff}.secondary{background:var(--card);color:var(--text);border:1px solid var(--line)!important}
dialog{border:0;border-radius:16px;padding:18px;width:min(92vw,420px);background:var(--card);color:var(--text)}
dialog::backdrop{background:#0006}
dialog h2{margin:0 0 12px;font-size:17px}
label{display:block;font-size:13px;color:var(--muted);margin:10px 0 4px}
input,select{width:100%;height:44px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--text);padding:0 10px;font:inherit}
.row{display:flex;gap:8px}.row>div{flex:1}
.actions{display:flex;gap:8px;margin-top:16px}.actions button{flex:1;height:44px;border-radius:10px;border:0;font-weight:600}
.err{color:var(--warn);font-size:13px;margin-top:8px;min-height:1em}
.seg{display:flex;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:3px;gap:3px}
.seg button{flex:1;border:0;background:transparent;color:var(--muted);height:38px;border-radius:9px;font-weight:600}
.seg button.on{background:var(--accent);color:#fff}
.modehint{font-size:12.5px;color:var(--muted);margin:6px 2px 12px}
.alert{background:var(--accent-soft);border-radius:10px;padding:8px 12px;font-size:13.5px;margin-bottom:6px}
.req{display:flex;gap:10px;align-items:center;padding:10px 0;border-top:1px solid var(--line)}.req:first-of-type{border-top:0}
.req .who{flex:1}.req .who b{display:block}
.ok,.no{border:0;border-radius:10px;height:38px;padding:0 12px;font-weight:600}.ok{background:var(--accent);color:#fff}.no{background:var(--warn-soft);color:var(--warn)}
.badge{display:inline-block;font-size:11.5px;font-weight:700;border-radius:999px;padding:1px 8px;margin-top:4px}
.b-req{background:var(--warn-soft);color:var(--warn)}.b-move{background:var(--accent-soft);color:var(--accent)}
.item.faded{opacity:.55}
.mini{border:1px solid var(--line);background:transparent;color:var(--text);border-radius:8px;height:30px;padding:0 10px;font-size:13px}
.check{display:flex;gap:8px;align-items:center;margin-top:12px;font-size:14px}.check input{width:20px;height:20px}
h3.sec{font-size:14px;margin:0 0 4px}
</style></head><body><div class="wrap">
<header><button class="nav" id="prev" aria-label="Giorno prima">‹</button><h1 id="title">Agenda</h1>
<button class="today-btn" id="today">Oggi</button><button class="nav" id="next" aria-label="Giorno dopo">›</button></header>
<div class="seg" id="seg"><button data-m="auto">⚡ Automatico</button><button data-m="approve">✋ Da approvare</button></div>
<div class="modehint" id="modehint"></div>
<div id="alerts"></div>
<div class="card" id="reqs" style="display:none"></div>
<div class="week" id="week"></div>
<div class="card"><div class="big"><span class="pct" id="pct">–</span><span id="pctlabel" class="muted">pieno</span></div>
<div class="meter" id="meter"><i id="meterfill" style="width:0"></i></div>
<div class="muted" id="hours"></div>
<div style="margin-top:10px"><span class="muted">Liberi:</span><div class="chips" id="free"></div></div></div>
<div class="card" id="list"><div class="empty">Caricamento…</div></div>
</div>
<div class="fab"><button class="primary" id="addBtn">+ Prenotazione</button><button class="secondary" id="blockBtn">Blocca orario</button></div>

<dialog id="addDlg"><h2>Nuova prenotazione</h2>
<label>Servizio</label><select id="aService">{{SERVICES}}</select>
<div class="row"><div><label>Giorno</label><input type="date" id="aDate"></div><div><label>Ora</label><input type="time" id="aTime" step="900"></div></div>
<label>Nome cliente</label><input id="aName" autocomplete="off">
<label>Telefono (per il promemoria, facoltativo)</label><input id="aPhone" inputmode="tel" placeholder="39 333 1234567">
<div class="err" id="aErr"></div>
<div class="actions"><button class="secondary" data-close>Annulla</button><button class="primary" id="aSave">Salva</button></div></dialog>

<dialog id="blockDlg"><h2>Blocca un orario</h2>
<div class="muted">Il bot non prenoterà nessuno in questo orario.</div>
<label>Giorno</label><input type="date" id="bDate">
<div class="row"><div><label>Dalle</label><input type="time" id="bStart" step="900"></div><div><label>Alle</label><input type="time" id="bEnd" step="900"></div></div>
<label>Motivo (facoltativo)</label><input id="bReason" placeholder="Pausa, visita, ferie…">
<div class="err" id="bErr"></div>
<div class="actions"><button class="secondary" data-close>Annulla</button><button class="primary" id="bSave">Blocca</button></div></dialog>

<dialog id="moveDlg"><h2>Sposta appuntamento</h2>
<div class="muted" id="mWho"></div>
<div class="row"><div><label>Nuovo giorno</label><input type="date" id="mDate"></div><div><label>Nuova ora</label><input type="time" id="mTime" step="900"></div></div>
<div class="muted" style="margin-top:10px">Il bot scrive al cliente e gli chiede se il nuovo orario va bene. Il vecchio resta finché non risponde.</div>
<div class="err" id="mErr"></div>
<div class="actions"><button class="secondary" data-close>Annulla</button><button class="primary" id="mSave">Chiedi al cliente</button></div></dialog>

<dialog id="cancelDlg"><h2>Cancella appuntamento</h2>
<div class="muted" id="cWho"></div>
<label>Motivo (facoltativo, lo vede il cliente)</label><input id="cReason" placeholder="Es. imprevisto">
<label class="check" id="cNotifyRow"><input type="checkbox" id="cNotify" checked> Avvisa il cliente su WhatsApp</label>
<div class="err" id="cErr"></div>
<div class="actions"><button class="secondary" data-close>Indietro</button><button class="no" id="cSave" style="flex:1;height:44px">Cancella</button></div></dialog>

<script>
let pw="";try{pw=localStorage.getItem("ownerpw")||""}catch(e){}
let day=null,todayIso=null,items=[],mode="auto",current=null;
const $=id=>document.getElementById(id);
function iso(d){return d.toISOString().slice(0,10)}
function shift(s,n){const d=new Date(s+"T12:00:00");d.setDate(d.getDate()+n);return iso(d)}
async function api(path,body){
  const r=await fetch("/titolare/api/"+path,{method:body?"POST":"GET",headers:{"Content-Type":"application/json","X-Owner-Password":pw},body:body?JSON.stringify(body):undefined});
  if(r.status===401){pw=prompt("Password dell'agenda")||"";try{localStorage.setItem("ownerpw",pw)}catch(e){}return api(path,body)}
  const d=await r.json();if(!r.ok)throw new Error(d.error||"Errore");return d}
function esc(s){const d=document.createElement("div");d.textContent=s||"";return d.innerHTML}
async function load(d){
  const s=await api("day"+(d?"?d="+d:""));day=s.date;
  $("title").textContent=s.label;
  $("pct").textContent=s.open?s.full+"%":"Chiuso";
  $("pctlabel").textContent=s.open?"pieno · "+s.count+" prenotazion"+(s.count===1?"e":"i"):"";
  $("meterfill").style.width=(s.open?s.full:0)+"%";$("meter").classList.toggle("hot",s.full>=85);
  $("hours").textContent=s.open?"Orario: "+s.hours:"Il salone è chiuso questo giorno.";
  $("free").innerHTML=s.free.length?s.free.map(f=>'<span class="chip">'+f+"</span>").join(""):'<span class="muted">'+(s.open?"nessun orario libero":"–")+"</span>";
  items=s.items;
  const badge={request:'<span class="badge b-req">✋ Da approvare</span>',moving_to:'<span class="badge b-move">⏳ Proposto, in attesa del cliente</span>',moving_from:'<span class="badge b-move">↪ In spostamento</span>'};
  $("list").innerHTML=s.items.length?s.items.map(i=>'<div class="item'+(i.blocked?" blocked":"")+(i.status==="moving_from"?" faded":"")+'"><div class="t">'+(i.all_day?"Tutto il giorno":i.start+"–"+i.end)+'</div><div class="who"><b>'+esc(i.blocked?("🔒 "+i.service):i.name)+"</b>"+(i.blocked?"":'<span class="muted">'+esc(i.service)+(i.barber?" · "+esc(i.barber):"")+"</span>")+(i.phone?'<br><a href="https://wa.me/'+i.phone+'">WhatsApp +'+i.phone+"</a>":"")+(badge[i.status]?"<br>"+badge[i.status]:"")+(!i.blocked&&i.phone&&!i.status?'<div style="margin-top:6px"><button class="mini" data-move="'+i.id+'">Sposta</button></div>':"")+'</div><button class="x" data-id="'+i.id+'" aria-label="Cancella">✕</button></div>').join(""):'<div class="empty">Nessuna prenotazione</div>';
  mode=s.mode||"auto";document.querySelectorAll("#seg button").forEach(b=>b.classList.toggle("on",b.dataset.m===mode));
  $("modehint").textContent=mode==="auto"?"Il bot conferma subito le prenotazioni negli orari liberi.":"Il bot tiene l'orario e aspetta che tu accetti o rifiuti ogni richiesta.";
  $("alerts").innerHTML=(s.alerts||[]).map(a=>'<div class="alert">'+esc(a.text)+"</div>").join("");
  const rq=s.requests||[];$("reqs").style.display=rq.length?"":"none";
  $("reqs").innerHTML='<h3 class="sec">Richieste da approvare ('+rq.length+")</h3>"+rq.map(r=>'<div class="req"><div class="who"><b>'+esc(r.name)+'</b><span class="muted">'+esc(r.service)+" · "+esc(r.when)+'</span></div><button class="no" data-ans="0" data-id="'+r.id+'">Rifiuta</button><button class="ok" data-ans="1" data-id="'+r.id+'">Accetta</button></div>').join("");
  if(!d)todayIso=s.date;
  if(!todayIso)todayIso=(await api("day")).date;
  // The strip shows the next 7 days from today, or from the chosen day if it's further out.
  const inWindow=s.date>=todayIso&&s.date<=shift(todayIso,6);
  const w=await api("week?d="+(inWindow?todayIso:s.date));
  $("week").innerHTML=w.days.map(x=>'<div class="wd'+(x.date===s.date?" sel":"")+(x.open?"":" closed")+'" data-d="'+x.date+'">'+x.label.slice(0,3)+"<b>"+x.label.split(" ")[1].split("/")[0]+'</b><div class="bar"><i style="width:'+(x.open?x.full:0)+'%"></i></div></div>').join("");
}
$("week").onclick=e=>{const el=e.target.closest(".wd");if(el)load(el.dataset.d)};
$("prev").onclick=()=>load(shift(day,-1));$("next").onclick=()=>load(shift(day,1));$("today").onclick=()=>load("");
$("list").onclick=e=>{
  const mv=e.target.closest("[data-move]");const x=e.target.closest(".x");
  if(mv){current=items.find(i=>i.id===mv.dataset.move);$("mWho").textContent=current.name+" · "+current.service+" · ora alle "+current.start;$("mDate").value=day;$("mTime").value="";$("mErr").textContent="";$("moveDlg").showModal()}
  if(x){current=items.find(i=>i.id===x.dataset.id);$("cWho").textContent=(current.blocked?current.service:current.name+" · "+current.service)+" · "+current.start+"–"+current.end;$("cNotifyRow").style.display=current.phone?"":"none";$("cReason").value="";$("cErr").textContent="";$("cancelDlg").showModal()}};
$("mSave").onclick=async()=>{try{await api("move",{id:current.id,date:$("mDate").value,time:$("mTime").value});$("moveDlg").close();load(day)}catch(err){$("mErr").textContent=err.message}};
$("cSave").onclick=async()=>{try{await api("cancel",{id:current.id,notify:$("cNotify").checked,reason:$("cReason").value});$("cancelDlg").close();load(day)}catch(err){$("cErr").textContent=err.message}};
$("seg").onclick=async e=>{const b=e.target.closest("button");if(!b)return;await api("mode",{mode:b.dataset.m});load(day)};
$("reqs").onclick=async e=>{const b=e.target.closest("[data-ans]");if(!b)return;let reason="";try{await api("answer",{id:b.dataset.id,accept:b.dataset.ans==="1",reason});load(day)}catch(err){$("reqs").insertAdjacentHTML("beforeend",'<div class="err">'+esc(err.message)+"</div>")}};
document.querySelectorAll("[data-close]").forEach(b=>b.onclick=()=>b.closest("dialog").close());
$("addBtn").onclick=()=>{$("aDate").value=day;$("aErr").textContent="";$("addDlg").showModal()};
$("blockBtn").onclick=()=>{$("bDate").value=day;$("bErr").textContent="";$("blockDlg").showModal()};
$("aSave").onclick=async()=>{try{await api("book",{date:$("aDate").value,time:$("aTime").value,service:$("aService").value,name:$("aName").value,phone:$("aPhone").value});$("addDlg").close();$("aName").value=$("aPhone").value="";load($("aDate").value)}catch(err){$("aErr").textContent=err.message}};
$("bSave").onclick=async()=>{try{await api("block",{date:$("bDate").value,start:$("bStart").value,end:$("bEnd").value,reason:$("bReason").value});$("blockDlg").close();load($("bDate").value)}catch(err){$("bErr").textContent=err.message}};
load("");setInterval(()=>{if(!document.querySelector("dialog[open]"))load(day)},60000);
</script></body></html>"""
