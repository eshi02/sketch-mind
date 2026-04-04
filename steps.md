# Running SketchMind Locally

## Option A: Docker Compose (recommended)

### Prerequisites

- Docker and Docker Compose
- GCP credentials (`gcloud auth application-default login`)
- A GCS bucket for video storage

### 1. Create a `.env` file in the project root

```env
GOOGLE_CLOUD_PROJECT=your-project-id
GCP_LOCATION=asia-south1
GCS_BUCKET=your-bucket-name

--> gcloud storage list

```

### 2. Start everything

```bash
docker compose up --build
```

This starts all 4 services + a PostgreSQL database with pgvector:

| Service  | URL                    |
|----------|------------------------|
| Web      | http://localhost:3000   |
| API      | http://localhost:8080   |
| Agents   | http://localhost:8081   |
| Renderer | http://localhost:8082   |
| Postgres | localhost:5432          |

### 3. Verify

```bash
curl http://localhost:8080/health
curl http://localhost:8081/health
curl http://localhost:8082/health
```

Then open http://localhost:3000.

### Useful commands

```bash
docker compose up --build -d   # run in background
docker compose logs -f api     # tail logs for one service
docker compose down            # stop everything
docker compose down -v         # stop and wipe database volume
```

---

## Option B: Run services manually (without Docker)

### Prerequisites

- Python 3.11+
- Node.js 20+
- PostgreSQL with pgvector extension
- `gcloud` CLI authenticated
- GCS bucket for video storage
- Manim system dependencies: `ffmpeg`, `cairo`, `pango`, LaTeX (texlive-base)

On macOS:
```bash
brew install ffmpeg cairo pango pkg-config
brew install --cask mactex-no-gui   # or: brew install basictex
```

### 1. Set up the database

```bash
brew install pgvector
psql -U postgres -c "CREATE DATABASE sketchmind;"
psql -U postgres -d sketchmind -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

The `videos` table and index are auto-created when the API starts.

### 2. Start services (each in a separate terminal)

**Renderer (port 8082):**
```bash
cd services/renderer
pip install -r requirements.txt
GCS_BUCKET=your-bucket-name uvicorn main:app --reload --port 8082
```

**Agents (port 8081):**
```bash
cd services/agents
pip install -r requirements.txt
RENDER_SERVICE_URL=http://localhost:8082 \
GOOGLE_CLOUD_PROJECT=your-project-id \
GOOGLE_CLOUD_LOCATION=asia-south1 \
  uvicorn main:app --reload --port 8081
```

**API (port 8080):**
```bash
cd services/api
pip install -r requirements.txt
AGENTS_SERVICE_URL=http://localhost:8081 \
GCP_PROJECT_ID=your-project-id \
ALLOYDB_HOST=127.0.0.1 \
ALLOYDB_PASS=your-password \
ALLOYDB_DB=sketchmind \
  uvicorn main:app --reload --port 8080
```

**Web (port 3000):**
```bash
cd services/web
npm install
NEXT_PUBLIC_API_URL=http://localhost:8080 npm run dev
```

### Startup order

```
Renderer (8082) → Agents (8081) → API (8080) → Web (3000)
```

## Quick Test Without the Full Pipeline

To test just the renderer in isolation:

```bash
curl -X POST http://localhost:8082/render \
  -H "Content-Type: application/json" \
  -d '{
    "python_code": "from manim import *\nclass GeneratedScene(Scene):\n    def construct(self):\n        self.play(Write(Text(\"Hello SketchMind\")))\n        self.wait(1)",
    "scene_class_name": "GeneratedScene",
    "quality": "l"
  }'
```

This renders a simple Manim scene and uploads to GCS, confirming your Manim install and GCS credentials work.

## Tips

- **Auth is skipped locally** — service-to-service auth only activates when the URL contains `run.app`, so local `localhost` URLs bypass it automatically.
- **Semantic caching** — if you re-submit the same (or similar) topic, the API returns the cached video instantly without re-running agents. To force re-generation during testing, use a significantly different topic or clear the `videos` table.
- **Manim render failures** — the agent orchestrator retries up to 2 times. Check the renderer logs for Manim stderr output if videos aren't generating.
- **Use separate terminals** — run each service in its own terminal window so you can see logs independently.
