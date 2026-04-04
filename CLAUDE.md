# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

SketchMind is an AI-powered platform that transforms topics into animated educational videos. A user submits a topic, an agent pipeline (research → script → Manim code → render) produces an MP4, and the video is returned via WebSocket.

## Architecture

Four microservices, each a separate FastAPI/Next.js app deployed to Google Cloud Run:

- **web** (`:3000`) — Next.js 14 App Router frontend. Submits topics to the API, polls status via WebSocket.
- **api** (`:8080`) — FastAPI gateway. Handles semantic caching (pgvector cosine similarity, threshold 0.85), creates sessions in AlloyDB, delegates to agents service, streams status over WebSocket.
- **agents** (`:8081`) — Google ADK orchestrator with 4 agents (researcher → scriptwriter → manim_coder → orchestrator). Uses Gemini 2.5 Flash. The orchestrator auto-retries failed renders up to 2 times.
- **renderer** (`:8082`) — Executes Manim Python code in a subprocess, uploads MP4 to GCS, returns public URL.

**Request flow:** Web → API `/api/generate` → (cache check via embeddings) → Agents `/generate` → Renderer `/render` → GCS → video URL returned through chain.

**Service-to-service auth:** Internal services (agents, renderer) use GCP ID tokens. Auth is skipped when the URL doesn't contain `run.app` (i.e., local dev).

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

AlloyDB (PostgreSQL) with pgvector. Single `videos` table with 768-dim embeddings for semantic caching. Schema auto-created on API startup via `database.py:init_db()`.

## Agent Pipeline Details

Defined in `services/agents/agent.py`. The `root_agent` (orchestrator) delegates sequentially:
1. `researcher` — uses `google_search` tool
2. `scriptwriter` — outputs JSON array of scenes
3. `manim_coder` — outputs raw Python (class must be `GeneratedScene`)
4. Orchestrator calls `render_manim_video` tool which POSTs to renderer service

The render tool is in `services/agents/tools/render_tool.py`. It's a synchronous function registered as an ADK tool.
