# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

SketchMind is an AI-powered platform that transforms topics into animated educational videos. A user submits a topic, an agent pipeline (research → script → Manim code → render) produces MP4s, and videos are returned via WebSocket.

## Architecture

Four microservices deployed to Google Cloud Run:

- **web** (`services/web`, `:3000`) — Next.js 14 App Router frontend. Single-page app in `app/page.tsx`. Submits topics via REST, polls status via WebSocket.
- **api** (`services/api`, `:8080`) — FastAPI gateway. Semantic caching with pgvector (cosine similarity threshold 0.85), session management in AlloyDB, delegates to agents service, streams status over WebSocket.
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
- `ALLOYDB_HOST`, `ALLOYDB_PASS`, `ALLOYDB_DB` — database connection
- `GOOGLE_CLOUD_PROJECT`, `GCP_LOCATION` — GCP project config (location defaults to `asia-south1`)

## Database

AlloyDB (PostgreSQL) with pgvector. Schema auto-created on API startup via `database.py:init_db()`. Uses 768-dim embeddings for semantic caching. Locally, Docker Compose provides a pgvector container.

## Key Implementation Notes

- The API stores pipeline state in an in-memory `sessions` dict (not DB) for WebSocket polling. This means status is lost on API restart.
- Subtopic processing is fully parallel via `asyncio.gather` in `services/api/main.py:run_pipeline()`.
- The renderer has a 240-second subprocess timeout and cleans up temp directories in a `finally` block.
- The web frontend is a single client component (`"use client"`) with inline styles — no CSS framework or component library.
- No test framework is configured in any service. No linting tools are set up.
