"""A web page that looks like a WhatsApp chat, for trying the bot without WhatsApp.

Turned on with DEMO_ENABLED=1. Open https://YOUR-ADDRESS/demo in a browser.
Bookings made here go into a separate practice calendar, never the real one.
Set DEMO_PASSWORD to stop strangers from using up the free AI quota.
"""

from __future__ import annotations

import hmac
from datetime import datetime, timedelta

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .agenda import LocalCalendar
from .booking import Booking
from .bot import GIORNI, Receptionist
from .store import Store


class ChatIn(BaseModel):
    session: str
    text: str
    password: str = ""


def fake_phone(session: str) -> str:
    """Each browser is a pretend customer with a fake phone number."""
    digits = "".join(c for c in session if c.isdigit())[:7]
    return "39000" + digits.zfill(7)


def make_demo_router(salon, settings, llm, password: str = "", faq=None) -> APIRouter:
    router = APIRouter()
    calendar = LocalCalendar()
    booking = Booking(salon, calendar)
    store = Store()
    outbox: dict[str, list[str]] = {}
    owner_alerts: list[str] = []

    def send(phone: str, text: str) -> None:
        outbox.setdefault(phone, []).append(text)

    def notify_owner(phone: str, name: str, reason: str) -> None:
        owner_alerts.append(f"{name or 'Cliente'} (+{phone}): {reason}")

    bot = Receptionist(salon, settings, llm, booking, store, send, notify_owner, faq)

    def allowed(pw: str) -> bool:
        return not password or hmac.compare_digest(pw.encode(), password.encode())

    @router.get("/demo", response_class=HTMLResponse)
    def page() -> str:
        return PAGE.replace("{{SALON}}", salon.name).replace(
            "{{NEEDS_PW}}", "true" if password else "false")

    @router.post("/demo/chat")
    def chat(body: ChatIn):
        if not allowed(body.password):
            return JSONResponse({"error": "password"}, status_code=401)
        phone = fake_phone(body.session)
        outbox[phone] = []
        bot.handle(phone, body.text[:1000], "Cliente Demo")
        replies = outbox.pop(phone, [])
        paused = store.is_paused(phone)
        return {"replies": replies, "handed_off": paused}

    @router.post("/demo/reset")
    def reset(body: ChatIn):
        if not allowed(body.password):
            return JSONResponse({"error": "password"}, status_code=401)
        phone = fake_phone(body.session)
        store.forget(phone)
        for a in booking.upcoming_for(phone):
            calendar.delete(a.id)
        return {"ok": True}

    @router.get("/demo/agenda")
    def agenda(password: str = ""):
        if not allowed(password):
            return JSONResponse({"error": "password"}, status_code=401)
        now = booking.now()
        items = calendar.list(now - timedelta(days=1), now + timedelta(days=salon.max_days_ahead + 1))
        return {
            "appointments": [
                {"when": f"{GIORNI[a.start.weekday()]} {a.start:%d/%m %H:%M}", "service": a.service,
                 "name": a.customer_name, "barber": a.barber}
                for a in items
            ],
            "owner_alerts": owner_alerts[-10:],
            "now": datetime.now(salon.tz).strftime("%H:%M"),
        }

    return router


