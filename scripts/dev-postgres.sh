#!/usr/bin/env bash
# Local PostgreSQL 18 (UBI 9) for Dear Agent development, matching the in-cluster StatefulSet
# (deploy/components/postgresql). The mailbox is ingress; Postgres is the durable queue
# (ADR 0005). Data lives in a named volume, so `down` does not destroy it.
#
#   scripts/dev-postgres.sh up|down|logs|psql|status
#
# The image is Red Hat's; if your host is not logged in:
#   podman login registry.redhat.io
#
# Overridable: IMAGE, CONTAINER, VOLUME, PORT, POSTGRESQL_USER, POSTGRESQL_PASSWORD,
# POSTGRESQL_DATABASE.
set -euo pipefail

IMAGE="${IMAGE:-registry.redhat.io/rhel9/postgresql-18:latest}"
CONTAINER="${CONTAINER:-dear-agent-postgres}"
VOLUME="${VOLUME:-dear-agent-postgres-data}"
PORT="${PORT:-5432}"

# Development-only defaults. Override with real values; never reuse them outside dev.
POSTGRESQL_USER="${POSTGRESQL_USER:-dear-agent}"
POSTGRESQL_PASSWORD="${POSTGRESQL_PASSWORD:-dear-agent}"
POSTGRESQL_DATABASE="${POSTGRESQL_DATABASE:-dear-agent}"

command="${1:-up}"

case "$command" in
  up)
    if podman container exists "$CONTAINER"; then
      podman start "$CONTAINER"
    else
      podman run -d \
        --name "$CONTAINER" \
        -p "127.0.0.1:${PORT}:5432" \
        -e "POSTGRESQL_USER=${POSTGRESQL_USER}" \
        -e "POSTGRESQL_PASSWORD=${POSTGRESQL_PASSWORD}" \
        -e "POSTGRESQL_DATABASE=${POSTGRESQL_DATABASE}" \
        -e "POSTGRESQL_ADMIN_PASSWORD=${POSTGRESQL_PASSWORD}" \
        -v "${VOLUME}:/var/lib/pgsql/data:Z" \
        "$IMAGE"
    fi
    echo "DEAR_AGENT_DATABASE_URL=postgresql://${POSTGRESQL_USER}:${POSTGRESQL_PASSWORD}@127.0.0.1:${PORT}/${POSTGRESQL_DATABASE}"
    ;;
  down)
    podman rm -f "$CONTAINER" >/dev/null 2>&1 || true
    echo "removed $CONTAINER (volume $VOLUME kept)"
    ;;
  logs)
    podman logs -f "$CONTAINER"
    ;;
  psql)
    podman exec -it "$CONTAINER" psql -U "$POSTGRESQL_USER" -d "$POSTGRESQL_DATABASE"
    ;;
  status)
    podman ps --filter "name=^${CONTAINER}$"
    ;;
  *)
    echo "usage: $0 [up|down|logs|psql|status]" >&2
    exit 2
    ;;
esac
