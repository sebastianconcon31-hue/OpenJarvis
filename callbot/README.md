# CallBot: a WhatsApp receptionist for barbers

CallBot answers a salon's WhatsApp messages in natural Italian. It:

- tells every new chat it's an AI assistant, and how to reach a person ("operatore")
- answers questions about services, prices and opening hours
- books, moves and cancels appointments in the salon's Google Calendar, without double-booking
- sends a WhatsApp reminder the day before each appointment
- understands voice messages
- hands the chat to the owner when it's unsure, and stays quiet while the owner replies

It can't answer normal phone calls. Calls still ring the owner.

## How it works, in one picture

```
Customer on WhatsApp ──> Meta (WhatsApp Cloud API) ──> CallBot on your €5 server
                                                         │
                         AI model (Groq, free) <─────────┤  writes the reply
                         Google Calendar <───────────────┤  checks free times, books
                         Owner's WhatsApp <──────────────┘  "chat da gestire" alerts
```

The AI only writes the words. Checking which times are free and making the booking is
done by normal code (`callbot/booking.py`), so the bot can't invent a free slot.

## What's in this folder

| File | What it is |
|---|---|
| `salon.example.yaml` | The salon's details: services, prices, hours, holidays, rules. Copy to `salon.yaml`. |
| `.env.example` | Passwords and switches (AI key, WhatsApp keys, calendar). Copy to `.env`. |
| `callbot/` | The code. You don't need to touch it. |
| `docker-compose.yml`, `Dockerfile`, `deploy/` | Everything to run it on a server. |
| `tests/` | Automatic checks that the booking rules and the bot behave. |

Changing a price, the hours or a holiday? Edit `salon.yaml` and restart the bot. No code.

## Switching the AI model

Change one line in `.env`:

```
LLM_PROVIDER=groq        # free tier, gpt-oss-120b
LLM_PROVIDER=deepseek    # paid fallback, very cheap
```

Any "OpenAI-compatible" provider works: set `LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY`.

---

## Setup, step by step

You'll need an adult for the accounts (Meta, Groq, the server), because those services
require you to be 18. Plan about two hours the first time.

### 1. Try it on your computer first (10 minutes)

1. Get a free API key at https://console.groq.com (Groq).
2. In this folder:
   ```bash
   pip install -r requirements.txt
   cp .env.example .env            # then put the key after GROQ_API_KEY=
   cp salon.example.yaml salon.yaml
   python -m callbot.simulate
   ```
3. Chat with it in Italian, as if you were a customer. Try booking, asking prices, asking for "operatore".

### 1b. Web demo: try it in a browser, no WhatsApp needed

With `DEMO_ENABLED=1` in `.env`, the bot also serves a page that looks like a WhatsApp chat at
`/demo` (on your computer: run `uvicorn callbot.app:app` and open http://localhost:8000/demo).
A side panel shows the practice calendar and the alerts the owner would get. Bookings made there
never touch the real calendar. Online, set `DEMO_PASSWORD` so strangers can't use up the free AI
quota. It's also a good way to show the salon owner how the bot behaves before going live.

### 2. Meta / WhatsApp (30 minutes)

Meta only lets a **verified business** send messages. If WhatsApp Manager says "Account restricted,
verify your business" (error 131031), the salon needs to verify its own Meta Business portfolio and
add you as a developer. Use the web demo until then.


1. Go to https://developers.facebook.com, create an app, type **Business**, and add the **WhatsApp** product.
2. In **WhatsApp > API Setup** you get a free **test number**. Add your own phone as a recipient to test.
   Copy the **Phone number ID** into `WHATSAPP_PHONE_NUMBER_ID`.
3. Create a **permanent token**: Business Settings > System users > add one > Generate token, with
   `whatsapp_business_messaging` and `whatsapp_business_management`. Put it in `WHATSAPP_ACCESS_TOKEN`.
   (The token on the API Setup page expires after 24 hours: fine for a first test only.)
4. **App settings > Basic > App secret** goes in `WHATSAPP_APP_SECRET`.
5. Invent a password and put it in `WHATSAPP_VERIFY_TOKEN`.
6. After step 4 below (server running), go to **WhatsApp > Configuration > Webhook**:
   - Callback URL: `https://YOUR-DOMAIN/webhook`
   - Verify token: the password from step 5
   - Subscribe to the **messages** field (and **smb_message_echoes** once the salon's number is connected).

### 3. Message templates (10 minutes, then Meta approves them)

WhatsApp only lets a business message someone first using pre-approved "templates".
In **WhatsApp Manager > Message templates**, create these two, language **Italian**:

**`promemoria_appuntamento`**, category **Utility**:
```
Ciao {{1}}, ti ricordiamo il tuo appuntamento per {{2}} domani alle {{3}} da {{4}}. Se non puoi venire, rispondi a questo messaggio. A presto!
```
Example values: Luca / Taglio uomo / 10:30 / Barberia Esempio

**`avviso_titolare`**, category **Utility** (alerts the owner):
```
Nuova chat da gestire: {{1}} ({{2}}). Motivo: {{3}}. Il bot è in pausa in quella chat.
```

### 4. The server (30 minutes)

1. Rent a small VPS (Hetzner CX22 or similar, about €4-5/month), with Ubuntu.
2. Point a domain or subdomain (for example `bot.yoursite.it`) at the server's IP address.
3. Log in to the server and run:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/sebastianconcon31-hue/openjarvis/main/callbot/deploy/setup-server.sh | bash
   cd /opt/openjarvis/callbot
   nano .env          # fill in the keys and DOMAIN=
   nano salon.yaml    # fill in the salon
   docker compose up -d --build
   ```
4. Open `https://YOUR-DOMAIN/` in a browser. You should see `{"ok": true, ...}`.
5. Now do step 2.6 (connect the webhook) and send a WhatsApp to the test number.

To see what the bot is doing: `docker compose logs -f bot`.
After editing `salon.yaml` or `.env`: `docker compose restart bot`.

### 5. Google Calendar (20 minutes)

1. At https://console.cloud.google.com create a project, and enable the **Google Calendar API**.
2. **IAM & Admin > Service accounts > Create**. Open it, **Keys > Add key > JSON**. Upload that file
   to the server as `/opt/openjarvis/callbot/google-credentials.json`.
3. In Google Calendar (the salon's account), open the calendar's **Settings and sharing**, and under
   **Share with specific people** add the service account's email with **Make changes to events**.
4. Copy the **Calendar ID** (same settings page) into `GOOGLE_CALENDAR_ID`, and set `CALENDAR_BACKEND=google`.
5. `docker compose restart bot`.

The owner can block time by adding events in the calendar. An all-day event (like "Ferie") closes the whole day.

### 6. Going live on the salon's own number (Coexistence)

With **Coexistence**, the salon keeps using the WhatsApp Business app on their phone, and the bot
answers on the same number. In WhatsApp Manager, add a phone number and choose to connect an
existing WhatsApp Business app number, then scan the QR code from the salon's phone.

- The salon must use the **WhatsApp Business** app (free), not normal WhatsApp. Moving from normal
  WhatsApp to WhatsApp Business keeps the chats.
- When the owner replies to a customer from the app, the bot stops answering in that chat for 12 hours
  (change with `HANDOFF_PAUSE_HOURS`).

### Owner commands

The owner can send these to the bot's number:

- `riprendi 393331234567`: the bot starts answering that customer again
- `pausa 393331234567`: the bot stops answering that customer until `riprendi`

## For developers

```bash
pip install -r requirements-dev.txt
python -m pytest -q
uvicorn callbot.app:app --reload
```