PAGE = r"""<!doctype html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{SALON}} · Demo</title>
<style>
:root{--bg:#efeae2;--me:#d9fdd3;--bot:#fff;--bar:#008069;--text:#111b21;--muted:#667781;--panel:#fff}
@media (prefers-color-scheme: dark){:root{--bg:#0b141a;--me:#005c4b;--bot:#202c33;--bar:#202c33;--text:#e9edef;--muted:#8696a0;--panel:#111b21}}
*{box-sizing:border-box}body{margin:0;font:15px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--text);height:100vh;display:flex;flex-direction:column}
header{background:var(--bar);color:#fff;padding:12px 16px;display:flex;align-items:center;gap:12px}
header .av{width:38px;height:38px;border-radius:50%;background:#25d366;display:grid;place-items:center;font-weight:700}
header .t{flex:1}header .t b{display:block}header .t small{opacity:.8}header button{background:transparent;border:1px solid #fff8;color:#fff;border-radius:16px;padding:6px 10px;cursor:pointer}
#toggle{display:none}
main{flex:1;display:flex;min-height:0}
#chat{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:6px}
.m{max-width:78%;padding:7px 10px;border-radius:8px;white-space:pre-wrap;box-shadow:0 1px .5px #0002;word-wrap:break-word}
.me{align-self:flex-end;background:var(--me)}.bot{align-self:flex-start;background:var(--bot)}
.sys{align-self:center;font-size:12.5px;color:var(--muted);background:var(--panel);padding:4px 10px;border-radius:8px}
#side{width:300px;background:var(--panel);border-left:1px solid #8883;padding:14px;overflow-y:auto;font-size:14px}
#side h3{margin:4px 0 8px;font-size:14px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
#side .a{padding:8px 0;border-bottom:1px solid #8882}#side .empty{color:var(--muted)}
form{display:flex;gap:8px;padding:10px;background:var(--panel)}
input{flex:1;padding:11px 14px;border-radius:22px;border:1px solid #8884;background:var(--bg);color:var(--text);font:inherit}
form button{background:#00a884;color:#fff;border:0;border-radius:50%;width:44px;height:44px;font-size:18px;cursor:pointer}
.typing{color:var(--muted);font-style:italic}
@media (max-width:760px){#toggle{display:inline-block}#side{display:none}#side.show{display:block;position:fixed;inset:62px 0 64px 0;width:auto;z-index:2}}
</style></head><body>
<header><div class="av">AI</div><div class="t"><b>{{SALON}}</b><small>Demo: prova il receptionist</small></div>
<button id="toggle">Agenda</button><button id="reset">Ricomincia</button></header>
<main><div id="chat"><div class="sys">Questa è una prova. Le prenotazioni finiscono in un'agenda di prova, non in quella vera.</div></div>
<aside id="side"><h3>Agenda di prova</h3><div id="agenda"><div class="empty">Nessun appuntamento</div></div>
<h3 style="margin-top:18px">Avvisi al titolare</h3><div id="alerts"><div class="empty">Nessuno</div></div></aside></main>
<form id="f"><input id="i" placeholder="Scrivi un messaggio" autocomplete="off" autofocus><button>➤</button></form>
<script>
const NEEDS_PW={{NEEDS_PW}};
let pw="";try{pw=sessionStorage.getItem("pw")||""}catch(e){}
let session="";try{session=localStorage.getItem("s")||""}catch(e){}
if(!session){session=String(Math.floor(Math.random()*1e7));try{localStorage.setItem("s",session)}catch(e){}}
if(NEEDS_PW&&!pw){pw=prompt("Password della demo")||"";try{sessionStorage.setItem("pw",pw)}catch(e){}}
const chat=document.getElementById("chat"),inp=document.getElementById("i");
function add(text,cls){const d=document.createElement("div");d.className="m "+cls;d.textContent=text;chat.appendChild(d);chat.scrollTop=chat.scrollHeight;return d}
async function post(url,body){const r=await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});if(r.status===401){try{sessionStorage.removeItem("pw")}catch(e){}throw new Error("Password sbagliata, ricarica la pagina.")}return r.json()}
async function refresh(){try{const r=await fetch("/demo/agenda?password="+encodeURIComponent(pw));const d=await r.json();
const ag=document.getElementById("agenda");ag.innerHTML="";(d.appointments||[]).forEach(a=>{const e=document.createElement("div");e.className="a";e.textContent=a.when+" · "+a.service+" · "+a.name+(a.barber?" ("+a.barber+")":"");ag.appendChild(e)});if(!ag.children.length)ag.innerHTML='<div class="empty">Nessun appuntamento</div>';
const al=document.getElementById("alerts");al.innerHTML="";(d.owner_alerts||[]).forEach(a=>{const e=document.createElement("div");e.className="a";e.textContent=a;al.appendChild(e)});if(!al.children.length)al.innerHTML='<div class="empty">Nessuno</div>'}catch(e){}}
document.getElementById("f").onsubmit=async e=>{e.preventDefault();const t=inp.value.trim();if(!t)return;inp.value="";add(t,"me");const ty=add("sta scrivendo…","bot typing");
try{const d=await post("/demo/chat",{session,text:t,password:pw});ty.remove();(d.replies||[]).forEach(r=>add(r,"bot"));if(d.handed_off)add("Chat passata al titolare: il bot ora resta in silenzio. Premi Ricomincia per riprovare.","sys")}catch(err){ty.remove();add(err.message||"Errore","sys")}refresh()};
document.getElementById("reset").onclick=async()=>{try{await post("/demo/reset",{session,text:"",password:pw})}catch(e){}chat.querySelectorAll(".m").forEach(m=>m.remove());refresh()};
document.getElementById("toggle").onclick=()=>document.getElementById("side").classList.toggle("show");
refresh();
</script></body></html>"""
