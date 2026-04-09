# Debugging a Lumeflow Application on a Lumeflow Cluster

Last Updated: April 9, 2026

This document is an expanded debugging guide for Lumeflow applications running on a Lumeflow cluster.
It is intentionally practical.
It is written for both humans and coding agents.
It focuses on repeatable workflows.
It includes direct commands.
It includes interpretation rules.
It includes operator-log and platform-log topology.
It includes failure-mode playbooks.

## 1) Why This Guide Exists

You can debug Lumeflow at different layers.
Those layers produce different logs.
Those logs are collected by different collectors.
If you mix these up, debugging is slow.
If you separate them clearly, debugging becomes mechanical.

This guide gives you that separation.

It also codifies intent-to-command mapping.
That means if someone says:

1. get me sidecar logs
2. check if operator X is receiving data
3. compare input and output message counts
4. why did StartJob fail
5. did we publish the updated operator image

A coding agent can translate that into exact commands and interpretations.

## 2) Scope

This guide covers:

1. local cluster debugging patterns used in Lumeflow workflows
2. control plane health checks
3. operator and sidecar log collection
4. OTEL topology and wiring checks
5. common launch and runtime failure modes
6. ChromaDB count and sizing checks
7. message-flow statistics from platform logs
8. user-defined operator logs and how they appear after rebuild/publish
9. incident-bundle collection
10. agent-friendly command recipes

This guide does not cover:

1. deep CRDB schema debugging
2. production SRE runbooks outside the local Lumeflow cluster model
3. cloud IAM policy design in depth

## 3) Core Topology

There are two OTEL collectors in the Lumeflow cluster.
They have different responsibilities.

### 3.1 Collector A: Platform Collector

Deployment:

1. `lumeflow-otel-collector`

Primary role:

1. collect logs from Lumeflow platform components

Examples of platform components:

1. flow-server
2. opnet-server
3. rpc-opnet-bridge
4. executor-control
5. config-query-server
6. sidecar-cas

Important note:

Platform logs include built-in runtime statistics.
These often include:

1. messages received count
2. messages published count
3. operator/port context
4. app/job correlation details

### 3.2 Collector B: Operator Collector

Deployment:

1. `operator-otel-collector`

Primary role:

1. collect logs from operator workloads and sidecars running in cartons

Important note:

Operators are user components.
Operator logging is user-controlled.
You can add any additional logs in operator code.
If your build/publish wiring is correct, new logs automatically appear in subsequent runs.

### 3.3 Why This Split Matters

When debugging:

1. use platform collector for routing, lifecycle, and message-transfer stats
2. use operator collector for business-level behavior inside operators

If you only look at one collector, you can miss half of the story.

## 4) Runtime Context Setup

Run from repo root.

```bash
cd /home/baba/lumesof
NS=lumeflow-local
```

Optional helper variables:

```bash
export NS=lumeflow-local
export PUBLIC_HOST="$(minikube ip)"
export K="kubectl -n ${NS}"
```

With aliases:

```bash
alias k="kubectl -n ${NS}"
alias kpods='kubectl -n "${NS}" get pods'
alias kevents='kubectl -n "${NS}" get events --sort-by=.lastTimestamp'
```

## 5) Quick Start Debug Workflow

When an issue is reported, run this sequence first.

1. verify package floor
2. verify cluster health
3. verify both collectors are running
4. pull platform collector logs
5. pull operator collector logs
6. isolate app/job/operator identifiers
7. compute flow stats from logs
8. decide next focused probe

### 5.1 Verify Package Floor

```bash
apt-cache policy \
  lumesof-rpath \
  cartond-non-lumesof-local \
  lumeflow-local-runtime \
  lumeflow-local-smoketest
```

Expected minimum versions:

1. `lumesof-rpath >= 1.0.2`
2. `cartond-non-lumesof-local >= 1.0.1`
3. `lumeflow-local-runtime >= 1.0.2`
4. `lumeflow-local-smoketest >= 1.0.3`

### 5.2 Verify Core Services

```bash
kubectl -n "$NS" get pods
kubectl -n "$NS" get events --sort-by=.lastTimestamp | tail -n 80
```

```bash
kubectl -n "$NS" rollout status deploy/flow-db --timeout=5m
kubectl -n "$NS" rollout status deploy/config-query-server --timeout=5m
kubectl -n "$NS" rollout status deploy/opnet-server --timeout=5m
kubectl -n "$NS" rollout status deploy/executor-control --timeout=5m
kubectl -n "$NS" rollout status deploy/sidecar-cas --timeout=5m
kubectl -n "$NS" rollout status deploy/flow-server --timeout=5m
kubectl -n "$NS" rollout status deploy/rpc-opnet-bridge-shard0-replica0 --timeout=5m
kubectl -n "$NS" rollout status deploy/rpc-opnet-bridge-shard0-replica1 --timeout=5m
```

### 5.3 Verify Collector Health

```bash
kubectl -n "$NS" rollout status deploy/lumeflow-otel-collector --timeout=5m
kubectl -n "$NS" rollout status deploy/operator-otel-collector --timeout=5m
kubectl -n "$NS" get pods | rg 'otel-collector'
```

### 5.4 Pull Baseline Logs

Platform logs:

```bash
kubectl -n "$NS" logs deploy/lumeflow-otel-collector --since=20m --tail=2000
```

Operator and sidecar logs:

```bash
kubectl -n "$NS" logs deploy/operator-otel-collector --since=20m --tail=2000
```

## 6) Distinguishing Log Sources Correctly

### 6.1 Platform Log Traits

Platform logs often contain:

1. job status transitions
2. launch failures
3. opnet connection and transfer behavior
4. message movement counters
5. bridge routing behavior

Common search keywords:

1. `flow`
2. `opnet`
3. `bridge`
4. `LAUNCH_FAILED`
5. `JOB_STATUS_`
6. `received`
7. `published`

### 6.2 Operator Log Traits

Operator logs often contain:

1. business payload summaries
2. batch sizes
3. document counts
4. hash-based uniqueness counters
5. model invocation details
6. chunking/indexing diagnostics

Common search keywords:

1. operator class names
2. custom stat prefixes
3. `batch_size`
4. `unique_documents`
5. `chunk_count`
6. `item_count`

