#!/usr/bin/env bash
# Start the infrastructure services (Postgres, Valkey, SeaweedFS) and wait until healthy.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] || ./scripts/init-env.sh
docker compose pull --quiet postgres valkey seaweedfs
docker compose up --detach --wait --wait-timeout 180 postgres valkey seaweedfs
docker compose ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'
