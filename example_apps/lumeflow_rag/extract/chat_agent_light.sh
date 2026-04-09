#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"

cd "${REPO_ROOT}"
DEFAULT_CHAT_LIGHT_TIMEOUT_MS="${DEFAULT_CHAT_LIGHT_TIMEOUT_MS:-300000}"
echo "==> Publishing agent DAG operators..."
bazel run //example_apps/lumeflow_rag/extract:sync_agent_light_graph_publish

echo "==> Submitting agent DAG job..."
bazel run //example_apps/lumeflow_rag/extract:chat_agent_light_bin -- \
  --timeout-ms="${DEFAULT_CHAT_LIGHT_TIMEOUT_MS}" \
  "$@"
