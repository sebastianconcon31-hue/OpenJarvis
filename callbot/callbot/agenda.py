"""Where appointments are stored.

Two options with the same methods:
- GoogleCalendar: the real one. The owner sees every booking in the Google Calendar app.
- LocalCalendar: a small file on the server, for testing and the demo.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import httpx


ALL_BARBERS = "*"  # an event with this barber (e.g. an all-day "Ferie") blocks everyone


@dataclass
class Appointment:
    id: str
    start: datetime  # timezone-aware
    end: datetime
    service: str
    customer_name: str
    phone: str
    barber: str = ""
    reminded: bool = False


class Calendar(Protocol):
    def list(self, start: datetime, end: datetime) -> list[Appointment]: ...
    def create(self, appt: Appointment) -> Appointment: ...
    def delete(self, appt_id: str) -> None: ...
    def mark_reminded(self, appt_id: str) -> None: ...


class LocalCalendar:
    def __init__(self, path: str = ":memory:") -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS appointments (id TEXT PRIMARY KEY, start TEXT, end TEXT,"
            " service TEXT, customer_name TEXT, phone TEXT, barber TEXT, reminded INTEGER DEFAULT 0)"
        )
        self._db.commit()

    def list(self, start: datetime, end: datetime) -> list[Appointment]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id,start,end,service,customer_name,phone,barber,reminded FROM appointments"
            ).fetchall()
        out = []
        for r in rows:
            a = Appointment(r[0], datetime.fromisoformat(r[1]), datetime.fromisoformat(r[2]), r[3], r[4], r[5], r[6], bool(r[7]))
            if a.start < end and a.end > start:
                out.append(a)
        return sorted(out, key=lambda a: a.start)

    def create(self, appt: Appointment) -> Appointment:
        appt.id = appt.id or uuid.uuid4().hex[:12]
        with self._lock:
            self._db.execute(
                "INSERT INTO appointments VALUES (?,?,?,?,?,?,?,?)",
                (appt.id, appt.start.isoformat(), appt.end.isoformat(), appt.service,
                 appt.customer_name, appt.phone, appt.barber, int(appt.reminded)),
            )
            self._db.commit()
        return appt

    def delete(self, appt_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM appointments WHERE id=?", (appt_id,))
            self._db.commit()

    def mark_reminded(self, appt_id: str) -> None:
        with self._lock:
            self._db.execute("UPDATE appointments SET reminded=1 WHERE id=?", (appt_id,))
            self._db.commit()


class GoogleCalendar:
    """Talks to Google Calendar with a 'service account' (a robot Google user).

    Setup: create a service account in Google Cloud, download its JSON key, and share the
    salon's calendar with the service account's email ("Make changes to events").
    Booking details are kept in hidden event fields so the bot can read them back.
    """

    API = "https://www.googleapis.com/calendar/v3"

    def __init__(self, calendar_id: str, credentials_file: str) -> None:
        from google.oauth2 import service_account

        self.calendar_id = calendar_id
        self._creds = service_account.Credentials.from_service_account_file(
            credentials_file, scopes=["https://www.googleapis.com/auth/calendar"]
        )
        self._lock = threading.Lock()

    def _headers(self) -> dict:
        from google.auth.transport.requests import Request

        with self._lock:
            if not self._creds.valid:
                self._creds.refresh(Request())
            return {"Authorization": f"Bearer {self._creds.token}"}

    def _url(self, suffix: str = "") -> str:
        from urllib.parse import quote

        return f"{self.API}/calendars/{quote(self.calendar_id, safe='')}/events{suffix}"

    def list(self, start: datetime, end: datetime) -> list[Appointment]:
        out: list[Appointment] = []
        params = {
            "timeMin": start.astimezone(timezone.utc).isoformat(),
            "timeMax": end.astimezone(timezone.utc).isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "2500",
        }
        while True:
            r = httpx.get(self._url(), params=params, headers=self._headers(), timeout=20)
            r.raise_for_status()
            data = r.json()
            for ev in data.get("items", []):
                if ev.get("status") == "cancelled" or "dateTime" not in ev.get("start", {}):
                    # All-day events (e.g. "Ferie") block the whole day.
                    if ev.get("status") != "cancelled" and "date" in ev.get("start", {}):
                        out.append(_all_day_block(ev, start.tzinfo))
                    continue
                if ev.get("transparency") == "transparent":
                    continue  # marked "free" in Google Calendar: doesn't block bookings
                priv = (ev.get("extendedProperties") or {}).get("private") or {}
                out.append(
                    Appointment(
                        id=ev["id"],
                        start=datetime.fromisoformat(ev["start"]["dateTime"]),
                        end=datetime.fromisoformat(ev["end"]["dateTime"]),
                        service=priv.get("service", ev.get("summary", "")),
                        customer_name=priv.get("customer_name", ""),
                        phone=priv.get("phone", ""),
                        barber=priv.get("barber", ""),
                        reminded=priv.get("reminded") == "1",
                    )
                )
            if not data.get("nextPageToken"):
                break
            params["pageToken"] = data["nextPageToken"]
        return out

    def create(self, appt: Appointment) -> Appointment:
        summary = f"{appt.service} - {appt.customer_name}"
        if appt.barber:
            summary += f" ({appt.barber})"
        body = {
            "summary": summary,
            "description": f"Prenotato su WhatsApp\nTelefono: +{appt.phone}",
            "start": {"dateTime": appt.start.isoformat()},
            "end": {"dateTime": appt.end.isoformat()},
            "extendedProperties": {
                "private": {
                    "callbot": "1",
                    "service": appt.service,
                    "customer_name": appt.customer_name,
                    "phone": appt.phone,
                    "barber": appt.barber,
                    "reminded": "0",
                }
            },
        }
        r = httpx.post(self._url(), json=body, headers=self._headers(), timeout=20)
        r.raise_for_status()
        appt.id = r.json()["id"]
        return appt

    def delete(self, appt_id: str) -> None:
        r = httpx.delete(self._url(f"/{appt_id}"), headers=self._headers(), timeout=20)
        if r.status_code not in (200, 204, 404, 410):
            r.raise_for_status()

    def mark_reminded(self, appt_id: str) -> None:
        body = {"extendedProperties": {"private": {"reminded": "1"}}}
        r = httpx.patch(self._url(f"/{appt_id}"), json=body, headers=self._headers(), timeout=20)
        r.raise_for_status()


def _all_day_block(ev: dict, tz) -> Appointment:
    from datetime import date, time

    s = datetime.combine(date.fromisoformat(ev["start"]["date"]), time(0), tz)
    e = datetime.combine(date.fromisoformat(ev["end"]["date"]), time(0), tz)
    return Appointment(id=ev["id"], start=s, end=e, service=ev.get("summary", "Chiuso"),
                       customer_name="", phone="", barber=ALL_BARBERS, reminded=True)


def make_calendar(settings) -> Calendar:
    if settings.calendar_backend == "google":
        return GoogleCalendar(settings.google_calendar_id, settings.google_credentials_file)
    import os

    os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
    return LocalCalendar(settings.db_path)


__all__ = ["Appointment", "Calendar", "LocalCalendar", "GoogleCalendar", "make_calendar"]
