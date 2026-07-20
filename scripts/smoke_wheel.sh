#!/usr/bin/env bash
# Pre-publish smoke gate.
#
# Builds a package's wheel, installs THE BUILT WHEEL into a fresh, empty venv
# (pulling dependencies — including private ones — from the agents registry),
# and imports it. For django_agent_runtime it also runs `manage.py check` style
# app loading. Fails loudly if the wheel is unimportable.
#
# This catches packaging mistakes that an in-tree test run cannot: a subpackage
# missing from the build's `packages` list, a dependency whose required version
# isn't published yet, etc. (e.g. the 0.17.0 billing / sovereignty breaks).
#
# Usage:   scripts/smoke_wheel.sh <package_dir> <top_import_module>
# Example: scripts/smoke_wheel.sh django_agent_runtime django_agent_runtime
#          scripts/smoke_wheel.sh agent_runtime_core   agent_runtime_core
#
# Run this BEFORE `twine upload`. bump_deploy_push.py should invoke it as a gate.
set -euo pipefail

PKG_DIR="${1:?package dir required}"
TOP_MODULE="${2:?top import module required}"
REGISTRY="https://europe-west2-python.pkg.dev/devpi-mmd/agents/simple/"

cd "$PKG_DIR"
echo ">> building $PKG_DIR"
rm -rf dist build
python3 -m build >/dev/null
WHL=$(ls -1 dist/*.whl | sort -V | tail -1)
echo ">> built $WHL"

VENV="$(mktemp -d)/venv"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip keyrings.google-artifactregistry-auth
echo ">> installing wheel into clean venv (deps via agents registry)"
"$VENV/bin/pip" install -q "$WHL" --extra-index-url "$REGISTRY"

echo ">> importing $TOP_MODULE"
"$VENV/bin/python" -c "import $TOP_MODULE; print('   import OK:', $TOP_MODULE.__name__)"

if [ "$TOP_MODULE" = "django_agent_runtime" ]; then
  WORK="$(mktemp -d)"
  cat > "$WORK/smoke_settings.py" <<'PY'
SECRET_KEY = "smoke"
INSTALLED_APPS = ["django.contrib.contenttypes", "django.contrib.auth",
                  "rest_framework", "django_agent_runtime"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
PY
  echo ">> django system check (apps + models + migrations load)"
  ( cd "$WORK" && DJANGO_SETTINGS_MODULE=smoke_settings "$VENV/bin/python" -c \
      "import django; django.setup(); from django.core.management import call_command; call_command('check')" )
fi

echo "SMOKE GATE PASSED: $WHL"
