# SketchMind

AI-powered platform that transforms any topic into animated educational videos using multi-agent orchestration and Manim rendering.

## How It Works

```
User enters topic
      │
      ▼
 [Web Frontend]  ──►  [API Gateway]  ──►  [Agent Orchestrator]  ──►  [Manim Renderer]
    Next.js              FastAPI            Google ADK + Gemini          Manim + GCS
    :3000                :8080                  :8081                     :8082
```

1. **User submits a topic** (e.g. "Pythagorean theorem")
2. **Researcher agent** gathers accurate information via Google Search
3. **Scriptwriter agent** creates a scene-by-scene JSON script
4. **Manim Coder agent** generates Manim Python code from the script
5. **Renderer service** executes the code, produces an MP4, uploads to GCS
6. **Video is returned** to the user in real-time via WebSocket

## Architecture

| Service | Stack | Port | Access |
|---------|-------|------|--------|
| `sketchmind-web` | Next.js 14, React 18 | 3000 | Public |
| `sketchmind-api` | FastAPI, asyncpg, Vertex AI | 8080 | Public |
| `sketchmind-agents` | FastAPI, Google ADK, Gemini 2.5 Flash | 8081 | Internal |
| `sketchmind-renderer` | FastAPI, Manim, FFmpeg, GCS | 8082 | Internal |

## Tech Stack

- **AI/ML**: Google ADK, Gemini 2.5 Flash, Vertex AI Embeddings
- **Animation**: Manim Community Edition
- **Backend**: FastAPI, asyncpg
- **Frontend**: Next.js 14 (App Router)
- **Database**: AlloyDB (PostgreSQL) + pgvector for semantic caching
- **Storage**: Google Cloud Storage
- **Infra**: Google Cloud Run (4 services), VPC Connector

## Project Structure

```
sketchmind/
├── services/
│   ├── renderer/              # Manim render engine
│   │   ├── Dockerfile
│   │   ├── main.py
│   │   └── requirements.txt
│   ├── agents/                # ADK agent orchestrator
│   │   ├── Dockerfile
│   │   ├── main.py
│   │   ├── agent.py
│   │   ├── requirements.txt
│   │   └── tools/
│   │       └── render_tool.py
│   ├── api/                   # Backend API gateway
│   │   ├── Dockerfile
│   │   ├── main.py
│   │   ├── database.py
│   │   ├── embeddings.py
│   │   └── requirements.txt
│   └── web/                   # Next.js frontend
│       ├── Dockerfile
│       ├── app/
│       ├── next.config.js
│       └── package.json
├── deploy.sh
├── .env
└── README.md
```

## Setup

### Prerequisites

- Google Cloud project with billing enabled
- `gcloud` CLI authenticated
- AlloyDB instance with pgvector extension
- GCS bucket for video storage

### Environment Variables

Create a `.env` file:

```env
GOOGLE_CLOUD_PROJECT=your-project-id
GCP_LOCATION=asia-south1
ALLOYDB_HOST=your-alloydb-ip
ALLOYDB_PORT=5432
ALLOYDB_DB=sketchmind
ALLOYDB_USER=postgres
ALLOYDB_PASS=your-password
GCS_BUCKET=your-project-id-sketchmind-videos
```

### Deploy

```bash
chmod +x deploy.sh
./deploy.sh
```

This builds and deploys all 4 services to Cloud Run, sets up IAM bindings for service-to-service auth, and prints the live URLs.

## Key Features

- **Semantic caching** — repeated or similar topics return cached videos instantly (pgvector cosine similarity)
- **Real-time status** — WebSocket updates as the pipeline progresses
- **Auto-retry** — if Manim rendering fails, the orchestrator sends the error back to the coder agent for a fix (up to 2 retries)
- **Isolated rendering** — heavy Manim workloads run in their own service with dedicated CPU/memory
