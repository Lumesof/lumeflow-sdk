#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"

cd "${REPO_ROOT}"
echo "==> Publishing ingest DAG operators..."
bazel run //example_apps/lumeflow_rag/ingest:async_ingest_light_graph_publish

echo "==> Submitting ingest DAG job..."
bazel run //example_apps/lumeflow_rag/ingest:launch_indexer_light_bin -- "$@"