### 6.3 Sidecars Are Not Pods

Important model:

1. `sidecar-cas` is a Kubernetes deployment with pod logs
2. operators and sidecars for DAG workloads run in cartons
3. carton workload logs are emitted via OTEL collectors

Implication:

If a user says "get me sidecar logs", do not default to `kubectl logs pod/<operator>`.
Use operator collector logs first.

## 7) Intent To Command Mapping (Agent-Centric)

This section is explicitly designed for coding-agent execution.

### 7.1 Intent: "get me sidecar logs"

Use:

```bash
kubectl -n "$NS" rollout status deploy/operator-otel-collector --timeout=5m
kubectl -n "$NS" logs deploy/operator-otel-collector --since=30m --tail=5000
```

If scope is unknown, enrich with labels if present in logs:

```bash
kubectl -n "$NS" logs deploy/operator-otel-collector --since=30m --tail=10000 \
  | rg -i 'lumesof\.app_id|lumesof\.operator_name|lumesof\.instance_id|error|exception'
```

Agent interpretation:

1. if no output, verify collector health and wiring
2. if output exists, extract operator/app identifiers
3. summarize by operator and error signature

### 7.2 Intent: "get me platform logs"

Use:

```bash
kubectl -n "$NS" rollout status deploy/lumeflow-otel-collector --timeout=5m
kubectl -n "$NS" logs deploy/lumeflow-otel-collector --since=30m --tail=5000
```

Focused:

```bash
kubectl -n "$NS" logs deploy/lumeflow-otel-collector --since=30m --tail=5000 \
  | rg -i 'flow|opnet|bridge|launch_failed|job_status|error|exception'
```

### 7.3 Intent: "look into operator X logs"

Use:

```bash
OPERATOR="<operator-name-or-fragment>"
kubectl -n "$NS" logs deploy/operator-otel-collector --since=60m --tail=20000 \
  | rg -i "${OPERATOR}|lumesof\.operator_name"
```

Add stats-focused filters:

```bash
kubectl -n "$NS" logs deploy/operator-otel-collector --since=60m --tail=20000 \
  | rg -i "${OPERATOR}|batch|count|size|unique|hash|chunk|document"
```

### 7.4 Intent: "how many messages transferred between operators"

Use platform collector logs first.

```bash
kubectl -n "$NS" logs deploy/lumeflow-otel-collector --since=60m --tail=50000 \
  | rg -i 'received|published|operator|port|lumesof\.operator_name|lumesof\.port_name'
```

Then aggregate with `awk` or `sort | uniq -c` depending on format.

Example generic aggregation:

```bash
kubectl -n "$NS" logs deploy/lumeflow-otel-collector --since=60m --tail=50000 \
  | rg -i 'received|published' \
  | sort | uniq -c | sort -nr | head -n 100
```

Agent interpretation:

1. identify ingress count at each operator
2. compare published count from upstream with received count downstream
3. highlight divergences and possible retries/fanout

### 7.5 Intent: "is the same document being sent multiple times"

Use operator logs where hash/unique metrics were added.

```bash
kubectl -n "$NS" logs deploy/operator-otel-collector --since=60m --tail=50000 \
  | rg -i 'unique|hash|document|batch|seen'
```

Interpretation:

1. if total docs grows faster than unique docs, duplicates exist
2. if unique docs track total docs closely, duplication is low

### 7.6 Intent: "why did StartJob fail"

Use platform collector logs.

```bash
kubectl -n "$NS" logs deploy/lumeflow-otel-collector --since=60m --tail=20000 \
  | rg -i 'StartJob|LAUNCH_FAILED|DockerLaunchError|Unauthenticated|downloadArtifacts|JOB_STATUS'
```

If GAR auth suspected:

```bash
gcloud auth application-default login
gcloud auth configure-docker us-central1-docker.pkg.dev
```

### 7.7 Intent: "check ChromaDB item count"

```bash
PUBLIC_HOST="$(minikube ip)"
BASE="http://${PUBLIC_HOST}:30090/api/v2/tenants/default_tenant/databases/default_database"

curl -sS "${BASE}/collections"
CID="$(curl -sS "${BASE}/collections" | rg -o '"id":"[^"]+"' -m1 | cut -d'"' -f4)"
echo "CID=${CID}"
curl -sS "${BASE}/collections/${CID}/count"
```

Live watch:

```bash
watch -n 2 "curl -sS \"${BASE}/collections/${CID}/count\""
```

### 7.8 Intent: "are logs missing because OTEL wiring is broken"

```bash
kubectl -n "$NS" get svc operator-otel-collector lumeflow-otel-collector -o wide
minikube ip
```

Expected NodePorts:

1. operator OTLP gRPC: `30317`
2. operator fluentforward: `30424`
3. platform OTLP gRPC: `30318`

If missing logs:

1. verify both collectors running
2. verify target endpoints in injected config
3. verify cartons can reach NodePorts via minikube IP

### 7.9 Intent: "I need a ready-to-paste incident bundle"

```bash
minikube status
kubectl -n "$NS" get pods
kubectl -n "$NS" get events --sort-by=.lastTimestamp | tail -n 200
kubectl -n "$NS" logs deploy/operator-otel-collector --since=30m
kubectl -n "$NS" logs deploy/lumeflow-otel-collector --since=30m
journalctl -u cartond-local -n 500 --no-pager
apt-cache policy lumesof-rpath cartond-non-lumesof-local lumeflow-local-runtime lumeflow-local-smoketest
```

## 8) Baseline Command Library

This section duplicates key commands by function so agents can copy and run quickly.

### 8.1 Cluster bring-up smoke flow

```bash
lumeflow-smoketest --start-cluster
lumeflow-smoketest --run-even-odd-dag
lumeflow-smoketest --teardown-cluster
```

### 8.2 Core service log tails

```bash
kubectl -n "$NS" logs deploy/flow-server -f --tail=200
kubectl -n "$NS" logs deploy/executor-control -f --tail=200
kubectl -n "$NS" logs deploy/config-query-server -f --tail=200
kubectl -n "$NS" logs deploy/opnet-server -f --tail=200
kubectl -n "$NS" logs deploy/rpc-opnet-bridge-shard0-replica0 -f --tail=200
kubectl -n "$NS" logs deploy/rpc-opnet-bridge-shard0-replica1 -f --tail=200
kubectl -n "$NS" logs deploy/sidecar-cas -f --tail=200
```

