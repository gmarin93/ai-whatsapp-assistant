# WhatsApp Cloud Assistant

A production-ready WhatsApp AI assistant built on **FastAPI**, **LangGraph**, **OpenAI**, and **MCP (Model Context Protocol)**. Designed for high-traffic deployments with a Redis-backed task queue, persistent conversation memory, and horizontally scalable workers.

---

## How it works

```
WhatsApp User
     │
     ▼
Meta Cloud API
     │  POST /webhooks/whatsapp
     ▼
FastAPI (api service)
     │  enqueue_job("process_message", wa_id, text)
     ▼
Redis Queue (ARQ)
     │
     ▼
ARQ Worker (worker service)
     │  acquires per-user lock
     │  calls WhatsAppAgent.reply(wa_id, text)
     │        │
     │        ├── LangGraph worker node
     │        │       └── ChatOpenAI (GPT-4o-mini)
     │        │
     │        └── Tool node (if LLM requests a tool)
     │                ├── search_web      ← MCP server (persistent subprocess)
     │                ├── send_email      ← MCP server (persistent subprocess)
     │                └── Google Calendar ← local LangChain tool
     │
     │  conversation state saved to Redis (AsyncRedisSaver)
     ▼
WhatsApp Cloud API → reply sent to user
```

---

## Project structure

```
ai-whatsapp-assistant/
├── app/
│   ├── main.py                  # FastAPI app + lifespan (ARQ pool or dev fallback)
│   ├── config.py                # All settings via env vars / .env
│   ├── bootstrap.py             # .env loading helper
│   ├── worker.py                # ARQ worker: startup, shutdown, process_message job
│   │
│   ├── webhooks/
│   │   └── whatsapp.py          # Meta webhook verify + receive; enqueues to ARQ
│   │
│   ├── meta/
│   │   └── client.py            # WhatsApp Cloud API client (send messages)
│   │
│   ├── services/
│   │   └── handler.py           # MessageHandler: wa_id + text → agent reply → send
│   │
│   ├── agent/
│   │   ├── graph.py             # LangGraph ReAct agent; Redis or memory checkpointer
│   │   ├── state.py             # AgentState (messages + wa_id)
│   │   ├── prompts.py           # System prompt
│   │   └── tools.py             # Local tools (Google Calendar)
│   │
│   └── mcp/
│       ├── loader.py            # Persistent MCP session pool + LangChain tool wrappers
│       ├── email_server.py      # MCP server: send_email via Resend
│       └── search_server.py     # MCP server: search_web via DuckDuckGo
│
├── docker-compose.yml           # Redis + API + Worker services
├── Dockerfile
├── requirements.txt
└── README.md
```

---

## Key components explained

### FastAPI app (`app/main.py`)

The web layer's only job is to receive Meta webhooks and respond with `200 OK` as fast as possible (Meta requires a response within 15 seconds or it retries).

On startup:
- **Redis available** — connects an ARQ pool and injects it into the webhook router. The API does **not** load the agent or MCP pool; workers own that work.
- **Redis unavailable** — falls back to running the agent and MCP pool in-process (development only).

The webhook router never imports `MessageHandler` directly; it receives either an ARQ pool or a fallback handler via startup injection.

### Webhook router (`app/webhooks/whatsapp.py`)

Handles two concerns before enqueuing:

- **Signature validation** — verifies `X-Hub-Signature-256` using `WHATSAPP_APP_SECRET` to reject forged payloads.
- **Message deduplication** — Meta can re-deliver the same webhook. In production, each message ID is stored in Redis (`whatsapp:seen:{msg_id}`) with `SET NX EX`. If the key already exists the message is skipped. Dedup is disabled in dev fallback mode (no Redis pool).

After passing both checks the message is enqueued via `arq_pool.enqueue_job("process_message", wa_id, text)` and the handler returns immediately.

### ARQ worker (`app/worker.py`)

Each worker process is independent and stateless from Redis's perspective:

1. **Startup** — opens persistent MCP session pools, builds the LangGraph agent with a Redis checkpointer.
2. **`process_message` job** — acquires a per-user Redis lock (`whatsapp:lock:{wa_id}`) before touching the agent. If another worker is already processing messages for that user, the job is re-queued to the same queue (`whatsapp:jobs`) with a 2-second delay. After processing the lock is released.
3. **Shutdown** — closes all MCP subprocess sessions cleanly.

Up to 10 concurrent jobs per worker process (`max_jobs = 10`). Scale workers independently: `docker compose up --scale worker=4 -d`

### LangGraph agent (`app/agent/graph.py`)

A ReAct-style loop: `worker → tools → worker → ... → END`.

- The LLM worker node is fully async (`await llm.ainvoke`) so it does not block the event loop under concurrent jobs.
- The LLM decides when to call tools and when to reply directly.
- Conversation history (all messages per `wa_id`) is persisted in Redis via `AsyncRedisSaver`, so any worker can continue any user's thread, and history survives restarts. Redis indices are created lazily on first use via `await checkpointer.asetup()`.
- Falls back to `MemorySaver` (in-memory, single-process) when Redis is not configured.
- Users can send `/reset` to clear their conversation thread.

