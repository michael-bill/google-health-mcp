#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ "${1:-}" != --updated ]; then
    [ "$#" -le 1 ] || { echo "Usage: ./deploy.sh [branch]" >&2; exit 1; }
    branch=${1:-main}
    git check-ref-format --branch "$branch" >/dev/null
    git fetch origin
    git switch "$branch"
    git pull --ff-only origin "$branch"
    exec ./deploy.sh --updated
fi
[ "$#" -eq 1 ] || exit 1
set -a
. ./.env
HEALTH_RELEASE=$(git rev-parse HEAD)
set +a
docker network inspect health-backend >/dev/null 2>&1 || docker network create --driver overlay health-backend
docker build -t "google-health-mcp:$HEALTH_RELEASE" .
docker stack deploy --detach=false --resolve-image never -c deploy/stack.yaml google-health
image=$(docker service inspect google-health_mcp -f '{{.Spec.TaskTemplate.ContainerSpec.Image}}')
[ "${image%@*}" = "google-health-mcp:$HEALTH_RELEASE" ] || { echo "Deployment rolled back to $image" >&2; exit 1; }