### 8.3 Collector log tails

```bash
kubectl -n "$NS" logs deploy/operator-otel-collector -f --tail=500
kubectl -n "$NS" logs deploy/lumeflow-otel-collector -f --tail=500
```

### 8.4 Collector log scans

```bash
kubectl -n "$NS" logs deploy/operator-otel-collector --tail=2000 \
  | rg -i 'error|exception|lumesof\.app_id|lumesof\.operator_name|lumesof\.instance_id'
```

```bash
kubectl -n "$NS" logs deploy/lumeflow-otel-collector --tail=2000 \
  | rg -i 'error|exception|flow|opnet|rpc'
```

### 8.5 Host runtime checks

```bash
systemctl status cartond-local --no-pager
journalctl -u cartond-local -n 300 --no-pager
/lumesof/bin/carton info
docker info
id -nG | tr ' ' '\n' | rg '^docker$' || echo "user is not in docker group"
```

### 8.6 Carton executable diagnosis

```bash
file /lumesof/bin/carton
readelf -d /lumesof/bin/carton | rg 'RUNPATH|NEEDED'
```

## 9) Understanding Message Statistics

The platform components emit useful statistics.
These are your first source for "how many messages moved where".

What to extract:

1. operator name
2. ingress port name
3. count received
4. egress port name
5. count published
6. app/job correlation fields

### 9.1 Generic extraction strategy

Step 1: pull logs in time window.

```bash
kubectl -n "$NS" logs deploy/lumeflow-otel-collector --since=45m --tail=100000 > /tmp/lumeflow_platform_logs.txt
```

Step 2: pull candidate lines.

```bash
rg -i 'received|published|operator|port|message' /tmp/lumeflow_platform_logs.txt > /tmp/lumeflow_message_stats_candidates.txt
```

Step 3: inspect a sample.

```bash
sed -n '1,80p' /tmp/lumeflow_message_stats_candidates.txt
```

Step 4: build targeted regex from real log shape.

```bash
rg -i 'lumesof\.operator_name|lumesof\.port_name|received|published' /tmp/lumeflow_message_stats_candidates.txt
```

### 9.2 Count consistency heuristics

Use these heuristics before blaming operators.

1. If downstream receives more than upstream publishes, check retries/replays.
2. If upstream publishes more than downstream receives, check link/consumer connectivity.
3. If counts differ by exact fanout factor, that may be expected topology behavior.
4. If ingress count is low but sink count is high, inspect chunking/splitting stage.
5. If unique document count is low but item count is high, duplication is likely.

## 10) Operator Logging Strategy

Operator logs are user-defined.
You can log anything useful.

Recommended operator log categories:

1. batch size in
2. batch size out
3. per-document size list
4. number of chunks produced
5. unique document count
6. hash or document ID summary
7. external-call latency summary
8. retry attempts and outcomes

### 10.1 Example stat lines to add in operators

The exact implementation can vary.
The key is consistency.

Examples of log messages:

1. `batch_in_count=112`
2. `batch_out_count=3571`
3. `doc_sizes_bytes=1024,980,1002,995`
4. `unique_docs_seen=112`
5. `chunks_per_doc=32,27,35,30`

### 10.2 Why these logs matter

These logs answer common debugging questions quickly:

1. Are duplicates entering the indexer?
2. Is chunker expanding unexpectedly?
3. Are document sizes causing skew?
4. Is the same source reprocessed repeatedly?

## 11) Bazel Workflow And Log Propagation

This is critical.

If operator logs are added in code, they only appear at runtime when the new operator image is actually used.

Correctly wired Bazel workflow ensures this happens automatically.

### 11.1 Expected flow

1. edit operator code
2. build graph/operator targets
3. run graph publish target
4. deploy/run graph
5. inspect collector logs

### 11.2 Why this works

`lore_operator` and `lore_graph` packaging/publish flow binds the latest code into published artifacts.
When the next run launches using those artifacts, your new log statements execute.

### 11.3 Sanity checks when logs do not appear

1. confirm code change exists in the branch/worktree used for build
2. confirm build succeeded
3. confirm publish step succeeded
4. confirm new run used the updated artifacts
5. confirm you are looking at operator collector logs for operator code logs

### 11.4 Sample build and publish commands

```bash
bazel build //lumecode/apps/lumeflow_rag/ingest:async_ingest_light_graph
bazel run //lumecode/apps/lumeflow_rag/ingest:async_ingest_light_graph_publish
```

For extract graph:

```bash
bazel build //lumecode/apps/lumeflow_rag/extract:sync_agent_light_graph
bazel run //lumecode/apps/lumeflow_rag/extract:sync_agent_light_graph_publish
```

## 12) Flow Failure Playbooks

Each playbook follows the same format:

1. symptom
2. commands
3. interpretation
4. next action

### 12.1 Playbook: StartJob LAUNCH_FAILED with GAR 403

Symptom:

1. job reaches created state
2. start fails at launch
3. logs show unauthenticated download artifacts error

Commands:

```bash
kubectl -n "$NS" logs deploy/lumeflow-otel-collector --since=30m \
  | rg -i 'DockerLaunchError|Unauthenticated request|downloadArtifacts|LAUNCH_FAILED'
```

Fix:

```bash
gcloud auth application-default login
gcloud auth configure-docker us-central1-docker.pkg.dev
```

Redeploy example:

```bash
PUBLIC_HOST="$(minikube ip)"
./lumecode/lumeflow/runtime/v1/demo/deploy_cluster.sh \
  --context=minikube \
  --namespace=lumeflow-local \
  --public-host="${PUBLIC_HOST}" \
  --executor-cartond-target=tcp://host.minikube.internal:30001 \
  --profile=local \
  --oci-registry-type=gcr \
  --build \
  --clean \
  --include-chromadb
```

### 12.2 Playbook: Missing operator logs

Symptom:

1. platform logs visible
2. operator collector empty or missing expected operator lines

Commands:

```bash
kubectl -n "$NS" rollout status deploy/operator-otel-collector --timeout=5m
kubectl -n "$NS" get svc operator-otel-collector -o wide
minikube ip
kubectl -n "$NS" logs deploy/operator-otel-collector --since=30m --tail=5000
```