### MCP session pool (`app/mcp/loader.py`)

MCP (Model Context Protocol) servers run as stdio subprocesses. The original approach opened a new subprocess for every tool call. The pool keeps **N persistent subprocesses per MCP server alive for the lifetime of the worker** (default `MCP_POOL_SIZE=4`), checked out per tool call from an `asyncio.Queue`. The queue both caps concurrency at the pool size and guarantees one in-flight call per single-threaded stdio session.

This eliminates ~200–500 ms of subprocess spawn overhead per tool call while still allowing parallel tool calls across the pool. Scale further by running more ARQ worker replicas — each worker owns its own pool.

### MCP servers

| Server | File | Tool | Description |
|--------|------|------|-------------|
| `whatsapp-search` | `search_server.py` | `search_web(query)` | DuckDuckGo web search, returns top 5 results |
| `whatsapp-email` | `email_server.py` | `send_email(subject, body_md)` | Sends email via Resend |

Add new MCP servers by creating `app/mcp/*_server.py` files — they are auto-discovered at startup.

### Local tools (`app/agent/tools.py`)

Google Calendar is loaded as a local LangChain tool (not MCP) because it requires OAuth token files on disk. Any tool that needs persistent local state or file access belongs here.

---

## Setup

### 1. Environment variables

Put your secrets in **`../.env`** (the `whatsapp_assistant/` folder, one level above this project). Docker Compose reads that file automatically. You can also add **`ai-whatsapp-assistant/.env`** for local-only overrides.

```env
# WhatsApp Cloud API (Meta Developer Console)
WHATSAPP_ACCESS_TOKEN=your_token
WHATSAPP_PHONE_NUMBER_ID=your_phone_number_id
WHATSAPP_VERIFY_TOKEN=your_verify_token
WHATSAPP_APP_SECRET=your_app_secret        # for signature validation

# OpenAI
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini

# Redis — required for production (queue + shared conversation state + dedup)
REDIS_URL=redis://localhost:6379            # leave unset or unreachable to run in-process dev mode

# Queue / concurrency tuning (optional — defaults shown)
ARQ_QUEUE_NAME=whatsapp:jobs
DEDUP_TTL_SECONDS=60                        # Meta re-delivery window
USER_LOCK_TTL_SECONDS=120                   # max time a worker holds a per-user lock
MCP_POOL_SIZE=4                             # stdio subprocesses per MCP server per worker

# Email via Resend (optional — https://resend.com)
RESEND_API_KEY=re_...
RESEND_FROM_EMAIL=you@your-verified-domain.com   # or onboarding@resend.dev for testing
RESEND_TO_EMAIL=recipient@example.com

# Allowlist (optional — leave empty to allow all users)
ALLOWED_WA_IDS=15551234567,15559876543

# LangSmith tracing (optional — https://smith.langchain.com)
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=ls__...
LANGCHAIN_PROJECT=whatsapp-assistant
```

### 2. Meta Developer Console

