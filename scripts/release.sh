#!/usr/bin/env bash
# One-command release: commit, push, build the image, deploy to Cloud Run, tag.
#
#   scripts/release.sh "commit message"          # version read from pyproject.toml
#   scripts/release.sh --no-git                  # build + deploy only
#   scripts/release.sh --no-deploy "message"     # commit + push + tag only
#
# Deployment coordinates come from deploy.env (git-ignored) next to this repo's
# root, or from the environment:
#
#   GCP_PROJECT=my-project
#   GCP_REGION=europe-west1
#   AR_REPO=mcp                 # Artifact Registry repository name
#   SERVICE=apple-ads-mcp       # Cloud Run service name
#
# Nothing secret is needed here: the service keeps its env vars and secret
# bindings across deploys, so only the image tag changes.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DO_GIT=1
DO_DEPLOY=1
MSG=""
for arg in "$@"; do
  case "$arg" in
    --no-git) DO_GIT=0 ;;
    --no-deploy) DO_DEPLOY=0 ;;
    *) MSG="$arg" ;;
  esac
done

[ -f deploy.env ] && set -a && . ./deploy.env && set +a
: "${GCP_PROJECT:?set GCP_PROJECT in deploy.env or the environment}"
: "${GCP_REGION:=europe-west1}"
: "${AR_REPO:=mcp}"
: "${SERVICE:=apple-ads-mcp}"

VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)"
[ -n "$VERSION" ] || { echo "could not read version from pyproject.toml" >&2; exit 1; }
IMAGE="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT}/${AR_REPO}/${SERVICE}:${VERSION}"

echo "==> version $VERSION"
python3 -m unittest discover tests >/dev/null && echo "==> tests OK"

if [ "$DO_GIT" = 1 ]; then
  rm -f .git/index.lock
  if [ -n "$(git status --porcelain)" ]; then
    [ -n "$MSG" ] || { echo "commit message required (or use --no-git)" >&2; exit 1; }
    git add -A
    git commit -q -m "$MSG"
    echo "==> committed"
  else
    echo "==> working tree clean, nothing to commit"
  fi
  git push
  if ! git rev-parse -q --verify "refs/tags/v${VERSION}" >/dev/null; then
    git tag "v${VERSION}"
    git push origin "v${VERSION}"
    echo "==> tagged v${VERSION}"
  fi
fi

if [ "$DO_DEPLOY" = 1 ]; then
  echo "==> building $IMAGE"
  gcloud builds submit --project "$GCP_PROJECT" --tag "$IMAGE" --quiet
  echo "==> deploying $SERVICE ($GCP_REGION)"
  gcloud run deploy "$SERVICE" --project "$GCP_PROJECT" --region "$GCP_REGION" --image "$IMAGE" --quiet
  echo "==> live: $(gcloud run services describe "$SERVICE" --project "$GCP_PROJECT" --region "$GCP_REGION" --format='value(status.url)')/health"
fi
