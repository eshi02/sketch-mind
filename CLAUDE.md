# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

SketchMind is an AI-powered platform that transforms topics into animated educational videos. A user submits a topic, an agent pipeline (research → script → Manim code → render) produces MP4s, and videos are returned via WebSocket.

## Architecture

Four microservices deployed to Google Cloud Run:

- **web** (`services/web`, `:3000`) — Next.js 14 App Router frontend. Single-page app in `app/page.tsx`. Submits topics via REST, polls status via WebSocket.
- **api** (`services/api`, `:8080`) — FastAPI gateway. Semantic caching with pgvector (cosine similarity threshold 0.78), session management in Cloud SQL PostgreSQL, delegates to agents service, streams status over WebSocket.
- **agents** (`services/agents`, `:8081`) — Google ADK agent orchestrator. Two-phase design: Phase 1 researches and splits topic into subtopics, Phase 2 processes each subtopic in parallel.
- **renderer** (`services/renderer`, `:8082`) — Executes Manim Python code in a subprocess, uploads MP4 to GCS, returns public URL.

**Request flow:** Web → API `POST /api/generate` → (cache check via embeddings) → Agents `POST /research` → returns subtopics → API fans out parallel calls to Agents `POST /process-subtopic` (NDJSON streaming) → each subtopic runs scriptwriter → manim_generator → render+fix loop → Renderer `POST /render` → GCS → video URLs streamed back through WebSocket.

**Service-to-service auth:** Internal services use GCP ID tokens. Auth is skipped when the URL doesn't contain `run.app` (local dev). See `_auth_headers()` in `services/api/main.py`.

## Agent Pipeline Details

Defined in `services/agents/agent.py`. Two top-level agents returned by `create_agents()`:

1. **researcher** — Breaks topic into 1-4 subtopics as JSON. Uses `google_search` tool. Output stored in `CURRICULUM_JSON` state key.
2. **subtopic_pipeline** (SequentialAgent) — Processes a single subtopic end-to-end:
   - `scriptwriter` → outputs JSON scene script (`SCRIPT_JSON`)
   - `manim_generator` → outputs raw Python code (`MANIM_CODE`). Uses MCP tools (`list_manim_animations`, `lookup_manim_class`, `search_manim_api`) from the Manim API MCP server at `mcp_servers/manim_api_server.py`.
   - `render_and_fix_loop` (LoopAgent, max 5 iterations) — alternates between `renderer` agent (calls `render_manim_video` tool, exits loop on success) and `manim_fixer` agent (debugs using MCP tools, rewrites `MANIM_CODE`).

All agents use `gemini-2.5-flash`. The generated Manim scene class must be named `GeneratedScene`.

The agents service exposes two endpoints: `POST /research` (returns subtopics JSON) and `POST /process-subtopic` (streams NDJSON stage updates).

## Commands

### Web frontend
```bash
cd services/web
npm install
npm run dev          # dev server on :3000
npm run build        # production build
```

### Python services (api, agents, renderer)
Each service runs independently with uvicorn:
```bash
cd services/<service>
pip install -r requirements.txt
uvicorn main:app --reload --port <port>
```
Ports: api=8080, agents=8081, renderer=8082

### Local dev with Docker Compose
```bash
docker-compose up     # starts all 4 services + pgvector DB
```
Docker Compose maps: db=5432, renderer=8082, agents=8081, api=8080, web=3000. The `db` service uses `pgvector/pgvector:pg16` with credentials `postgres/localpass`.

### Deploy all services to Cloud Run
```bash
./deploy.sh
```

## Key Environment Variables

