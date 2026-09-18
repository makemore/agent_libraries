#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Initialize missing pinned repositories; never switch an existing checkout.
exec "${PYTHON:-python3}" "$REPO_ROOT/scripts/workspace.py" checkout