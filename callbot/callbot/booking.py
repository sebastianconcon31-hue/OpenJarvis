"""Booking rules: which times are free, booking, cancelling. Plain code, no AI involved,
so the bot can never invent a free slot or double-book."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .agenda import ALL_BARBERS, Appointment, Calendar
from .config import Salon


class BookingError(Exception):
    """A booking was refused. The message is shown to the AI so it can explain."""


class Booking:
    def __init__(self, salon: Salon, calendar: Calendar, now=None) -> None:
        self.salon = salon
        self.cal = calendar
        self._now = now  # tests can freeze time

    def now(self) -> datetime:
        return self._now() if self._now else datetime.now(self.salon.tz)

    # -- helpers -------------------------------------------------------------

    def is_open_day(self, d: date) -> bool:
        return d not in self.salon.closed_dates and bool(self.salon.hours[d.weekday()])

    def _barbers_for(self, barber: str | None) -> list[str]:
        if not barber:
            return list(self.salon.barbers)
        for b in self.salon.barbers:
            if b.lower() == barber.strip().lower():
                return [b]
        raise BookingError(
            f"'{barber}' non lavora qui. Barbieri disponibili: {', '.join(self.salon.barbers)}"
        )

    def _free_barber(self, start: datetime, end: datetime, candidates: list[str],
                     events: list[Appointment]) -> str | None:
        """Return a barber free for [start, end), or None."""
        overlapping = [e for e in events if e.start < end and e.end > start]
        if any(e.barber == ALL_BARBERS for e in overlapping):
            return None
        busy_named = {e.barber for e in overlapping if e.barber in self.salon.barbers}
        unnamed = sum(1 for e in overlapping if e.barber not in self.salon.barbers)
        free = [b for b in self.salon.barbers if b not in busy_named]
        # Events without a barber (e.g. added by hand) take up one chair each.
        if len(free) - unnamed <= 0:
            return None
        free_candidates = [b for b in candidates if b in free]
        if not free_candidates:
            return None
        return free_candidates[0]

    def _check_window(self, start: datetime, ignore_notice: bool = False) -> None:
        now = self.now()
        if ignore_notice:
            # The owner can book right away (a walk-in), just not in the past.
            if start < now - timedelta(minutes=30):
                raise BookingError("Quell'orario è già passato.")
        elif start < now + timedelta(minutes=self.salon.min_notice_minutes):
            raise BookingError(
                f"Serve almeno {self.salon.min_notice_minutes} minuti di preavviso."
            )
        if start.date() > (now + timedelta(days=self.salon.max_days_ahead)).date():
            raise BookingError(
                f"Si può prenotare al massimo {self.salon.max_days_ahead} giorni in anticipo."
            )

    def _fits_hours(self, start: datetime, end: datetime) -> bool:
        if not self.is_open_day(start.date()):
            return False
        for a, b in self.salon.hours[start.weekday()]:
            if start.time() >= a and end.time() <= b and end.date() == start.date():
                return True
        return False

    # -- public --------------------------------------------------------------

    def free_slots(self, service_name: str, day: date, barber: str | None = None,
                   limit: int = 40, ignore_notice: bool = False) -> list[datetime]:
        service = self.salon.service(service_name)
        if not service:
            raise BookingError(
                f"Servizio sconosciuto '{service_name}'. Servizi: "
                + ", ".join(s.name for s in self.salon.services)
            )
        candidates = self._barbers_for(barber)
        if not self.is_open_day(day):
            return []
        if day > (self.now() + timedelta(days=self.salon.max_days_ahead)).date():
            return []
        tz = self.salon.tz
        day_start = datetime.combine(day, datetime.min.time(), tz)
        events = self.cal.list(day_start, day_start + timedelta(days=1))
        dur = timedelta(minutes=service.minutes)
        step = timedelta(minutes=self.salon.slot_step_minutes)
        earliest = self.now() + timedelta(minutes=0 if ignore_notice else self.salon.min_notice_minutes)
        slots: list[datetime] = []
        for a, b in self.salon.hours[day.weekday()]:
            t = datetime.combine(day, a, tz)
            close = datetime.combine(day, b, tz)
            while t + dur <= close:
                if t >= earliest and self._free_barber(t, t + dur, candidates, events):
                    slots.append(t)
                t += step
        return slots[:limit]

    def book(self, service_name: str, start: datetime, customer_name: str, phone: str,
             barber: str | None = None, ignore_notice: bool = False) -> Appointment:
        service = self.salon.service(service_name)
        if not service:
            raise BookingError(f"Servizio sconosciuto '{service_name}'.")
        if start.tzinfo is None:
            start = start.replace(tzinfo=self.salon.tz)
        end = start + timedelta(minutes=service.minutes)
        self._check_window(start, ignore_notice)
        if not self._fits_hours(start, end):
            raise BookingError("Il negozio è chiuso in quell'orario.")
        if start.minute % self.salon.slot_step_minutes:
            raise BookingError(
                f"Gli appuntamenti iniziano ogni {self.salon.slot_step_minutes} minuti."
            )
        candidates = self._barbers_for(barber)
        events = self.cal.list(start - timedelta(hours=12), end + timedelta(hours=12))
        chosen = self._free_barber(start, end, candidates, events)
        if not chosen:
            raise BookingError("Quell'orario non è più libero.")
        if len(self.salon.barbers) == 1:
            chosen = self.salon.barbers[0]
        return self.cal.create(
            Appointment(id="", start=start, end=end, service=service.name,
                        customer_name=customer_name.strip(), phone=phone, barber=chosen)
        )

    def upcoming_for(self, phone: str) -> list[Appointment]:
        now = self.now()
        events = self.cal.list(now, now + timedelta(days=self.salon.max_days_ahead + 1))
        return [e for e in events if e.phone == phone and e.start > now]

    def cancel(self, phone: str, appt_id: str) -> Appointment:
        for a in self.upcoming_for(phone):
            if a.id == appt_id:
                self.cal.delete(a.id)
                return a
        raise BookingError("Appuntamento non trovato tra quelli di questo numero.")
