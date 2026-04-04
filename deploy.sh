#!/bin/bash
set -e
export PROJECT_ID=$(gcloud config get-value project)
export REGION="asia-south1"
export REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/sketchmind"

echo "=== Building & deploying all 4 services ==="

# 1. Renderer (build first - takes longest)
echo ">>> Renderer..."
cd services/renderer
gcloud builds submit --tag $REGISTRY/renderer --timeout=1200
gcloud run deploy sketchmind-renderer \
    --image=$REGISTRY/renderer --region=$REGION \
    --cpu=2 --memory=2Gi --timeout=300 \
    --concurrency=1 --min-instances=1 --max-instances=5 \
    --set-env-vars="GCS_BUCKET=${PROJECT_ID}-sketchmind-videos" \
    --no-allow-unauthenticated
cd ../..

RENDER_URL=$(gcloud run services describe sketchmind-renderer \
    --region=$REGION --format="value(status.url)")

# 2. Agents
echo ">>> Agents..."
cd services/agents
gcloud builds submit --tag $REGISTRY/agents
gcloud run deploy sketchmind-agents \
    --image=$REGISTRY/agents --region=$REGION \
    --cpu=1 --memory=512Mi --timeout=300 \
    --min-instances=1 --max-instances=3 \
    --set-env-vars="RENDER_SERVICE_URL=$RENDER_URL,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=$REGION" \
    --no-allow-unauthenticated
cd ../..

AGENTS_URL=$(gcloud run services describe sketchmind-agents \
    --region=$REGION --format="value(status.url)")

# 3. API
echo ">>> API..."
cd services/api
gcloud builds submit --tag $REGISTRY/api
gcloud run deploy sketchmind-api \
    --image=$REGISTRY/api --region=$REGION \
    --cpu=1 --memory=512Mi --timeout=300 \
    --min-instances=1 --max-instances=3 \
    --vpc-connector=sketchmind-vpc \
    --set-env-vars="AGENTS_SERVICE_URL=$AGENTS_URL,GCP_PROJECT_ID=$PROJECT_ID,ALLOYDB_HOST=$ALLOYDB_HOST,ALLOYDB_PASS=$ALLOYDB_PASS,ALLOYDB_DB=sketchmind" \
    --allow-unauthenticated
cd ../..

API_URL=$(gcloud run services describe sketchmind-api \
    --region=$REGION --format="value(status.url)")

# 4. Web
echo ">>> Web..."
cd services/web
# Update API URL in Next.js env
echo "NEXT_PUBLIC_API_URL=$API_URL" > .env.production
gcloud builds submit --tag $REGISTRY/web
gcloud run deploy sketchmind-web \
    --image=$REGISTRY/web --region=$REGION \
    --cpu=1 --memory=256Mi \
    --min-instances=0 --max-instances=3 \
    --allow-unauthenticated
cd ../..

# 5. IAM bindings (service-to-service auth)
SA=$(gcloud iam service-accounts list \
    --filter="displayName:Compute Engine" --format="value(email)")
gcloud run services add-iam-policy-binding sketchmind-renderer \
    --region=$REGION --member="serviceAccount:$SA" --role="roles/run.invoker"
gcloud run services add-iam-policy-binding sketchmind-agents \
    --region=$REGION --member="serviceAccount:$SA" --role="roles/run.invoker"

WEB_URL=$(gcloud run services describe sketchmind-web \
    --region=$REGION --format="value(status.url)")

echo ""
echo "=============================="
echo "  SketchMind deployed!"
echo "  Web:      $WEB_URL"
echo "  API:      $API_URL"
echo "  Agents:   $AGENTS_URL"
echo "  Renderer: $RENDER_URL"
echo "=============================="