Interpretation:

1. if collector down, fix deployment first
2. if collector up but no logs, inspect OTEL endpoint config
3. if logs exist but missing expected strings, verify operator image update path

### 12.3 Playbook: Too many indexed items vs ingress count

Symptom:

1. indexer ingress seems small
2. index item count is much larger

Commands:

```bash
kubectl -n "$NS" logs deploy/lumeflow-otel-collector --since=60m --tail=50000 \
  | rg -i 'received|published|indexer|chunker|structured_extractor'
```

```bash
kubectl -n "$NS" logs deploy/operator-otel-collector --since=60m --tail=50000 \
  | rg -i 'batch|chunk|document|unique|hash|index'
```

```bash
PUBLIC_HOST="$(minikube ip)"
BASE="http://${PUBLIC_HOST}:30090/api/v2/tenants/default_tenant/databases/default_database"
CID="$(curl -sS "${BASE}/collections" | rg -o '"id":"[^"]+"' -m1 | cut -d'"' -f4)"
curl -sS "${BASE}/collections/${CID}/count"
```

Interpretation:

1. high expansion can be expected with fine chunking
2. huge expansion with stable unique docs suggests splitting logic or duplication
3. compare unique-doc stats to item count

### 12.4 Playbook: Unknown consumer / ghost node noise

Symptom:

1. opnet-server logs unknown consumer
2. sidecar heartbeat errors mention node not found

Commands:

Connected node IDs:

```bash
kubectl -n "$NS" logs deploy/opnet-server --since=30m \
  | rg 'NodeCollection async_connect: success' \
  | rg -o 'node_id=[0-9a-f-]{36}' \
  | cut -d= -f2 \
  | sort -u
```

Polling node IDs:

```bash
kubectl -n "$NS" logs deploy/opnet-server --since=30m \
  | rg -o 'port_name=[0-9a-f-]{36}:[a-z_]+' \
  | sed -E 's/port_name=//' \
  | cut -d: -f1 \
  | sort -u
```

Interpretation:

1. polling IDs not in connected set are often zombies from older runs
2. focus debugging on connected node IDs for the current session

## 13) ChromaDB Debugging Workflow

### 13.1 Pod and storage health

```bash
kubectl -n "$NS" get pods -l app=chromadb -o wide
kubectl -n "$NS" exec chromadb-0 -- du -sh /data
kubectl -n "$NS" exec chromadb-0 -- sh -lc "du -h /data | sort -h | tail -n 20"
```

### 13.2 Collection and count

```bash
PUBLIC_HOST="$(minikube ip)"
BASE="http://${PUBLIC_HOST}:30090/api/v2/tenants/default_tenant/databases/default_database"

curl -sS "${BASE}/collections"
CID="$(curl -sS "${BASE}/collections" | rg -o '"id":"[^"]+"' -m1 | cut -d'"' -f4)"
echo "CID=${CID}"

curl -sS "${BASE}/collections/${CID}/count"
```

### 13.3 Continuous monitoring during ingestion

```bash
watch -n 2 "curl -sS \"${BASE}/collections/${CID}/count\""
```

### 13.4 Multiple collections vs too many items

Separate these cases clearly:

1. too many collections means collection lifecycle issue
2. too many items in one collection means ingestion/chunking/duplication issue

## 14) Control Plane Health Deep Checks

### 14.1 Pod view

```bash
kubectl -n "$NS" get pods -o wide
```

### 14.2 Event timeline

```bash
kubectl -n "$NS" get events --sort-by=.lastTimestamp | tail -n 200
```

### 14.3 Deployment rollouts

```bash
kubectl -n "$NS" rollout status deploy/flow-db --timeout=5m
kubectl -n "$NS" rollout status deploy/config-query-server --timeout=5m
kubectl -n "$NS" rollout status deploy/opnet-server --timeout=5m
kubectl -n "$NS" rollout status deploy/executor-control --timeout=5m
kubectl -n "$NS" rollout status deploy/sidecar-cas --timeout=5m
kubectl -n "$NS" rollout status deploy/flow-server --timeout=5m
kubectl -n "$NS" rollout status deploy/rpc-opnet-bridge-shard0-replica0 --timeout=5m
kubectl -n "$NS" rollout status deploy/rpc-opnet-bridge-shard0-replica1 --timeout=5m
```

### 14.4 Fast failure scan

```bash
kubectl -n "$NS" get pods | rg -i 'error|crash|backoff|pending|imagepull'
```

## 15) Host Runtime Health (Cartond + Docker)

### 15.1 Cartond service

```bash
systemctl status cartond-local --no-pager
journalctl -u cartond-local -n 500 --no-pager
```

### 15.2 Carton and docker basics

```bash
/lumesof/bin/carton info
docker info
id -nG | tr ' ' '\n' | rg '^docker$' || echo "user is not in docker group"
```

### 15.3 Binary runtime linkage

```bash
file /lumesof/bin/carton
readelf -d /lumesof/bin/carton | rg 'RUNPATH|NEEDED'
```

Interpretation:

1. missing dependencies suggest package floor or installation mismatch
2. rpath/runtime linker issues often map to `lumesof-rpath` problems

## 16) Infrastructure Checks (Minikube + Disk)

```bash
minikube status
minikube logs --problems | tail -n 200
df -h
docker system df
```

No-space issues can break unrelated workflows.
If disk is full, builds and pulls can fail with misleading errors.

## 17) Registry And Image Pull Issues

### 17.1 Pod-level pull diagnosis

```bash
kubectl -n "$NS" describe pod <pod-name> | rg -i 'image|pull|back-off|failed'
```

### 17.2 Registry split reminder

1. repo-built OCI images: `us-central1-docker.pkg.dev/lumesof-sdk-infra/sdk-docker-registry/...:latest`
2. OSS images: original upstream registries

### 17.3 Practical checks

1. verify image reference in launch path
2. verify registry credentials
3. verify image exists in expected repo/tag

## 18) Structured Debug Session Template

Use this template for each investigation.

### 18.1 Session metadata

Record:

1. timestamp UTC
2. namespace
3. graph/app id
4. branch and recent code changes
5. target symptom

