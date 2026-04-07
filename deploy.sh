#!/bin/bash
set -euo pipefail

# ── Configuration ─────────────────────────────────────
export PROJECT_ID=$(gcloud config get-value project)
export REGION="asia-south1"
export REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/sketchmind"
export SQL_INSTANCE_NAME="sketchmind-db"
export SQL_INSTANCE_CONNECTION="${PROJECT_ID}:${REGION}:${SQL_INSTANCE_NAME}"

# ── Source .env & validate ────────────────────────────
if [ -f .env ]; then
  set -a; source .env; set +a
fi

for var in DB_PASS DB_USER DB_NAME; do
  if [ -z "${!var:-}" ]; then
    echo "ERROR: $var is not set. Check your .env file."
    exit 1
  fi
done

# ── Service account (deterministic lookup) ────────────
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
echo "Using service account: $SA"

# ── Cloud SQL provisioning (idempotent) ───────────────
if gcloud sql instances describe "$SQL_INSTANCE_NAME" --project="$PROJECT_ID" &>/dev/null; then
  echo "Cloud SQL instance '$SQL_INSTANCE_NAME' already exists, skipping creation."
else
  echo "Creating Cloud SQL instance '$SQL_INSTANCE_NAME' (this takes ~5 minutes)..."
  gcloud sql instances create "$SQL_INSTANCE_NAME" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --database-version=POSTGRES_16 \
    --tier=db-f1-micro \
    --storage-size=10GB \
    --no-assign-ip \
    --database-flags=cloudsql.enable_pgvector=on

  echo "Setting postgres user password..."
  gcloud sql users set-password postgres \
    --instance="$SQL_INSTANCE_NAME" \
    --password="$DB_PASS"

  echo "Creating database '$DB_NAME'..."
  gcloud sql databases create "$DB_NAME" \
    --instance="$SQL_INSTANCE_NAME" || true
fi

# ── IAM: Cloud SQL client access ──────────────────────
echo "Granting Cloud SQL Client role..."
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$SA" \
  --role="roles/cloudsql.client" \
  --condition=None --quiet

# ── Artifact Registry (ensure repo exists) ────────────
gcloud artifacts repositories describe sketchmind \
  --location="$REGION" --project="$PROJECT_ID" &>/dev/null || \
gcloud artifacts repositories create sketchmind \
  --repository-format=docker --location="$REGION" --project="$PROJECT_ID"

echo "=== Building & deploying all 4 services ==="

# ── 1. Renderer ───────────────────────────────────────
echo ">>> Renderer..."
gcloud builds submit services/renderer --tag "$REGISTRY/renderer" --timeout=1200
gcloud run deploy sketchmind-renderer \
    --image="$REGISTRY/renderer" --region="$REGION" \
    --cpu=2 --memory=2Gi --timeout=300 \
    --concurrency=1 --min-instances=1 --max-instances=5 \
    --set-env-vars="GCS_BUCKET=${PROJECT_ID}-sketchmind-videos" \
    --no-allow-unauthenticated

RENDER_URL=$(gcloud run services describe sketchmind-renderer \
    --region="$REGION" --format="value(status.url)")

# ── 2. Agents ─────────────────────────────────────────
echo ">>> Agents..."
gcloud builds submit services/agents --tag "$REGISTRY/agents"
gcloud run deploy sketchmind-agents \
    --image="$REGISTRY/agents" --region="$REGION" \
    --cpu=1 --memory=512Mi --timeout=300 \
    --min-instances=1 --max-instances=3 \
    --set-env-vars="RENDER_SERVICE_URL=$RENDER_URL,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=$REGION" \
    --no-allow-unauthenticated

AGENTS_URL=$(gcloud run services describe sketchmind-agents \
    --region="$REGION" --format="value(status.url)")

# ── 3. API (Cloud SQL via Unix socket) ────────────────
echo ">>> API..."
gcloud builds submit services/api --tag "$REGISTRY/api"
gcloud run deploy sketchmind-api \
    --image="$REGISTRY/api" --region="$REGION" \
    --cpu=1 --memory=512Mi --timeout=300 \
    --min-instances=1 --max-instances=3 \
    --add-cloudsql-instances="$SQL_INSTANCE_CONNECTION" \
    --set-env-vars="AGENTS_SERVICE_URL=$AGENTS_URL,GCP_PROJECT_ID=$PROJECT_ID,DB_NAME=$DB_NAME,DB_USER=$DB_USER,DB_PASS=$DB_PASS,DB_UNIX_SOCKET=/cloudsql/$SQL_INSTANCE_CONNECTION" \
    --allow-unauthenticated

API_URL=$(gcloud run services describe sketchmind-api \
    --region="$REGION" --format="value(status.url)")

# ── 4. Web ────────────────────────────────────────────
echo ">>> Web..."
echo "NEXT_PUBLIC_API_URL=$API_URL" > services/web/.env.production
gcloud builds submit services/web --tag "$REGISTRY/web"
gcloud run deploy sketchmind-web \
    --image="$REGISTRY/web" --region="$REGION" \
    --cpu=1 --memory=256Mi \
    --min-instances=0 --max-instances=3 \
    --allow-unauthenticated

# ── IAM: service-to-service auth ──────────────────────
gcloud run services add-iam-policy-binding sketchmind-renderer \
    --region="$REGION" --member="serviceAccount:$SA" --role="roles/run.invoker"
gcloud run services add-iam-policy-binding sketchmind-agents \
    --region="$REGION" --member="serviceAccount:$SA" --role="roles/run.invoker"

# ── Summary ───────────────────────────────────────────
WEB_URL=$(gcloud run services describe sketchmind-web \
    --region="$REGION" --format="value(status.url)")

echo ""
echo "=============================="
echo "  SketchMind deployed!"
echo "  Web:      $WEB_URL"
echo "  API:      $API_URL"
echo "  Agents:   $AGENTS_URL"
echo "  Renderer: $RENDER_URL"
echo "  DB:       $SQL_INSTANCE_CONNECTION"
echo "=============================="
