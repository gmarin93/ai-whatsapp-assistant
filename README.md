# WhatsApp Cloud Assistant

A WhatsApp bot powered by **FastAPI** (Meta webhooks) and **LangGraph** (AI agent with tools). Incoming messages are handled by an OpenAI-backed agent that can search the web, send email, and use Google Calendar.

## Project structure

```
ai-whatsapp-assistant/
├── app/
│   ├── main.py              # FastAPI entry point
│   ├── config.py            # Environment settings
│   ├── bootstrap.py         # Loads .env files
│   ├── webhooks/whatsapp.py # Meta webhook verify + receive
│   ├── meta/client.py       # Send messages via WhatsApp Cloud API
│   ├── agent/
│   │   ├── state.py         # LangGraph state
│   │   ├── graph.py         # Agent loop (LLM + tools)
│   │   ├── tools.py         # Email, web search, calendar tools
│   │   └── prompts.py       # System prompt
│   └── services/handler.py  # Message → agent → reply
├── run.sh                   # Local dev server script
└── README.md
```

---

## Setup guide

### 1. Prerequisites

- **Python 3.11+**
- A [Meta Developer](https://developers.facebook.com/) account with the **WhatsApp** product enabled
- An [OpenAI API key](https://platform.openai.com/api-keys)
- (Optional) [ngrok](https://ngrok.com/) or similar to expose your local server over HTTPS for webhooks

### 2. Clone and create a virtual environment

```bash
git clone <your-repo-url>
cd ai-whatsapp-assistant

python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install \
  fastapi \
  uvicorn[standard] \
  pydantic-settings \
  python-dotenv \
  httpx \
  langchain-core \
  langchain-openai \
  langgraph \
  sendgrid \
  duckduckgo-search
```

**Optional** — Google Calendar tools:

```bash
pip install langchain-google-community google-auth-oauthlib google-auth-httplib2 google-api-python-client
```

### 4. Configure environment variables

Create a `.env` file in the project root (`ai-whatsapp-assistant/.env`):

```env
# Required — WhatsApp Cloud API (Meta Developer Console → WhatsApp → API Setup)
WHATSAPP_ACCESS_TOKEN=your_permanent_or_temporary_token
WHATSAPP_PHONE_NUMBER_ID=your_phone_number_id
WHATSAPP_VERIFY_TOKEN=any_random_string_you_choose

# Recommended — validates incoming webhook signatures in production
WHATSAPP_APP_SECRET=your_meta_app_secret

# Required — OpenAI
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini

# Optional — restrict who the bot replies to (comma-separated WhatsApp user IDs)
# Leave empty to allow everyone (dev only)
ALLOWED_WA_IDS=

# Optional — email tool (SendGrid)
SENDGRID_API_KEY=
SENDGRID_FROM_EMAIL=
SENDGRID_TO_EMAIL=
```

| Variable | Required | Description |
|----------|----------|-------------|
| `WHATSAPP_ACCESS_TOKEN` | Yes | Token from Meta → WhatsApp → API Setup |
| `WHATSAPP_PHONE_NUMBER_ID` | Yes | Phone number ID shown in API Setup |
| `WHATSAPP_VERIFY_TOKEN` | Yes | Any secret string; must match what you enter in the Meta webhook form |
| `WHATSAPP_APP_SECRET` | No | App secret from Meta → App settings → Basic; enables signature validation |
| `OPENAI_API_KEY` | Yes | Powers the LangGraph agent |
| `OPENAI_MODEL` | No | Defaults to `gpt-4o-mini` |
| `ALLOWED_WA_IDS` | No | Comma-separated list of allowed sender IDs; empty = allow all |
| `SENDGRID_*` | No | Needed only if you want the agent to send emails |

### 5. Set up Meta WhatsApp Cloud API

1. Go to [Meta for Developers](https://developers.facebook.com/) and create an app (type: **Business**).
2. Add the **WhatsApp** product to your app.
3. Under **WhatsApp → API Setup**, note:
   - **Temporary access token** (or generate a permanent one)
   - **Phone number ID**
   - **WhatsApp Business Account ID**
4. Under **API Setup → Step 1**, add your personal phone number as a **test recipient** so the bot can reply to you during development.
5. Copy the values into your `.env` file.

### 6. (Optional) Google Calendar

If you want calendar tools, place OAuth credentials in the project root:

- `credentials.json` — downloaded from [Google Cloud Console](https://console.cloud.google.com/) (OAuth 2.0 Client ID)
- `token.json` — created automatically on first auth (or copy from a previous setup)

The agent loads calendar tools automatically when these files are present. If they are missing, the bot still works — calendar tools are simply skipped.

### 7. Run the server locally

```bash
# From the project root, with your venv activated:
chmod +x run.sh
./run.sh
```

Or run uvicorn directly:

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Verify it is running:

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok"}
```

### 8. Expose HTTPS and register the webhook

Meta requires a public HTTPS URL for webhooks. Use ngrok for local development:

```bash
ngrok http 8000
```

Copy the `https://` URL ngrok gives you (e.g. `https://abc123.ngrok-free.app`).

In **Meta Developer Console → WhatsApp → Configuration → Webhook**:

| Field | Value |
|-------|-------|
| Callback URL | `https://<your-ngrok-host>/webhooks/whatsapp` |
| Verify token | Same string as `WHATSAPP_VERIFY_TOKEN` in `.env` |
| Webhook fields | Subscribe to **messages** |

Click **Verify and save**. You should see `Webhook verified successfully` in your server logs.

> Keep both `./run.sh` and ngrok running while testing.

### 9. Test it

1. Open WhatsApp on your phone and send a message to the **Meta test business number** shown in API Setup (or your configured business number).
2. The server logs should show the incoming webhook and the agent's reply.
3. Send `/reset` to clear the conversation thread for your number.

**Quick webhook checks** (without WhatsApp):

```bash
# Health check
curl http://127.0.0.1:8000/health

# Simulate Meta verification (replace YOUR_VERIFY_TOKEN and CHALLENGE)
curl "http://127.0.0.1:8000/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=YOUR_VERIFY_TOKEN&hub.challenge=CHALLENGE123"
# Should return: CHALLENGE123
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Webhook verification fails (403) | Ensure `WHATSAPP_VERIFY_TOKEN` in `.env` exactly matches the token in Meta Console |
| Bot receives messages but does not reply | Add your phone number to Meta → API Setup → test recipient list |
| `401` on send | Token expired — generate a new token in Meta and update `WHATSAPP_ACCESS_TOKEN` |
| `131030` / "not in allowed list" | Your number is not on the Meta test recipient list |
| Calendar tools not loading | Check `credentials.json` exists and Google Calendar API is enabled in Cloud Console |
| Email tool skipped | Set `SENDGRID_API_KEY` in `.env` |

---

## Features to implement

- [ ] Validate `X-Hub-Signature-256` on all incoming POSTs (partially implemented)
- [ ] Parse all message types (text, image, audio, buttons)
- [ ] Add more tools in `app/agent/tools.py`
- [ ] `/help` command
- [ ] Replace `MemorySaver` with a Postgres checkpointer for production
- [ ] Queue (Redis/SQS) if webhook processing gets slow
- [ ] Typing indicator / read receipts via Meta client
- [ ] Deploy (Docker + Cloud Run / ECS)
