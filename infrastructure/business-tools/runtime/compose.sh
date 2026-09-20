#!/bin/bash
set -euo pipefail
umask 077
[[ $EUID -eq 0 ]] || { printf '%s\n' 'Compose wrapper requires root.' >&2; exit 1; }

# Never source env files or print resolved configuration. Raw secret env files
# are referenced by the fragments, NOT supplied as Compose's CLI --env-file.
cd /opt/business-tools
export COMPOSE_DISABLE_ENV_FILE=1
unset COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_ENV_FILES
exec /usr/bin/docker compose --project-name business-tools \
    --file /opt/business-tools/compose.yaml \
    --file /opt/business-tools/agentic-social/compose.yaml \
    --file /opt/business-tools/mautic/compose.yaml \
    --file /opt/business-tools/actual-budget/compose.yaml \
    --file /opt/business-tools/invoice-ninja/compose.yaml \
    --file /opt/business-tools/observability/compose.yaml "$@"