#!/usr/bin/env bash
set -euo pipefail

# Editable-install the Python packages that live in this meta-repo.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

pip install -e "$REPO_ROOT/packages/python/agent_runtime_core"
pip install -e "$REPO_ROOT/packages/python/django_agent_runtime"
pip install -e "$REPO_ROOT/products/parrot/parrot-django"
pip install -e "$REPO_ROOT/packages/python/django_agent_studio"