1. Create a Meta app at [developers.facebook.com](https://developers.facebook.com)
2. Add the **WhatsApp** product
3. Get your access token and phone number ID from **API Setup**
4. Set `WHATSAPP_APP_SECRET` from **App Settings → Basic**

### 3. Run locally (development, no Docker)

```bash
# Install dependencies
pip install -r requirements.txt
```

**Option A — full stack (recommended, mirrors production)**

Run Redis, the API, and at least one worker in separate terminals:

```bash
# Terminal 1 — Redis (or use an existing instance)
redis-server

# Terminal 2 — API (enqueues jobs, does not run the agent)
uvicorn app.main:app --reload --port 8000

# Terminal 3 — worker (runs the agent + MCP pool)
arq app.worker.WorkerSettings
```

**Option B — single-process dev fallback**

If Redis is not running or `REDIS_URL` is unreachable, the API loads the agent and MCP pool in-process and processes messages via background tasks. This is fine for quick iteration but does not share state across restarts or multiple processes, and webhook deduplication is disabled.

```bash
uvicorn app.main:app --reload --port 8000
```

**Expose for Meta webhooks**

```bash
ngrok http 8000
```

Set the webhook in Meta Developer Console:
- **Callback URL:** `https://<ngrok-host>/webhooks/whatsapp`
- **Verify token:** same as `WHATSAPP_VERIFY_TOKEN`
- **Subscribed fields:** `messages`

### 4. Run with Docker (production)

```bash
# Build and start all services (Redis + API + 2 workers)
docker compose up --build -d

# Check logs
docker compose logs -f worker
docker compose logs -f api

# Scale workers up under load
docker compose up --scale worker=4 -d

# Stop everything
docker compose down
```

---

## Observability (LangSmith)

[LangSmith](https://smith.langchain.com) is LangChain/LangGraph's native tracing platform. When enabled it records every run as a structured trace showing:

- The full LangGraph execution graph (which nodes ran, in what order)
- Each LLM call with inputs, outputs, token usage, and latency
- Every tool invocation and its return value
- Errors with full stack context

### Enable in 3 steps

**1. Get a LangSmith API key**

Sign up at [smith.langchain.com](https://smith.langchain.com) → Settings → API Keys → Create.

**2. Add to your `.env`**

```env
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=ls__...
LANGCHAIN_PROJECT=whatsapp-assistant   # groups traces by project
```

**3. Restart the services**

```bash
# Local
arq app.worker.WorkerSettings   # worker reads the vars at startup

# Docker
docker compose up --build -d
```

No code changes needed. LangChain auto-instruments everything when `LANGCHAIN_TRACING_V2=true` is set. Tracing works for both the in-process dev fallback and the full ARQ worker stack.

### What you'll see in the UI

Each incoming WhatsApp message produces one top-level trace (named after the `wa_id` thread). Expanding it shows:

```
WhatsAppAgent.reply
└── LangGraph: whatsapp-agent
    ├── worker         ← LLM call (inputs + output + tokens)
    ├── tools          ← tool calls (if any)
    │   ├── search_web
    │   └── send_email
    └── worker         ← follow-up LLM call
```

### Disable in production

Leave `LANGCHAIN_TRACING_V2` unset (or set to `false`). There is zero overhead when tracing is off.

---

## Dev vs production

| Concern | Dev fallback (no Redis) | Production (Redis + workers) |
|--------|-------------------------|------------------------------|
| Message processing | In-process background task | ARQ worker queue |
| Agent / MCP pool | Loaded in API process | Loaded in worker process only |
| Conversation state | `MemorySaver` (lost on restart) | `AsyncRedisSaver` in Redis |
| Webhook dedup | Disabled | Redis `SET NX EX` per message ID |
| Per-user ordering lock | Not applied | Redis lock + re-enqueue |
| Horizontal scaling | Single process only | Scale API and workers independently |

In Docker Compose, the `api` service runs Gunicorn with 4 Uvicorn workers (webhook receivers only). The `worker` service runs ARQ and owns all agent/MCP work. Both connect to the same Redis instance.

---

## Architecture decisions

### Why ARQ instead of Celery?

ARQ is fully async (built on `aioredis`), has no broker/backend split, and fits naturally into an async Python codebase. Celery adds significant configuration overhead and requires sync-to-async bridging for LangGraph. For a single-Redis deployment ARQ is the simpler, faster choice.

### Why stdio MCP instead of SSE?

Stdio keeps MCP servers in the same deployment unit — no extra HTTP service to manage. The persistent session pool eliminates the main downside (subprocess spawn per call). If you later need to share MCP servers across multiple API instances (not just workers), migrate to an SSE transport.

### Why per-user Redis lock?

WhatsApp users can send multiple messages quickly. Without a lock, two workers could process messages from the same user simultaneously, causing LangGraph to write conflicting checkpoints and produce out-of-order replies. The lock ensures one-at-a-time processing per user while still allowing full parallelism across different users.

### Why Redis for conversation state?

`MemorySaver` is lost on every restart and is not visible to other worker processes. `AsyncRedisSaver` (from `langgraph-checkpoint-redis`) stores the full message history in Redis, so any worker can pick up any user's conversation exactly where it left off. The checkpointer runs a one-time `asetup()` on first agent use to create the required Redis search indices.

### Redis key layout

| Key pattern | Purpose | TTL |
|-------------|---------|-----|
| `whatsapp:seen:{msg_id}` | Webhook deduplication | `DEDUP_TTL_SECONDS` (default 60 s) |
| `whatsapp:lock:{wa_id}` | Per-user processing lock | `USER_LOCK_TTL_SECONDS` (default 120 s) |
| LangGraph checkpoint keys | Conversation history | Managed by `AsyncRedisSaver` |

---

## Adding tools

### Add an MCP tool (recommended for external services)

Create `app/mcp/your_server.py`:

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("your-server-name")

@mcp.tool()
async def your_tool(param: str) -> str:
    """Description the LLM will use to decide when to call this tool."""
    # your logic here
    return result

if __name__ == "__main__":
    mcp.run(transport="stdio")
```

That's it — the loader auto-discovers all `*_server.py` files at startup.

### Add a local tool (for tools needing local state or files)

Add your tool to `app/agent/tools.py` inside `get_supplemental_tools()`:

```python
from langchain_core.tools import tool

@tool
def my_local_tool(param: str) -> str:
    """Description for the LLM."""
    return "result"
```

---

## Webhook endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/webhooks/whatsapp` | Meta verification handshake |
| `POST` | `/webhooks/whatsapp` | Receive incoming messages |
| `GET` | `/health` | Health check (`{"status": "ok"}`) |
