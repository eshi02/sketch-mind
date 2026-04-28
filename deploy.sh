#!/bin/bash
set -euo pipefail

# ── Usage ─────────────────────────────────────────────
# ./deploy.sh              Deploy ALL services (full deploy)
# ./deploy.sh web          Deploy only the web service
# ./deploy.sh agents web   Deploy agents + web
# ./deploy.sh renderer     Deploy only the renderer
# Valid names: renderer, mcp, agents, api, web, infra

SERVICES=("$@")

should_deploy() {
  # If no args given, deploy everything
  [ ${#SERVICES[@]} -eq 0 ] && return 0
  for s in "${SERVICES[@]}"; do
    [ "$s" = "$1" ] && return 0
  done
  return 1
}

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

for var in DB_PASS DB_USER DB_NAME JWT_SECRET GOOGLE_CLIENT_ID; do
  if [ -z "${!var:-}" ]; then
    echo "ERROR: $var is not set. Check your .env file."
    exit 1
  fi
done

# ── Service account (deterministic lookup) ────────────
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
echo "Using service account: $SA"

# ── Infrastructure (only on full deploy or explicit) ──
if should_deploy "infra" && [ ${#SERVICES[@]} -eq 0 ] || should_deploy "infra"; then
  # Cloud SQL
  if gcloud sql instances describe "$SQL_INSTANCE_NAME" --project="$PROJECT_ID" &>/dev/null; then
    echo "Cloud SQL instance '$SQL_INSTANCE_NAME' already exists, skipping."
  else
    echo "Creating Cloud SQL instance '$SQL_INSTANCE_NAME' (~5 minutes)..."
    gcloud sql instances create "$SQL_INSTANCE_NAME" \
      --project="$PROJECT_ID" --region="$REGION" \
      --database-version=POSTGRES_16 --edition=enterprise \
      --tier=db-f1-micro --storage-size=10GB --assign-ip
    gcloud sql users set-password postgres \
      --instance="$SQL_INSTANCE_NAME" --password="$DB_PASS"
    gcloud sql databases create "$DB_NAME" \
      --instance="$SQL_INSTANCE_NAME" || true
  fi

  # APIs + IAM
  echo "Enabling required APIs..."
  gcloud services enable cloudtasks.googleapis.com --project="$PROJECT_ID"

  echo "Granting IAM roles..."
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$SA" --role="roles/cloudsql.client" \
    --condition=None --quiet
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$SA" --role="roles/cloudtasks.enqueuer" \
    --condition=None --quiet
  gcloud iam service-accounts add-iam-policy-binding "$SA" \
    --member="serviceAccount:$SA" --role="roles/iam.serviceAccountTokenCreator" \
    --project="$PROJECT_ID" --quiet

  # Cloud Tasks queue
  QUEUE="subtopic-processing"
  if ! gcloud tasks queues describe "$QUEUE" --location="$REGION" --project="$PROJECT_ID" &>/dev/null; then
    gcloud tasks queues create "$QUEUE" --location="$REGION" --project="$PROJECT_ID"
  fi
  gcloud tasks queues update "$QUEUE" \
      --location="$REGION" --project="$PROJECT_ID" \
      --max-concurrent-dispatches=12 --max-dispatches-per-second=5 --max-attempts=3

  # Artifact Registry
  gcloud artifacts repositories describe sketchmind \
    --location="$REGION" --project="$PROJECT_ID" &>/dev/null || \
  gcloud artifacts repositories create sketchmind \
    --repository-format=docker --location="$REGION" --project="$PROJECT_ID"
fi

QUEUE="subtopic-processing"

# ── Helper: get existing service URL ──────────────────
get_url() {
  gcloud run services describe "$1" --region="$REGION" --format="value(status.url)" 2>/dev/null || echo ""
}

if [ ${#SERVICES[@]} -eq 0 ]; then
  echo "=== Full deploy: all 5 services ==="
else
  echo "=== Deploying: ${SERVICES[*]} ==="
fi

# ── 1. Renderer ───────────────────────────────────────
if should_deploy "renderer"; then
  echo ">>> Renderer..."
  gcloud builds submit services/renderer --tag "$REGISTRY/renderer" --timeout=1200
  gcloud run deploy sketchmind-renderer \
      --image="$REGISTRY/renderer" --region="$REGION" \
      --cpu=4 --memory=4Gi --timeout=360 \
      --concurrency=2 --min-instances=1 --max-instances=8 \
      --set-env-vars="GCS_BUCKET=${PROJECT_ID}-sketchmind-videos" \
      --no-allow-unauthenticated
fi
RENDER_URL=$(get_url sketchmind-renderer)

# ── 2. MCP Server ─────────────────────────────────────
if should_deploy "mcp"; then
  echo ">>> MCP Server..."
  gcloud builds submit services/mcp-server --tag "$REGISTRY/mcp-server"
  gcloud run deploy sketchmind-mcp \
      --image="$REGISTRY/mcp-server" --region="$REGION" \
      --cpu=1 --memory=256Mi --timeout=300 \
      --concurrency=80 --min-instances=1 --max-instances=2 \
      --allow-unauthenticated
fi
MCP_URL=$(get_url sketchmind-mcp)

# ── 3. Agents ─────────────────────────────────────────
if should_deploy "agents"; then
  echo ">>> Agents..."
  gcloud builds submit services/agents --tag "$REGISTRY/agents"
  gcloud run deploy sketchmind-agents \
      --image="$REGISTRY/agents" --region="$REGION" \
      --cpu=2 --memory=2Gi --timeout=1800 \
      --concurrency=4 --min-instances=1 --max-instances=3 \
      --add-cloudsql-instances="$SQL_INSTANCE_CONNECTION" \
      --set-env-vars="RENDER_SERVICE_URL=$RENDER_URL,MCP_SERVER_URL=$MCP_URL,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=global,GOOGLE_GENAI_USE_VERTEXAI=true,AGENT_PRO_MODEL=${AGENT_PRO_MODEL:-gemini-3.1-pro-preview},AGENT_FLASH_MODEL=${AGENT_FLASH_MODEL:-gemini-2.5-flash},DB_NAME=$DB_NAME,DB_USER=$DB_USER,DB_PASS=$DB_PASS,DB_UNIX_SOCKET=/cloudsql/$SQL_INSTANCE_CONNECTION" \
      --no-allow-unauthenticated
fi
AGENTS_URL=$(get_url sketchmind-agents)

# ── 4. API ────────────────────────────────────────────
if should_deploy "api"; then
  echo ">>> API..."
  gcloud builds submit services/api --tag "$REGISTRY/api"
  gcloud run deploy sketchmind-api \
      --image="$REGISTRY/api" --region="$REGION" \
      --cpu=1 --memory=512Mi --timeout=300 \
      --min-instances=1 --max-instances=5 \
      --add-cloudsql-instances="$SQL_INSTANCE_CONNECTION" \
      --set-env-vars="AGENTS_SERVICE_URL=$AGENTS_URL,GCP_PROJECT_ID=$PROJECT_ID,DB_NAME=$DB_NAME,DB_USER=$DB_USER,DB_PASS=$DB_PASS,DB_UNIX_SOCKET=/cloudsql/$SQL_INSTANCE_CONNECTION,JWT_SECRET=${JWT_SECRET:-},GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID:-},SUBTOPIC_QUEUE=$QUEUE,CLOUD_TASKS_LOCATION=$REGION,OIDC_SERVICE_ACCOUNT_EMAIL=$SA" \
      --allow-unauthenticated
fi
API_URL=$(get_url sketchmind-api)

# ── 5. Web ────────────────────────────────────────────
if should_deploy "web"; then
  echo ">>> Web..."
  cat > services/web/.env.production <<EOF
NEXT_PUBLIC_API_URL=$API_URL
NEXT_PUBLIC_GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID:-}
EOF
  gcloud builds submit services/web --tag "$REGISTRY/web"
  gcloud run deploy sketchmind-web \
      --image="$REGISTRY/web" --region="$REGION" \
      --cpu=1 --memory=256Mi \
      --min-instances=1 --max-instances=3 \
      --allow-unauthenticated
fi

# ── IAM: service-to-service auth ──────────────────────
if [ ${#SERVICES[@]} -eq 0 ]; then
  gcloud run services add-iam-policy-binding sketchmind-renderer \
      --region="$REGION" --member="serviceAccount:$SA" --role="roles/run.invoker"
  gcloud run services add-iam-policy-binding sketchmind-agents \
      --region="$REGION" --member="serviceAccount:$SA" --role="roles/run.invoker"
  gcloud run services add-iam-policy-binding sketchmind-mcp \
      --region="$REGION" --member="serviceAccount:$SA" --role="roles/run.invoker"
fi

# ── Summary ───────────────────────────────────────────
WEB_URL=$(get_url sketchmind-web)

echo ""
echo "=============================="
echo "  SketchMind deployed!"
echo "  Web:      $WEB_URL"
echo "  API:      $API_URL"
echo "  Agents:   $AGENTS_URL"
echo "  Renderer: $RENDER_URL"
echo "  MCP:      $MCP_URL"
echo "  DB:       $SQL_INSTANCE_CONNECTION"
echo "=============================="
