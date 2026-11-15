"""Sends and receives messages through Meta's official WhatsApp Cloud API."""

from __future__ import annotations

import hashlib
import hmac
import logging

import httpx

log = logging.getLogger(__name__)


class WhatsApp:
    def __init__(self, token: str, phone_number_id: str, api_version: str = "v21.0") -> None:
        self.token = token
        self.phone_number_id = phone_number_id
        self.base = f"https://graph.facebook.com/{api_version}"

    def _post(self, payload: dict) -> bool:
        r = httpx.post(
            f"{self.base}/{self.phone_number_id}/messages",
            json={"messaging_product": "whatsapp", **payload},
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=20,
        )
        if r.status_code >= 400:
            log.error("WhatsApp send failed %s: %s", r.status_code, r.text[:500])
            return False
        return True

    def send_text(self, to: str, text: str) -> bool:
        # WhatsApp's limit is 4096 characters per message.
        ok = True
        for i in range(0, len(text), 4000):
            ok &= self._post({"to": to, "type": "text", "text": {"body": text[i:i + 4000]}})
        return ok

    def send_template(self, to: str, name: str, lang: str, params: list[str]) -> bool:
        components = []
        if params:
            components = [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}]
        return self._post({
            "to": to,
            "type": "template",
            "template": {"name": name, "language": {"code": lang}, "components": components},
        })

    def mark_read(self, message_id: str) -> None:
        try:
            self._post({"status": "read", "message_id": message_id})
        except httpx.HTTPError:
            pass

    def download_media(self, media_id: str) -> tuple[bytes, str]:
        h = {"Authorization": f"Bearer {self.token}"}
        meta = httpx.get(f"{self.base}/{media_id}", headers=h, timeout=20)
        meta.raise_for_status()
        info = meta.json()
        data = httpx.get(info["url"], headers=h, timeout=60)
        data.raise_for_status()
        return data.content, info.get("mime_type", "audio/ogg")


def valid_signature(app_secret: str, body: bytes, header: str) -> bool:
    """Checks that a webhook really came from Meta."""
    if not app_secret or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:])


def parse_webhook(payload: dict) -> list[dict]:
    """Turn Meta's nested webhook JSON into a simple list of events.

    Each event is one of:
      {"kind": "message", "id", "from", "name", "type", "text", "media_id"}
      {"kind": "owner_echo", "to"}   # the owner replied from the WhatsApp Business app
    """
    events: list[dict] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            field = change.get("field")
            value = change.get("value", {})
            if field == "smb_message_echoes":
                for echo in value.get("message_echoes", []):
                    events.append({"kind": "owner_echo", "to": echo.get("to", "")})
                continue
            if field != "messages":
                continue
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in value.get("contacts", [])}
            for m in value.get("messages", []):
                t = m.get("type")
                ev = {"kind": "message", "id": m.get("id", ""), "from": m.get("from", ""),
                      "name": names.get(m.get("from"), ""), "type": t, "text": "", "media_id": ""}
                if t == "text":
                    ev["text"] = m["text"]["body"]
                elif t == "button":
                    ev["text"] = m["button"].get("text", "")
                elif t == "interactive":
                    i = m["interactive"]
                    ev["text"] = (i.get("button_reply") or i.get("list_reply") or {}).get("title", "")
                elif t == "audio":
                    ev["media_id"] = m["audio"]["id"]
                events.append(ev)
    return events
