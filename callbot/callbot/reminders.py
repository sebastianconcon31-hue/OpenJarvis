"""Sends a WhatsApp reminder the day before each appointment."""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from .agenda import Calendar
from .config import Salon, Settings

log = logging.getLogger(__name__)


def send_due_reminders(salon: Salon, settings: Settings, calendar: Calendar, send_template,
                       now: datetime | None = None) -> int:
    """After REMINDER_HOUR, remind everyone booked for tomorrow who hasn't been reminded yet.
    Safe to run as often as you like. Returns how many reminders were sent."""
    now = now or datetime.now(salon.tz)
    if now.hour < settings.reminder_hour:
        return 0
    tomorrow = now.date() + timedelta(days=1)
    start = datetime.combine(tomorrow, time(0), salon.tz)
    sent = 0
    for a in calendar.list(start, start + timedelta(days=1)):
        if a.reminded or not a.phone:
            continue
        first_name = (a.customer_name or "").split(" ")[0] or "cliente"
        ok = send_template(
            a.phone,
            settings.reminder_template,
            settings.reminder_template_lang,
            [first_name, a.service, a.start.astimezone(salon.tz).strftime("%H:%M"), salon.name],
        )
        if ok:
            calendar.mark_reminded(a.id)
            sent += 1
        else:
            log.error("Reminder failed for appointment %s", a.id)
    return sent