### 18.2 Command transcript sections

Capture output for:

1. cluster health
2. platform collector logs
3. operator collector logs
4. control-plane service logs
5. storage/index checks
6. runtime host checks

### 18.3 Findings format

Summarize in three blocks:

1. observed facts
2. inferred root cause hypotheses
3. validating next steps

## 19) Agent Procedure: From Request To Evidence

This is a strict process for coding agents.

### 19.1 Parse request intent

Map request to one of:

1. sidecar/operator logs
2. platform stats
3. lifecycle failure
4. index counts
5. collector wiring
6. host runtime

### 19.2 Establish minimal context

Commands:

```bash
kubectl -n "$NS" get pods
kubectl -n "$NS" get events --sort-by=.lastTimestamp | tail -n 60
```

### 19.3 Pull only relevant logs first

If sidecar/operator question:

```bash
kubectl -n "$NS" logs deploy/operator-otel-collector --since=30m --tail=10000
```

If platform transfer/lifecycle question:

```bash
kubectl -n "$NS" logs deploy/lumeflow-otel-collector --since=30m --tail=10000
```

### 19.4 Extract key dimensions

Agent should extract:

1. time range of issue
2. app_id / job_id
3. operator_name
4. port_name
5. message counts
6. error types

### 19.5 Report format to user

Provide:

1. concise summary first
2. evidence lines or command outputs summarized
3. anomaly list
4. suggested next probe

## 20) Practical Regex Cookbook

Use these patterns as a starting point.
Tune to actual log format.

### 20.1 General errors

```bash
rg -i 'error|exception|traceback|failed|timeout|unauthenticated|forbidden'
```

### 20.2 Lifecycle

```bash
rg -i 'StartJob|JOB_STATUS_|LAUNCH_FAILED|CREATED|RUNNING|FAILED|CANCELLED'
```

### 20.3 Message flow

```bash
rg -i 'received|published|ingress|egress|operator|port|message'
```

### 20.4 Operator dimensions

```bash
rg -i 'lumesof\.operator_name|lumesof\.instance_id|lumesof\.app_id|lumesof\.port_name'
```

### 20.5 RAG-specific stats patterns

```bash
rg -i 'batch|chunk|document|size|unique|hash|index|collection|count'
```

## 21) Counting and Summarization Shortcuts

### 21.1 Top repeating lines

```bash
... | sort | uniq -c | sort -nr | head -n 100
```

### 21.2 Count matching lines

```bash
... | rg -i 'pattern' | wc -l
```

### 21.3 Distinct operator names (example)

```bash
... | rg -o 'lumesof\.operator_name=[^ ]+' | sort -u
```

### 21.4 Distinct app IDs (example)

```bash
... | rg -o 'lumesof\.app_id=[^ ]+' | sort -u
```

## 22) Common Questions And What To Check

### 22.1 "Indexer has more items than ingress messages. Is that a bug?"

Check:

1. chunk size and overlap
2. structure extraction granularity
3. duplicate documents
4. retry duplication

### 22.2 "Chunker got many messages. I expected one."

Check:

1. upstream extractor emission semantics
2. whether extractor outputs one array or many element messages
3. platform received/published counts at extractor and chunker ports

### 22.3 "Why do I not see my new log statements?"

Check:

1. code changed in the right operator source
2. build target succeeded
3. publish target succeeded
4. cluster run used updated artifacts
5. reading operator collector logs, not platform collector

### 22.4 "How do I know if duplicates are from operator logic or transport retries?"

Check:

1. unique hash count logs inside operator
2. platform transfer counts and retry/error lines
3. timing and correlation of duplicates to failures

## 23) Sidecar Logs: Detailed Procedure

This section is intentionally explicit for the phrase "get me sidecar logs".

### 23.1 Step-by-step

1. ensure namespace is set
2. verify operator collector rollout
3. pull recent logs
4. filter by operator/app if known
5. report findings and unknowns

### 23.2 Commands

```bash
NS=lumeflow-local
kubectl -n "$NS" rollout status deploy/operator-otel-collector --timeout=5m
kubectl -n "$NS" logs deploy/operator-otel-collector --since=30m --tail=10000 > /tmp/operator_collector.log
```

Optional filters:

```bash
rg -i 'lumesof\.operator_name|lumesof\.instance_id|error|exception' /tmp/operator_collector.log
```

If specific operator:

```bash
OPERATOR="chunker"
rg -i "${OPERATOR}|lumesof\.operator_name" /tmp/operator_collector.log
```

### 23.3 What to tell the user

1. sidecar/operator logs are retrieved from operator collector
2. include count of relevant lines found
3. include strongest error or anomaly signatures
4. mention if additional scoping is needed (app_id, time range)

## 24) Platform Stats: Detailed Procedure

If user asks "how many messages moved between operators", do this.

### 24.1 Pull platform collector logs

```bash
kubectl -n "$NS" logs deploy/lumeflow-otel-collector --since=45m --tail=100000 > /tmp/platform_collector.log
```

### 24.2 Extract message movement candidates

```bash
rg -i 'received|published|operator|port|message' /tmp/platform_collector.log > /tmp/platform_message_lines.log
```

### 24.3 Summarize frequency

```bash
sort /tmp/platform_message_lines.log | uniq -c | sort -nr | head -n 120
```

### 24.4 Build operator-level summary

Use available fields from actual lines.
If fields are key-value pairs, aggregate by key.
If lines are JSON, parse with `jq` when available.

### 24.5 Report expected vs actual

Always compare:

1. upstream published count
2. downstream received count
3. sink/index observed count

This triangulation catches hidden multiplication.

## 25) JSON Log Mode Tips

Some collector output formats are JSON.
If so, use `jq`.

### 25.1 Sample JSON extraction pattern

```bash
kubectl -n "$NS" logs deploy/lumeflow-otel-collector --since=30m --tail=20000 \
  | jq -r 'select(.body != null) | .body'
```

### 25.2 Filter by severity and operator field

```bash
... | jq -r 'select((.severity_text // "") | test("ERROR|WARN"; "i"))'
```

```bash
... | jq -r 'select((.attributes["lumesof.operator_name"] // "") != "")'
```

If JSON parsing fails, fallback to plain `rg`.

## 26) Time Window Discipline