- `NEXT_PUBLIC_API_URL` — API base URL for the web frontend (defaults to `http://localhost:8080`)
- `AGENTS_SERVICE_URL` — agents service URL used by api
- `RENDER_SERVICE_URL` — renderer service URL used by agents
- `GCS_BUCKET` — GCS bucket name for video storage
- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASS` — database connection (TCP, used locally)
- `DB_UNIX_SOCKET` — Cloud SQL Unix socket path (used in production, e.g. `/cloudsql/PROJECT:REGION:INSTANCE`)
- `GOOGLE_CLOUD_PROJECT`, `GCP_LOCATION` — GCP project config (location defaults to `asia-south1`)

## Database

Cloud SQL PostgreSQL 16 with pgvector. Schema auto-created on API startup via `database.py:init_db()`. Uses 768-dim embeddings for semantic caching. In production, Cloud Run connects via Unix socket (`--add-cloudsql-instances`). Locally, Docker Compose provides a pgvector container over TCP.

## Learning Paths

A user-authored ordered curriculum of sub-topics, each generated as a video. Defined in `services/api/main.py` (`/api/paths/...` endpoints), `services/api/database.py` (`learning_paths` table), and `services/web/app/paths/` (Next.js routes).

**Storage:** `learning_paths` row holds `title`, an ordered JSONB `topics` array (`{topic, session_id, completed}` per item), `current_index`, and a `title_embedding vector(768)` for fuzzy lookups. Two relevant indexes: `(user_id, LOWER(title))` for exact dedupe, HNSW on `title_embedding` for fuzzy dedupe + cross-user reuse.

**Dedupe + cache flow on `POST /api/paths`** (cheapest → most expensive, short-circuits on first hit):
1. **L0 exact dedupe** — SQL `LOWER(title) =` for this user → return existing path. *0 LLM calls.*
2. **L1 embed** — `generate_embedding(title)`. (`normalize_topic` is intentionally skipped here for credit savings; `text-embedding-004` is robust enough to phrasing variation that "Trigonometry" vs "explain the concept of trigonometry" still cluster above the 0.78 threshold.)
3. **L2 fuzzy per-user dedupe** — pgvector cosine ≥ `SIMILARITY_THRESHOLD` (0.78), filtered by `user_id` → return existing path.
4. **L3 cross-user syllabus cache** — same threshold, all users → reuse the existing `topics` titles list. Only consulted when the user did not provide explicit `topics`.
5. **L4 fallback** — `generate_path_outline(title)` (one Gemini 2.5 Flash call) → produces a fresh syllabus.

Per-topic video kick-off (`_kick_off_path_topic`) tries an **exact-text fast-path** against `videos` first (`find_completed_parent_by_topic`, partial index `idx_videos_lower_topic_completed_parents`) — zero LLM cost when two paths share a sub-topic title verbatim. Falls through to the existing `check_semantic_cache` on miss.

**2-deep prefetch:** `POST /api/paths/{id}/start/{index}` kicks off the clicked topic and also fires `_prefetch_next_topic` for `index+1` and `index+2` in parallel — gives the second-next topic a full extra topic's worth of head-start so it's ready by the time the user advances. The submit endpoint additionally fires a backstop prefetch on a passing quiz. The session_id check inside `_kick_off_path_topic` is a no-op when a session already exists, so duplicate triggers don't double-spend; total generations per fully-completed N-topic path stay at N.

**Startup backfill:** the API lifespan calls `backfill_path_embeddings(generate_embedding)` after `init_db()`, embedding any `learning_paths` row with `title_embedding IS NULL`. Idempotent — needed once after the embedding column was added so L2 can see legacy paths and collapse near-misses (typos like `"Trignometry"` vs `"Trigonometry"`).

**Path-topic quizzes (gate completion):** each path topic has a 5-question MCQ quiz. Stored in `quizzes` table keyed by parent `session_id` — one Gemini call per unique session, shared across users. `GET /api/paths/{id}/quiz/{index}` lazy-generates and returns questions with `correct`/`explain` stripped via `_strip_answers`. `POST /api/paths/{id}/quiz/{index}/submit` grades server-side; on `score >= QUIZ_PASS_THRESHOLD` (0.8) it calls `mark_path_topic_completed`, unlocking the next topic. **The submit endpoint is the only path to topic completion** — no separate `/complete` endpoint exists, so the gate cannot be bypassed by direct API call.

**Quiz UI in `services/web/app/paths/[id]/page.tsx`:**
- Trigger button on each topic card (`Take Quiz to Unlock Next` for incomplete, `Retake Quiz` for completed) opens a fixed-position modal with a blurred backdrop (z-index 150). All quiz interaction happens inside that modal.
- Per-option highlighting after grading **only marks the user's own pick** (green if correct, red if wrong). The actual correct option is never lit up, so the answer key cannot be inferred from a failed attempt.
- Per-question explanations render only when `quizResult.passed` is true — wrong answers never reveal the explanation/answer.
- `Take Again` / `Retry Quiz` uses `restartQuizAttempt` (soft reset of answers + result, keeping `quizQuestions`) so retakes don't refetch and the modal doesn't flicker.
- Celebration overlay (z-index 200) with falling emoji confetti + spring-pop card fires on **any** pass (fresh unlock or retake), showing the actual percentage; auto-dismisses after 3.5s.

**Important rules:**
- A user cannot create two semantically-similar paths (L0/L2 enforce this). To force a parallel curriculum, use a different title.
- `generate_path_outline` and `generate_quiz` are **not** ADK agents — both are single direct Gemini 2.5 Flash calls from the API service, sitting in `services/api/embeddings.py` next to `normalize_topic`. No changes to `services/agents/agent.py` for the path/quiz features.
- L3 reuses topic *titles* only — never `session_id`s. Per-user progress (`current_index`, `completed`) stays isolated.
- Quiz answers and explanations never leave the server before submission. After submission, the option-highlighting UI reveals only the user's pick, and explanations render only on a passing score.

## Key Implementation Notes

- The API stores pipeline state in an in-memory `sessions` dict (not DB) for WebSocket polling. This means status is lost on API restart.
- Subtopic processing is fully parallel via `asyncio.gather` in `services/api/main.py:run_pipeline()`.
- The renderer has a 240-second subprocess timeout and cleans up temp directories in a `finally` block.
- The web frontend is a single client component (`"use client"`) with inline styles — no CSS framework or component library.
- No test framework is configured in any service. No linting tools are set up.
