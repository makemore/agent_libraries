#!/usr/bin/env bash
set -euo pipefail

# Clone every sub-repo in this meta-repo into its target directory.
# Idempotent: skips a directory that already has a .git folder.

REPOS=(
  # url                                    target-dir
  "https://github.com/makemore/chisel.git                         chisel"
  "https://github.com/makemore/django_chisel.git                  django_chisel"
  "https://github.com/makemore/parrot.git                         parrot"
  "https://github.com/makemore/agent-docs.git                     docs"
  # clients/ — parent must exist first
  "https://github.com/makemore/agent-frontend.git                 clients/agent-frontend"
  "https://github.com/makemore/agent-android.git                  clients/agent-android"
  "https://github.com/makemore/agent-unity.git                    clients/agent-unity"
  "https://github.com/makemore/agent-client.git                   clients/agent-client"
  "https://github.com/makemore/agent-ios.git                      clients/agent-ios"
  # agent/ — parent must exist first
  "https://github.com/makemore/agent-runtime-core.git             agent/agent_runtime_core"
  "https://github.com/makemore/agent_studio.git                   agent/agent_studio"
  "https://github.com/makemore/django-agent-runtime.git           agent/django_agent_runtime"
  "https://github.com/makemore/django_agent_studio.git            agent/django_agent_studio"
)

mkdir -p clients agent

for entry in "${REPOS[@]}"; do
  url="${entry%% *}"
  dir="${entry##* }"
  if [ -d "$dir/.git" ]; then
    echo "skip  $dir  (already cloned)"
  else
    echo "clone $url  ->  $dir"
    git clone "$url" "$dir"
  fi
done