Always use a bounded time window.
Large unbounded logs are noisy and slow.

Recommended windows:

1. `--since=10m` for immediate failures
2. `--since=30m` for active debug sessions
3. `--since=60m` for intermittent issues

Also record the window in reports.

## 27) Sync vs Async Debug Patterns

### 27.1 Sync graph debugging focus

1. bridge request path
2. sync ingress and retriever link behavior
3. app_id routing correctness
4. end-to-end latency

### 27.2 Async graph debugging focus

1. ingestion rate
2. queue/backpressure behavior
3. operator batch dynamics
4. side-effect verification (DB/collector)

## 28) RAG Pipeline-Specific Debug Checklist

For ingest pipelines (extractor -> chunker -> indexer):

1. extractor ingress count
2. extractor published count
3. chunker received count
4. chunker output chunk counts
5. indexer received count
6. unique document count at indexer
7. Chroma item count and growth

If mismatch appears:

1. determine where multiplication starts
2. verify if multiplication is expected by settings
3. verify if duplicates are identical or legitimate splits

## 29) Chunking Configuration Interpretation

When analyzing high item counts, capture:

1. chunk size
2. overlap size
3. split heuristic (token/char/semantic)
4. max output chunks per doc behavior

Interpretation guideline:

1. smaller chunk size generally increases item count
2. higher overlap increases item count and can improve retrieval continuity
3. very aggressive splitting can produce noisy retrieval quality

## 30) Quality Implications Of Chunk Size Changes

General rule of thumb:

1. reducing chunk size can improve recall granularity
2. reducing chunk size too far can harm coherence and precision
3. increasing chunk size can improve context coherence but may reduce fine-grained recall

Always evaluate with representative queries.
Track both quality and cost/latency.

## 31) Deployment Wiring Checks For New Logs

If user says:

"I added logs in operator code but don’t see them"

Run this workflow:

1. verify source file contains new logs
2. run build for graph or operator
3. run publish target
4. relaunch/re-run graph
5. fetch operator collector logs in current time window

Commands example:

```bash
rg -n 'new_log_key|unique_docs_seen|batch_size' lumecode/apps/lumeflow_rag -S
bazel build //lumecode/apps/lumeflow_rag/ingest:async_ingest_light_graph
bazel run //lumecode/apps/lumeflow_rag/ingest:async_ingest_light_graph_publish
kubectl -n "$NS" logs deploy/operator-otel-collector --since=10m --tail=20000 | rg -i 'new_log_key|unique_docs_seen|batch_size'
```

## 32) Verifying Graph Is Using Updated Artifacts

Potential failure mode:

1. build and publish succeeded
2. run still uses older image or older deployment state

Checks:

1. inspect publish output for pushed references
2. inspect launch logs for pulled image references
3. compare expected tags/digests

## 33) What To Ignore During Signal Extraction

Not every error line is relevant.

Common low-priority noise examples:

1. zombie node heartbeats from prior runs
2. expected transient retries that later succeed
3. unrelated background warnings outside time window

Prioritize:

1. errors tied to current app_id/job
2. repeated failures with same signature
3. count anomalies tied to current operator chain

## 34) Jammy/Noble Compatibility Notes

Runtime package supports both via:

1. `libfuse2t64 | libfuse2`
2. installer accepting `jammy` and `noble`

When install behavior differs by host OS:

1. re-check apt policies
2. re-check repository setup
3. re-check package versions

## 35) Command Bundles By Role

### 35.1 Developer bundle

```bash
kubectl -n "$NS" get pods
kubectl -n "$NS" logs deploy/operator-otel-collector --since=20m --tail=5000
kubectl -n "$NS" logs deploy/lumeflow-otel-collector --since=20m --tail=5000
```

### 35.2 Platform bundle

```bash
kubectl -n "$NS" get events --sort-by=.lastTimestamp | tail -n 200
kubectl -n "$NS" logs deploy/flow-server --since=20m --tail=2000
kubectl -n "$NS" logs deploy/opnet-server --since=20m --tail=2000
kubectl -n "$NS" logs deploy/executor-control --since=20m --tail=2000
```

### 35.3 Runtime host bundle

```bash
systemctl status cartond-local --no-pager
journalctl -u cartond-local -n 500 --no-pager
docker info
```

## 36) Pre-Submission Checklist For Debug Findings

Before sharing root cause, verify:

1. you checked both collectors
2. you isolated current run by time window
3. you tied evidence to app_id/operator where possible
4. you compared transfer counts at at least two adjacent stages
5. you validated storage-level count if indexing involved
6. you listed assumptions clearly

## 37) Example Investigations

This section gives concise examples.

### 37.1 Example A: "Indexer saw 112 ingress but Chroma has 3571 items"

Likely avenues:

1. chunk expansion
2. duplicate submissions
3. retry duplication

Procedure:

1. extract platform transfer stats for extractor/chunker/indexer
2. inspect operator logs for chunk counts and unique hash counts
3. inspect Chroma count over time

Decision:

1. if unique docs stable and chunks high, expansion is likely expected
2. if unique docs low but items high, duplication likely

### 37.2 Example B: "Sidecar logs missing"

Procedure:

1. check operator collector rollout
2. check NodePorts and endpoint reachability
3. fetch operator collector logs
4. verify app actually ran and emitted logs

Decision:

1. collector down -> infra issue
2. collector up but empty -> wiring issue
3. logs present without expected keys -> code/build/publish mismatch

### 37.3 Example C: "StartJob fails instantly"

Procedure:

1. search platform collector for `StartJob|LAUNCH_FAILED`
2. check for DockerLaunchError and GAR unauthenticated
3. refresh auth and redeploy with expected registry mode

## 38) Agent Output Templates

Use these templates when reporting findings.

### 38.1 Template: Sidecar logs request

Report:

1. source: operator collector
2. time window
3. total lines matched
4. top error signatures
5. top operator names observed
6. recommended next command

### 38.2 Template: Message transfer stats request

Report:

1. source: platform collector
2. operators in path
3. received/published counts per stage
4. first stage where mismatch appears
5. likely cause candidates
6. next validation step

### 38.3 Template: Index inflation request

Report:

1. ingress count at indexer
2. observed output item count
3. unique-doc metric from operator logs
4. chunk settings snapshot
5. conclusion: expected expansion vs duplication

