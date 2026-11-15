"""The 150 common barber questions (faq.yaml), and a simple search the bot uses as a tool.

Search is plain word matching, no AI, so it's free, fast and predictable. The AI then
rewrites the matching answer in its own words and the right tone.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import Salon

GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
DAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Placeholders filled from salon.yaml itself (not from "facts").
AUTO = {"name", "address", "hours", "prices", "services", "barbers", "max_days", "min_notice", "closed_dates"}

STOPWORDS = set("""
il lo la i gli le un uno una di a da in con su per tra fra e o ma se che chi cosa come quando
quanto quanta quanti quante perche mi ti si ci vi ne non piu molto anche gia sono sei e
siamo siete ho hai ha abbiamo avete hanno fate fai fa faccio posso puoi puo possiamo potete
devo deve voglio vorrei vuoi del dello della dei degli delle al allo alla ai agli alle dal
dalla dai nel nella nei sul sulla mio mia miei mie tuo tua suo sua vostro vostra nostro questo
questa quello quella c ce qui li la lei voi noi io tu lui ok ciao salve buongiorno buonasera
the a an of to in on for with and or but is are am be do does did can could i you we my your me it
what how when who why there this that have has at by from
""".split())


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", text)


# Different words customers use for the same thing.
SYNONYMS = {"bimbi": "bambini", "bimbo": "bambino", "bimba": "bambina", "pupo": "bambino",
            "closing": "open", "close": "open", "chiudete": "aperti", "aprite": "aperti",
            "kids": "children", "child": "children", "bancomat": "carta", "pos": "carta",
            "satispay": "carta", "parcheggiare": "parcheggio", "park": "parking"}


def _tokens(text: str) -> set[str]:
    # Cutting words to 5 letters lets "prenotare/prenotazione/prenoto" match each other.
    words = [SYNONYMS.get(w, w) for w in _norm(text).split()]
    return {w[:5] for w in words if w not in STOPWORDS and len(w) > 1}


@dataclass
class Entry:
    id: str
    cat: str
    q_it: list[str]
    q_en: list[str]
    a_it: str
    a_en: str
    needs: list[str] = field(default_factory=list)
    handoff: bool = False
    _q_tokens: set[str] = field(default_factory=set, repr=False)
    _a_tokens: set[str] = field(default_factory=set, repr=False)


def load_faq(path: str | Path) -> list[Entry]:
    entries = []
    for raw in yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []:
        e = Entry(id=raw["id"], cat=raw.get("cat", ""), q_it=list(raw.get("q_it") or []),
                  q_en=list(raw.get("q_en") or []), a_it=raw["a_it"], a_en=raw["a_en"],
                  needs=list(raw.get("needs") or []), handoff=bool(raw.get("handoff", False)))
        e._q_tokens = _tokens(" ".join(e.q_it + e.q_en + [e.id.replace("_", " ")]))
        # Answers that are only a placeholder ("{kids}") have no words of their own.
        e._a_tokens = _tokens(re.sub(r"\{\w+\}", " ", e.a_it + " " + e.a_en)) or set(e._q_tokens)
        entries.append(e)
    return entries


def _auto_values(salon: Salon, lang: str) -> dict[str, str]:
    en = lang == "en"
    days = DAYS_EN if en else GIORNI
    hours = []
    for i, d in enumerate(days):
        rs = salon.hours[i]
        closed = "closed" if en else "chiuso"
        hours.append(f"{d} " + (", ".join(f"{a:%H:%M}-{b:%H:%M}" for a, b in rs) if rs else closed))
    from datetime import date

    upcoming = sorted(d for d in salon.closed_dates if d >= date.today())[:8]
    return {
        "name": salon.name,
        "address": salon.address,
        "hours": "; ".join(hours),
        "prices": "; ".join(f"{s.name} €{s.price:g}" for s in salon.services),
        "services": "; ".join(f"{s.name} (€{s.price:g}, ~{s.minutes} min)" for s in salon.services),
        "barbers": ", ".join(salon.barbers),
        "max_days": str(salon.max_days_ahead),
        "min_notice": str(salon.min_notice_minutes),
        "closed_dates": ", ".join(d.strftime("%d/%m/%Y") for d in upcoming) or ("none planned" if en else "nessuna in programma"),
    }


def _fact(salon: Salon, key: str, lang: str) -> str:
    v = salon.facts.get(key)
    if isinstance(v, dict):  # facts can be given per language: {it: ..., en: ...}
        v = v.get(lang) or v.get("it") or ""
    return str(v or "").strip()


def render(entry: Entry, salon: Salon, lang: str = "it") -> dict:
    """Fill in an answer. If a fact it needs is missing, say so instead of guessing."""
    missing = [k for k in entry.needs if not _fact(salon, k, lang)]
    result: dict = {"id": entry.id, "question": (entry.q_en if lang == "en" else entry.q_it)[:1]}
    if missing:
        result["answer"] = None
        result["missing_info"] = missing
        result["note"] = ("Questa informazione del salone non è disponibile. Non inventarla: di' al cliente che "
                          "chiedi al titolare e usa handoff_to_owner.")
        return result
    values = _auto_values(salon, lang)
    values.update({k: _fact(salon, k, lang) for k in entry.needs})
    template = entry.a_en if lang == "en" else entry.a_it
    result["answer"] = re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), template)
    if entry.handoff:
        result["note"] = "Per questa richiesta passa la chat al titolare con handoff_to_owner."
    return result


class FAQ:
    def __init__(self, entries: list[Entry], salon: Salon) -> None:
        self.entries = entries
        self.salon = salon
        # Rare words count more than common ones ("parcheggio" tells us more than "taglio").
        import math

        n = len(entries) or 1
        df: dict[str, int] = {}
        for e in entries:
            for t in e._q_tokens | e._a_tokens:
                df[t] = df.get(t, 0) + 1
        self.idf = {t: math.log(1 + n / c) for t, c in df.items()}

    def search(self, query: str, lang: str = "it", limit: int = 3) -> list[dict]:
        q = _tokens(query)
        if not q:
            return []
        scored = []
        for e in self.entries:
            score = sum(3 * self.idf.get(t, 0) for t in q & e._q_tokens)
            score += sum(self.idf.get(t, 0) for t in q & e._a_tokens)
            if score:
                scored.append((score, e))
        scored.sort(key=lambda x: -x[0])
        return [render(e, self.salon, lang) for _, e in scored[:limit]]
