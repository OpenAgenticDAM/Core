#!/usr/bin/env bash
# Start the whole stack (infra + migrate + gateway + worker) and wait until healthy.
# The app image is built only if missing or with --build; build cache is pruned afterwards
# because Colima's disk is a sparse file that does not give space back to macOS on its own.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] || ./scripts/init-env.sh

build=""
if [[ "${1:-}" == "--build" ]] || ! docker image inspect openagenticdam:dev >/dev/null 2>&1; then
  build="--build"
fi
# shellcheck disable=SC2086  # $build is either empty or a single flag
docker compose up --detach $build --wait --wait-timeout 240
if [[ -n "$build" ]]; then
  ./scripts/reclaim-disk.sh >/dev/null
fi
docker compose ps --all --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'