## 39) Safety and Hygiene For Repeated Debugging

1. keep commands read-only unless explicitly changing deployment
2. prefer `--since` bounded log reads
3. avoid deleting data unless requested
4. label temporary files in `/tmp` clearly
5. note exact command variants used in report

## 40) Scriptable Snippets For Agents

These snippets are suitable building blocks.

### 40.1 Function: pull platform logs

```bash
pull_platform_logs() {
  local since="${1:-30m}"
  kubectl -n "${NS}" logs deploy/lumeflow-otel-collector --since="${since}" --tail=100000
}
```

### 40.2 Function: pull operator logs

```bash
pull_operator_logs() {
  local since="${1:-30m}"
  kubectl -n "${NS}" logs deploy/operator-otel-collector --since="${since}" --tail=100000
}
```

### 40.3 Function: scan transfer stats

```bash
scan_transfer_stats() {
  local since="${1:-30m}"
  pull_platform_logs "${since}" | rg -i 'received|published|operator|port|message'
}
```

### 40.4 Function: scan operator stats

```bash
scan_operator_stats() {
  local since="${1:-30m}"
  pull_operator_logs "${since}" | rg -i 'batch|document|chunk|size|unique|hash|count|error|exception'
}
```

## 41) Troubleshooting Decision Tree

Use this strict order.

1. Are pods and rollouts healthy?
2. Are both collectors healthy?
3. Are logs present in the expected collector?
4. Is failure lifecycle-level or dataflow-level?
5. If dataflow-level, where does mismatch begin?
6. If indexing involved, do storage counts align with flow stats?
7. Are duplicates confirmed by unique hashes?
8. Are code changes reflected in deployed artifacts?

## 42) Known High-Value Signals

Prioritize these lines when scanning large logs:

1. `LAUNCH_FAILED`
2. `DockerLaunchError`
3. `Unauthenticated request`
4. `NodeCollection async_connect: success`
5. `unknown consumer`
6. message `received`/`published` with operator/port context
7. user-added stats keys (`batch_size`, `unique_docs_seen`, `doc_sizes_bytes`)

## 43) Human + Agent Collaboration Pattern

Recommended interaction style:

1. human states intent in plain language
2. agent maps intent to command bundle
3. agent returns concise findings with evidence
4. human chooses next hypothesis
5. agent runs focused follow-up commands

This keeps debugging fast and transparent.

## 44) Minimal Command Matrix

If you only remember six commands, use these.

```bash
kubectl -n "$NS" get pods
kubectl -n "$NS" get events --sort-by=.lastTimestamp | tail -n 100
kubectl -n "$NS" logs deploy/lumeflow-otel-collector --since=30m --tail=5000
kubectl -n "$NS" logs deploy/operator-otel-collector --since=30m --tail=5000
kubectl -n "$NS" logs deploy/opnet-server --since=30m --tail=2000
journalctl -u cartond-local -n 300 --no-pager
```

## 45) Expanded Incident Bundle

When escalating, include this expanded bundle.

```bash
echo "=== cluster ==="
minikube status
kubectl -n "$NS" get pods -o wide
kubectl -n "$NS" get events --sort-by=.lastTimestamp | tail -n 300

echo "=== collectors ==="
kubectl -n "$NS" rollout status deploy/lumeflow-otel-collector --timeout=5m
kubectl -n "$NS" rollout status deploy/operator-otel-collector --timeout=5m
kubectl -n "$NS" logs deploy/lumeflow-otel-collector --since=45m
kubectl -n "$NS" logs deploy/operator-otel-collector --since=45m

echo "=== platform services ==="
kubectl -n "$NS" logs deploy/flow-server --since=45m --tail=5000
kubectl -n "$NS" logs deploy/opnet-server --since=45m --tail=5000
kubectl -n "$NS" logs deploy/executor-control --since=45m --tail=5000
kubectl -n "$NS" logs deploy/rpc-opnet-bridge-shard0-replica0 --since=45m --tail=5000
kubectl -n "$NS" logs deploy/rpc-opnet-bridge-shard0-replica1 --since=45m --tail=5000

echo "=== host runtime ==="
systemctl status cartond-local --no-pager
journalctl -u cartond-local -n 700 --no-pager
/lumesof/bin/carton info
docker info

echo "=== package floor ==="
apt-cache policy lumesof-rpath cartond-non-lumesof-local lumeflow-local-runtime lumeflow-local-smoketest
```

## 46) Practical Notes About Log Volume

Collector logs can be large.
Use windows and filters early.

Good practice:

1. start with `--since=10m` for immediate issue
2. widen to `30m` or `60m` only if needed
3. pipe to `rg` before writing large files
4. write to `/tmp` if repeated parsing needed

## 47) Example Agent Responses (Reference)

### 47.1 Response style for "get me sidecar logs"

Suggested response:

1. mention that sidecar logs come from `operator-otel-collector`
2. provide line count and major signatures
3. provide next command for deeper operator filtering

### 47.2 Response style for "how many messages transferred"

Suggested response:

1. present per-stage counts
2. identify first mismatch stage
3. flag expected fanout vs anomaly possibility

### 47.3 Response style for "why are there more items than ingress"

Suggested response:

1. combine platform transfer stats and operator batch stats
2. include unique-doc evidence
3. include chunk settings context
4. conclude expected expansion or likely duplication

## 48) Debugging Checklist For PR Validation

When code changes are intended to improve observability:

1. add logs in operator code
2. build graph target
3. publish graph target
4. run a known input workload
5. verify new logs appear in operator collector
6. verify platform stats still available
7. document command used to retrieve new logs

## 49) Troubleshooting Missing Collector Deployments

If collector deployment itself is missing:

1. verify deployment manifest/profile included collector
2. check deployment script flags
3. redeploy cluster profile with collectors enabled

Fast check:

```bash
kubectl -n "$NS" get deploy | rg 'otel-collector'
```

## 50) FAQ

### 50.1 Where do sidecar logs come from?

From `operator-otel-collector`.
Not from per-operator Kubernetes pods.

### 50.2 Where do platform message-transfer stats come from?

From Lumeflow platform component logs collected by `lumeflow-otel-collector`.

