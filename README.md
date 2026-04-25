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
- **Database**: Cloud SQL PostgreSQL 16 + pgvector for semantic caching
- **Storage**: Google Cloud Storage
- **Infra**: Google Cloud Run (4 services), Cloud SQL Unix socket connector

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
│   │   ├── auth.py            # Google OAuth + JWT sessions
│   │   └── requirements.txt
│   └── web/                   # Next.js frontend
│       ├── Dockerfile
│       ├── app/
│       │   ├── page.tsx       # Main single-page app
│       │   ├── auth-context.tsx # Auth state provider
│       │   └── login/page.tsx # Google Sign-In page
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
- GCS bucket for video storage

### Environment Variables

Create a `.env` file:

```env
GOOGLE_CLOUD_PROJECT=your-project-id
GCP_LOCATION=asia-south1
DB_HOST=127.0.0.1
DB_PORT=5432
DB_NAME=sketchmind
DB_USER=postgres
DB_PASS=your-password
CLOUD_SQL_INSTANCE=your-project-id:asia-south1:sketchmind-db
GCS_BUCKET=your-project-id-sketchmind-videos

# Authentication
GOOGLE_CLIENT_ID=your-google-oauth-client-id.apps.googleusercontent.com
JWT_SECRET=your-jwt-secret-change-in-prod
NEXT_PUBLIC_GOOGLE_CLIENT_ID=your-google-oauth-client-id.apps.googleusercontent.com
```

### Local Development

```bash
docker-compose up     # starts all 4 services + pgvector DB
```

### Deploy to Cloud Run

```bash
chmod +x deploy.sh
./deploy.sh
```

The deploy script automatically:
- Provisions a Cloud SQL PostgreSQL 16 instance with pgvector (if it doesn't exist)
- Builds and deploys all 4 services to Cloud Run
- Connects the API to Cloud SQL via Unix socket (`--add-cloudsql-instances`)
- Sets up IAM bindings for service-to-service auth and Cloud SQL access
- Prints the live URLs

## Key Features

- **Multi-subtopic generation** — topics are broken into 1-4 subtopics, each producing its own video in parallel
- **Semantic caching** — repeated or similar topics return cached videos instantly (pgvector cosine similarity, 0.78 threshold)
- **Real-time status** — WebSocket updates as the pipeline progresses through research, scripting, coding, and rendering stages
- **Auto-retry** — if Manim rendering fails, the orchestrator sends the error back to the coder agent for a fix (up to 5 retries)
- **Isolated rendering** — heavy Manim workloads run in their own service with dedicated CPU/memory
- **Google OAuth authentication** — sign in with Google to persist search history across sessions
- **Search history** — view, replay, and manage past generations (individual delete + clear all)
- **Background generation** — browse history while a video generates in the background, then restore the result
- **Anonymous rate limiting** — 3 free generations for unauthenticated users, then sign-in required
- **Example topics** — curated topic suggestions to help new users get started quickly
- **Engaging loading UX** — pipeline progress indicator with rotating fun facts during generation
