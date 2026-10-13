# Loan Lens AI

Spec-driven project. Do not implement features except via the checklist.

- Architecture: `documents/LOAN-LENS-SPEC.md`
- Implementation plan: `documents/LOAN-LENS-DEV-CHECKLIST.md`

V1 is a mobile-first web app for loan screenshot analysis. It is one Python API process and one React app. There is no Redis, Postgres, Kafka, Temporal, Celery, RAG, or other extra service.

V1.1 adds a draft inbox on top of that same process. **Analyze enqueues**: `POST .../analyze` stores the draft as queued and returns `202` without waiting on the vision model. **The worker is in-process**: one asyncio worker inside the API process runs one queued draft at a time. **The inbox is per book**: counts and rows are only for the book id on this phone. There is no list of every book. **Never commit `.env`.** A real `VISION_API_KEY` belongs only in that gitignored file.

## Run locally

Use two terminals. The API listens on **8002**. The UI listens on **5175**.

1. Copy `.env.example` to `.env` in the repo root if you do not already have one. Leave `VISION_API_KEY` empty for local runs. Set `VISION_PROVIDER=fake` so analysis uses the built-in fake vision provider and does not call a live model. Never commit `.env`. A real key belongs only in that file, on the server.

2. API:

```text
cd backend
python -m uvicorn app.main:app --host 127.0.0.1 --port 8002
```

3. UI:

```text
cd frontend
npm install
npm run dev
```

4. Open `http://127.0.0.1:5175`. Health is `GET http://127.0.0.1:8002/api/v1/health` and returns `{"status":"ok"}`. That check does not call a vision provider.

To use a real model, set `VISION_PROVIDER=openai_compatible` and fill `VISION_API_KEY`, `VISION_BASE_URL`, and `VISION_MODEL` in `.env` only. Tests and the default local run stay on `VISION_PROVIDER=fake`. For a slow local model such as CPU Ollama, raise `LOANLENS_VISION_TIMEOUT_SECONDS` (for example `1200`). The default is `60`. That value is a ceiling for each vision HTTP call. The call returns as soon as the provider completes. It does not hold the Analyze request open.

## Environment

Names and defaults are in `.env.example` and SPEC §5. Backend variables include `LOANLENS_ENV` (`development`, `test`, or `production`), the SQLite URL, image directory, image limits, draft TTL, vision settings, `LOANLENS_WORKER_ENABLED` (default `true`), `LOANLENS_VISION_TIMEOUT_SECONDS` (default `60`), log level, and `LOANLENS_CORS_ORIGINS`. `VITE_API_BASE_URL` is frontend-only (default `http://127.0.0.1:8002`).

`LOANLENS_CORS_ORIGINS` is a comma-separated allowlist. The default, including when `LOANLENS_ENV=production` and the variable is unset, is `http://127.0.0.1:5175,http://localhost:5175`. It is never `*`. In production, set it to the real site origin. The API refuses to start if the list contains `*`.

## Limits

These match SPEC §22:

| Limit | Value |
|-------|--------|
| Images per analysis | 12 |
| Bytes per image | 10 MiB |
| Longest edge sent to vision | 2048 px |
| Analyze HTTP | Enqueues and returns. Does not wait on vision. Server cap 120 s |
| Vision request timeout | `LOANLENS_VISION_TIMEOUT_SECONDS`, default 60 s. Ceiling only. CPU Ollama example: 1200 |
| Unaccepted draft TTL | 24 h |
| Accepted loans | Kept until that loan is deleted |

## Production

Set `LOANLENS_ENV=production`, point `LOANLENS_CORS_ORIGINS` at the deployed site, and serve the site over HTTPS. Dev may stay on HTTP at localhost. Do not log the API key, image bytes, borrower names, loan IDs, or amounts. V1 has no login: the book id in this browser is the capability for that book.