### 50.3 Can users customize operator logs?

Yes.
Operator logs are fully user-defined.

### 50.4 Do new operator logs require manual collector changes?

Normally no.
With correct Bazel build/publish wiring, rebuilding and publishing updated graph/operator artifacts is enough.

### 50.5 What if I only have one symptom and no identifiers?

Start with collector logs using a short time window.
Then extract app/operator identifiers from those logs.

## 51) Glossary

1. collector: OTEL log collector deployment
2. platform collector: collector for Lumeflow control/runtime services
3. operator collector: collector for user operator and sidecar workload logs
4. sidecar: runtime companion process for operator workload execution path
5. carton: workload runtime unit managed outside direct pod-per-operator model
6. ingress: input port of operator or graph boundary
7. egress: output port of operator or graph boundary
8. fanout: one input leads to many outputs
9. dedupe: duplicate elimination logic

## 52) Copy-Paste Recipes By Ask

### 52.1 Ask: "Get me sidecar logs for last 20 minutes"

```bash
NS=lumeflow-local
kubectl -n "$NS" rollout status deploy/operator-otel-collector --timeout=5m
kubectl -n "$NS" logs deploy/operator-otel-collector --since=20m --tail=20000
```

### 52.2 Ask: "Get me errors from operator logs"

```bash
kubectl -n "$NS" logs deploy/operator-otel-collector --since=30m --tail=50000 \
  | rg -i 'error|exception|failed|traceback'
```

### 52.3 Ask: "Get me transfer stats from platform"

```bash
kubectl -n "$NS" logs deploy/lumeflow-otel-collector --since=30m --tail=50000 \
  | rg -i 'received|published|operator|port|message'
```

### 52.4 Ask: "Is Chroma count growing right now?"

```bash
PUBLIC_HOST="$(minikube ip)"
BASE="http://${PUBLIC_HOST}:30090/api/v2/tenants/default_tenant/databases/default_database"
CID="$(curl -sS "${BASE}/collections" | rg -o '"id":"[^"]+"' -m1 | cut -d'"' -f4)"
watch -n 2 "curl -sS \"${BASE}/collections/${CID}/count\""
```

### 52.5 Ask: "Are collectors up?"

```bash
kubectl -n "$NS" get pods | rg 'otel-collector'
kubectl -n "$NS" rollout status deploy/lumeflow-otel-collector --timeout=5m
kubectl -n "$NS" rollout status deploy/operator-otel-collector --timeout=5m
```

### 52.6 Ask: "Bundle everything for escalation"

Use the expanded bundle in Section 45.

## 53) Final Operational Guidance

When in doubt, follow this order:

1. health
2. collectors
3. platform stats
4. operator stats
5. storage state
6. runtime host
7. build/publish wiring

This sequence prevents thrashing.
It narrows scope quickly.
It produces evidence suitable for both immediate fixes and escalation.

## 54) Source References

Primary source cheatsheet:

1. `cheatsheets/LOCAL_DAG_DEBUGGING_CHEATSHEET.md`

Related docs:

1. `lumesof/lumeflow_documentation/ARCHITECTURE.md`
2. `lumesof/lumeflow_documentation/API_REFERENCE.md`
3. `lumesof/lumeflow_documentation/WRITING_A_LUMEFLOW_APPLICATION.md`

## 55) Appendix A: One-Liner Packs

### 55.1 Platform error one-liner

```bash
kubectl -n "$NS" logs deploy/lumeflow-otel-collector --since=30m --tail=50000 | rg -i 'error|exception|failed|launch_failed|unauthenticated'
```

### 55.2 Operator error one-liner

```bash
kubectl -n "$NS" logs deploy/operator-otel-collector --since=30m --tail=50000 | rg -i 'error|exception|failed|traceback'
```

### 55.3 Operator stats one-liner

```bash
kubectl -n "$NS" logs deploy/operator-otel-collector --since=30m --tail=50000 | rg -i 'batch|chunk|document|size|unique|hash|count'
```

### 55.4 Platform transfer one-liner

```bash
kubectl -n "$NS" logs deploy/lumeflow-otel-collector --since=30m --tail=50000 | rg -i 'received|published|operator|port|message'
```

### 55.5 Collector wiring one-liner

```bash
kubectl -n "$NS" get svc operator-otel-collector lumeflow-otel-collector -o wide && minikube ip
```

## 56) Appendix B: Structured Notes Template

Use this markdown template for every debug run.

```markdown
# Debug Session

## Context
- Time window:
- Namespace:
- App/job identifiers:
- Symptom:

## Commands Run
- command 1
- command 2
- command 3

## Observations
1. 
2. 
3. 

## Statistics
- platform received/published summary:
- operator batch/unique summary:
- storage count summary:

## Hypotheses
1. 
2. 

## Next Step
1. 
```

## 57) Appendix C: What Good Evidence Looks Like

Good evidence has:

1. exact command used
2. bounded time window
3. concrete line counts or values
4. correlation across at least two sources
5. explicit uncertainty where needed

Examples:

1. "Indexer ingress saw 112 messages (platform logs), chunker emitted 3,571 chunks (operator logs), unique_docs_seen remained 112 (operator logs)."
2. "Chroma count rose from 0 to 3,571 during run; this matches chunk output count."

## 58) Appendix D: Common Mistakes

1. searching pod logs for operator sidecars directly
2. ignoring one of the two collectors
3. reading logs without `--since` window
4. concluding duplication without unique-hash stats
5. forgetting to rebuild/publish after adding logs

## 59) Appendix E: Minimum Agent Capabilities

A coding agent assisting with Lumeflow debugging should be able to:

1. locate both OTEL collectors
2. fetch and filter logs from each collector
3. differentiate platform and operator signals
4. compute or summarize transfer stats
5. retrieve Chroma counts and storage sizes
6. package incident bundle commands
7. propose next commands based on observed anomalies

## 60) Closing

Debugging Lumeflow is fastest when you maintain strict source separation:

1. platform collector for runtime mechanics and transfer stats
2. operator collector for business logic and custom operator instrumentation

Use the command recipes in this document.
Use bounded time windows.
Use count comparisons at each stage.
Use unique-document metrics when duplication is suspected.

With those habits, most dataflow issues become diagnosable in minutes rather than hours.
