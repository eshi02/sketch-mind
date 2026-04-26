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
│       │   ├── page.tsx       # Home / topic search
│       │   ├── auth-context.tsx # Auth state provider
│       │   ├── login/page.tsx # Google Sign-In page
│       │   └── paths/         # Learning paths (list + roadmap detail)
│       │       ├── page.tsx
│       │       └── [id]/page.tsx
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
- **Learning paths** — turn a high-level title (e.g. "Calculus Fundamentals") into an AI-designed syllabus of ordered sub-topics; progress is sequential (next unlocks when previous is marked complete), and the next topic is silently pre-fetched so it's ready to watch on demand
- **Anonymous rate limiting** — 3 free generations for unauthenticated users, then sign-in required
- **Example topics** — curated topic suggestions to help new users get started quickly
- **Engaging loading UX** — pipeline progress indicator with rotating fun facts during generation

---

## Submission Notes

### Chosen vertical

**EdTech / personalized visual learning.** Most online learning still hands students static text or pre-recorded video. SketchMind turns *any* topic — typed in plain English — into a custom animated lesson, plus an auto-graded quiz, plus a structured multi-topic learning path. The target user is a learner who wants a visual, paced explanation of something specific, on demand, without trawling YouTube.

### Approach and logic

- **Multi-agent pipeline over monolith.** Each stage (research → script → Manim code → render+fix loop) is its own agent with a narrow job, which makes failures recoverable and prompts tight. The render+fix loop is the differentiator: Manim code that fails to compile is fed back to a fixer agent for up to 5 retries instead of failing the request.
- **Manim, not stock footage / generic image gen.** Manim produces 3Blue1Brown-style animations — the right idiom for math, algorithms, and abstract concepts where motion explains the idea. We trade some flexibility for explanatory power.
- **Semantic caching as a first-class architectural concern, not an afterthought.** Every paid step (embedding, Gemini call, render) is wrapped by a cache that gets cheaper as the system grows. Path creation in particular goes through 5 staged lookups (L0 SQL exact → L1 embed → L2 fuzzy per-user dedupe → L3 cross-user syllabus cache → L4 Gemini) so credits scale with *unique* topics, not with users.
- **Quizzes gate progression, server-side.** Each path topic has 5 multiple-choice questions; the next topic only unlocks at ≥80%. Grading runs on the server, the answer key is stripped from the GET response, and the legacy "mark complete" endpoint was removed so the only path to completion is a passing quiz submission. No client-side bypass.
- **Prefetch is staggered, not eager.** When a user starts topic N we kick off N+1 *and* N+2 in the background, with a backstop trigger on quiz pass. Total generations per fully-completed path stay at N (the session-id check no-ops duplicates), but the second-next topic gets ~10 minutes of head-start instead of ~5.

### How the solution works

End-to-end for a single topic (full diagram above):
1. Frontend posts the topic to the API.
2. API normalises + embeds the query, hits the semantic cache; on hit it returns videos in milliseconds.
3. On miss, the agents service runs the research → script → Manim → render+fix chain, fanning out subtopics in parallel via `asyncio.gather`.
4. The renderer service executes Manim Python in a sandboxed subprocess, uploads the MP4 to GCS, and returns a public URL.
5. Status streams to the browser over a WebSocket throughout.

For learning paths the same machinery is reused per-topic. Path creation runs the L0–L4 lookup chain to either reuse an existing syllabus, fuzzy-dedupe to the user's existing path, or generate a new one. Quiz state lives in its own table keyed by parent `session_id`, so a quiz is generated once and shared across every user who lands on the same topic.

### Assumptions made

- **Topics are educational, not adversarial.** Prompts rely on Gemini's default safety; we don't add a separate jailbreak layer.
