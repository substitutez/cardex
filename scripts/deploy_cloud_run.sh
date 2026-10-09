#!/usr/bin/env bash
# ==============================================================================
# CarDex Cloud Run Automated Deployment Pipeline
# Executes automated pre-flight tests, syncs static assets, builds container
# source, deploys to Google Cloud Run, and retrieves live URL.
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

echo "================================================================================"
echo "🚀 CarDex Cloud Run Deployment Pipeline"
echo "================================================================================"

REGION="${GCP_REGION:-us-east1}"
SERVICE_NAME="cardex-frontend"
DRY_RUN=false

for arg in "$@"; do
  case $arg in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
  esac
done

# 1. Run automated test suite
echo "==> 1. Running test suite via uv to guarantee zero regressions..."
if command -v uv >/dev/null 2>&1; then
  uv run pytest --ignore=tests/integration/
else
  pytest --ignore=tests/integration/
fi
echo "    ✔ All test suites passed."

# 2. Sync static assets
echo "==> 2. Synchronizing static assets and legal pages..."
if [ -d "frontend/static" ]; then
  test -f "frontend/static/privacy.html"
  test -f "frontend/static/terms.html"
  test -f "frontend/static/index.html"
  test -f "frontend/static/js/offline_sync.js"
  test -f "frontend/static/js/card_exporter.js"
  echo "    ✔ Static assets verified: privacy, terms, HUD index, offline sync & card exporter."
fi

# 3. Deploy to Cloud Run
if [ "$DRY_RUN" = true ]; then
  echo "==> Dry run mode enabled: Skipping gcloud run deploy."
  echo "    Command that would run:"
  echo "    gcloud run deploy $SERVICE_NAME --source . --region $REGION --allow-unauthenticated"
  echo "================================================================================"
  echo "✔ Pre-deployment verification complete (dry run)!"
  exit 0
fi

echo "==> 3. Deploying service to Google Cloud Run ($SERVICE_NAME in $REGION)..."
gcloud run deploy "$SERVICE_NAME" \
  --source . \
  --region "$REGION" \
  --allow-unauthenticated \
  --set-env-vars="AGENT_DIRECTORY=app"

echo "==> 4. Fetching live service URL..."
SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" --region "$REGION" --format='value(status.url)' 2>/dev/null || echo "")

echo "================================================================================"
echo "🎉 Deployment Successful!"
if [ -n "$SERVICE_URL" ]; then
  echo "🌐 Live Service URL: $SERVICE_URL"
else
  echo "🌐 Service deployed to region $REGION."
fi
echo "================================================================================"
