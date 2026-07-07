# Falcon — Next.js + FastAPI

Falcon has been rebuilt from Streamlit into a modern two-service app:

- **`frontend/`** — Next.js 15 (App Router, TypeScript, Tailwind v4, TanStack Query, Zustand). A fast SPA with token-by-token streaming, all seven tabs (Chat, Context, Memory, Audit, Logs, Testing, Dual Run), image uploads, and light/dark themes.
- **`backend/`** — FastAPI. Reuses the original, battle-tested `falcon/` package (engine, memory, identity, audit, judge, summarizer, dual-run, tools) unchanged in behaviour, exposed over a typed REST + Server-Sent-Events API.

This is a rewrite of an original Streamlit app. The feature concepts (identities, memory types, retrieval scoring, dual-run breakthrough detection, etc.) are documented in [`README.md`](README.md) and are preserved 1:1 in this rewrite.

---

## Architecture

```
Browser ──HTTP/SSE──▶  Next.js (frontend)     ── serves the UI
        ──/api/*────▶  FastAPI (backend)      ── inference, memory, audit, …
                              │
                              ├──▶ MongoDB Atlas   (all persistence)
                              └──▶ OpenRouter      (LLM inference + tools)
```

- The frontend calls **relative `/api/...`** paths. In production the App Platform ingress routes `/api` to the backend, so there is **no CORS** and no cross-origin config.
- Chat streams over **Server-Sent Events** (`POST /api/chat/send`). The blocking OpenAI/LangGraph generators run in a worker thread and are pumped to the event loop, so streaming stays smooth and the loop never blocks.
- The backend is **stateless**: the frontend owns all settings (model, toggles, generation controls) and sends them per request — identical to what the Streamlit `session_state` fed the inference flow.

---

## Repository layout

```
backend/
  app/            FastAPI layer (routers, services, sse, settings)
  falcon/         reused domain package (db.py decoupled from Streamlit)
  tests/          continuity-test harness (Testing tab)
  config.yaml     runtime config (models, persona, generation, judge prompt)
  requirements.txt
  Dockerfile
frontend/
  src/app         App Router pages + providers
  src/components  Sidebar, tabs, chat, UI primitives
  src/lib         api client, SSE client, store, query hooks, types
  Dockerfile
.do/app.yaml      DigitalOcean App Platform spec (both services)
docker-compose.yml + deploy/nginx.conf   local full-stack / Droplet path
```

---

## Local development

**Prerequisites:** the `falcon` conda env (Python 3.11+), Node 20+, a MongoDB URI and OpenRouter key.

### 1. Backend (conda `falcon`)

```bash
# from backend/  — needs backend/.env (copy from ../.env.example)
conda run -n falcon python -m uvicorn app.main:app --reload --port 8017
```

Health check: <http://127.0.0.1:8017/health> · API docs: <http://127.0.0.1:8017/docs>

### 2. Frontend

```bash
cd frontend
npm install
npm run dev          # http://localhost:3000
```

`frontend/.env.local` already points the UI at the backend:
`NEXT_PUBLIC_API_BASE=http://127.0.0.1:8017`.

### Or: full stack in Docker (mirrors production)

```bash
cp .env.example .env      # fill in secrets
docker compose up --build
open http://localhost:8080
```

nginx routes `/api` → backend and `/` → frontend on one origin, exactly like App Platform.

---

## Environment variables

| Variable | Where | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | backend | LLM inference (required) |
| `MONGODB_URI` | backend | MongoDB Atlas connection (required) |
| `NASA_API_KEY` | backend | NASA APOD tool (optional; `DEMO_KEY` works) |
| `WEATHERAPI_KEY` | backend | Weather tool (optional; Open-Meteo fallback) |
| `CORS_ORIGINS` | backend | allowed origins, default `*` (no auth) |
| `ENABLE_DOCS` | backend | expose `/docs` `/redoc` (default true) |
| `NEXT_PUBLIC_API_BASE` | frontend build | backend origin; empty = relative `/api` |

---

## Deploy to DigitalOcean App Platform (git auto-deploy)

The app is defined in [`.do/app.yaml`](.do/app.yaml) as two Docker services that **auto-deploy on every push** to `awaismaq97/falcon`.

### First-time switch (one-time)

Your existing App was configured for Streamlit. Point it at the new spec once:

**Option A — dashboard**
1. DigitalOcean → **Apps** → your Falcon app → **Settings** → **Edit App Spec**.
2. Paste the contents of `.do/app.yaml`, **Save**.
3. Under **Settings → App-Level Environment Variables**, set the four secrets
   (`OPENROUTER_API_KEY`, `MONGODB_URI`, `NASA_API_KEY`, `WEATHERAPI_KEY`) as
   **encrypted**. (They likely already exist from the Streamlit deploy.)

**Option B — doctl**
```bash
doctl apps list                                  # find your APP_ID
doctl apps update <APP_ID> --spec .do/app.yaml
# then set the encrypted secrets in the dashboard (values aren't committed)
```

> Adjust `region:` in the spec to match your existing app, and the
> `instance_size_slug`s to your plan if needed.

### After that

Just `git push` to `main`. App Platform rebuilds both services and deploys —
same workflow you already use. The frontend is served at your app URL; `/api`
is routed to the backend automatically.

---

## Deploy to a DigitalOcean Droplet (alternative)

```bash
# on the Droplet, with Docker + compose installed and .env filled in
git clone https://github.com/awaismaq97/falcon.git && cd falcon
docker compose up -d --build
```

Put a TLS terminator (e.g. Caddy, or certbot + the provided nginx) in front for HTTPS.

---

## Notes on parity & performance

- **Streaming** reuses the exact `<think>`-stripping, anti-fabrication tool guard, and judge buffering from the original engine — behaviour is identical, latency is lower (no Streamlit reruns).
- **Background work** (memory extraction, summarization, audit, token totals, dual-run) runs in daemon threads *after* the answer is delivered, so the send never blocks — same design as before.
- **No auth** by design (matches the current app). Since every request spends OpenRouter credits, restrict access at the platform level (or add a gate) before sharing a public URL widely.
- Scale by increasing `instance_count`, not workers per instance — the Mongo client and background threads are per-process.
```
