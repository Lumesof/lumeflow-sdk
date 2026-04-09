# LumeFlow RAG Operator Split PR Plan

Date: 2026-04-03
Owner: Lumeflow RAG
Status: Planned

## Objective

Implement a three-operator RAG architecture:

1. `llm_agent_operator`
2. `chromadb_indexer_operator`
3. `chromadb_retriever_operator`

with these constraints:

- `llm_agent_operator` inputs: `initial_request`, `enriched_request`; outputs: `retrieval_command`, `final_text`.
- `chromadb_indexer_operator` input: `StoreRequest`; outputs: none.
- `chromadb_retriever_operator` input: retrieval command; output: enriched request.
- All three operators perform constructor-time model discovery.
- If required model discovery fails, operator must set a fail flag, emit `WARN`, and return `Result(ok=False, ...)` for every request (with per-request `WARN`).

## Architecture Summary

### Sync agent flow (loop-enabled)

- `SYNC_INJECTOR -> llm_agent.initial_request`
- `llm_agent.retrieval_command -> chromadb_retriever.retrieval_command`
- `chromadb_retriever.enriched_request -> llm_agent.enriched_request`
- `llm_agent.final_text -> SYNC_RETRIEVER`

`dag.allow_loops = True` and `dag.graph_type = SYNC` are required for sync submit.

### Async ingest flow

- keep `structure_extractor -> chunker`
- replace insert stage with `chromadb_indexer_operator` (consuming `StoreRequest`)

## PR Stack

### PR1: Proto contracts and discovery utilities

Scope:

- Extend `lumecode/apps/lumeflow_rag/operators.proto` with:
  - `RetrievalCommandRequest`
  - `EnrichedRequest`
- Add shared Ollama discovery utility module for:
  - `/api/tags` listing
  - LLM model selection
  - embedding model selection with `/api/embed` probe
  - fail-flag state helpers

Files:

- `lumecode/apps/lumeflow_rag/operators.proto`
- `lumecode/apps/lumeflow_rag/*discovery*.py` (new)
- `lumecode/apps/lumeflow_rag/BUILD.bazel` (deps/targets)

Tests:

- New unit tests for selection and failure cases.

Completion gate:

- Proto compiles.
- Discovery helper test suite passes.

### PR2: Add `llm_agent_operator`

Scope:

- Create operator with ports:
  - ingress `initial_request` (`AgentRequest`)
  - ingress `enriched_request` (`EnrichedRequest`)
  - egress `retrieval_command` (`RetrievalCommandRequest`)
  - egress `final_text` (`RespondRequest`)
- Reuse embedded Ollama lifecycle from current `ollama_agent_operator`.
- `initial_request` path performs first LLM call and emits retrieval command.
- `enriched_request` path performs second/final LLM call and emits final response.
- Constructor discovery for LLM model.
- Fail-flag warn+nack behavior.

Files:

- `lumecode/apps/lumeflow_rag/llm_agent_operator.py` (new)
- `lumecode/apps/lumeflow_rag/BUILD.bazel`
- tests under `lumecode/apps/lumeflow_rag/tests/`

Tests:

- unit tests for both ingress paths
- unit tests for fail-flag behavior and warning logs

Completion gate:

- Operator tests pass and port payload types are validated.

### PR3: Add `chromadb_retriever_operator`

Scope:

- Create retriever operator with ports:
  - ingress `retrieval_command` (`RetrievalCommandRequest`)
  - egress `enriched_request` (`EnrichedRequest`)
- Execute retrieval commands against Chroma.
- Embed query using discovered embedding model via embedded Ollama.
- Append retrieval results to message context for downstream final LLM call.
- Constructor discovery for embedding model.
- Fail-flag warn+nack behavior.

Files:

- `lumecode/apps/lumeflow_rag/chromadb_retriever_operator.py` (new)
- `lumecode/apps/lumeflow_rag/BUILD.bazel`
- tests under `lumecode/apps/lumeflow_rag/tests/`

Tests:

- retrieval hit/miss behavior
- payload enrichment shape validation
- fail-flag behavior

Completion gate:

- Retriever unit tests pass with deterministic mocked Chroma and embedding calls.

### PR4: Add `chromadb_indexer_operator` using `StoreRequest`

Scope:

- Create indexer operator with one ingress:
  - `store` (`StoreRequest`)
- For each document:
  - generate embedding with discovered embedding model
  - write `documents + embeddings + metadatas + ids` to Chroma
- No output ports.
- Constructor discovery for embedding model.
- Fail-flag warn+nack behavior.

Files:

- `lumecode/apps/lumeflow_rag/chromadb_indexer_operator.py` (new)
- `lumecode/apps/lumeflow_rag/BUILD.bazel`
- tests under `lumecode/apps/lumeflow_rag/tests/`

Tests:

- vector insert path
- id/metadata propagation
- fail-flag behavior

Completion gate:

- Indexer unit tests pass and write payload shape is correct.

### PR5: Build targets and image targets for split operators

Scope:

- Add `lumesof_py_library`, `lumesof_py_binary`, `lumesof_py_test` for new operators.
- Add OCI image targets for:
  - LLM operator using qwen base variants
  - Retriever/indexer using embedding-model bases
- Keep old operator targets temporarily for safe migration.

Files:

- `lumecode/apps/lumeflow_rag/BUILD.bazel`

Tests:

- Bazel build target checks for new binaries/images.

Completion gate:

- New binary targets build cleanly.

### PR6: Update sync/async flow builders

Scope:

- Update sync DAG in `agent_flow.py` to use split operators and cyclic links.
- Set `dag.allow_loops = True` and `dag.graph_type = SYNC` on the sync DAG.
- Update ingest flow to use `chromadb_indexer_operator` downstream of chunker.

Files:

- `lumecode/apps/lumeflow_rag/agent_flow.py`
- `lumecode/apps/lumeflow_rag/ingest_flow.py`
- related tests in `lumecode/apps/lumeflow_rag/tests/`

Tests:

- DAG shape tests
- payload type tests
- loop validation test path

Completion gate:

- DAG validation passes with loop enabled.

### PR7: Launcher/chat app migration

Scope:

- Add separate image flags/config for:
  - llm agent image
  - retriever image
  - indexer image
- Preserve startup visibility logs:
  - submit/create/start transitions
  - selected image URLs

Files:

- `lumecode/apps/lumeflow_rag/launch_agent_main.py`
- `lumecode/apps/lumeflow_rag/chat_agent_main.py`
- `lumecode/apps/lumeflow_rag/launch_indexer_main.py`
- docs under `lumecode/apps/lumeflow_rag/`

Tests:

- launcher argument parsing and request wiring tests

Completion gate:

- launchers build and tests pass.

### PR8: Integration tests and observability hardening

Scope:

- Add/refresh integration tests for:
  - sync agent loop flow end-to-end
  - async ingest via indexer end-to-end
- Verify fail-flag WARN + nack behavior in integration context.
- Add metrics/log assertions for model discovery outcomes.

Files:

- `lumecode/apps/lumeflow_rag/tests/*integration*`
- `lumecode/apps/lumeflow_rag/tests/*`

Tests:

- integration tests (mocked or local harness as appropriate)

Completion gate:

- Integration suite green.

### PR9: Remove legacy monolithic agent path

Scope:

- Remove old split-by-two-instance `ollama_agent_operator` flow wiring from app-level DAG builders.
- Keep legacy operator code only if still needed by unrelated paths; otherwise remove obsolete wiring/docs.

Files:

- `lumecode/apps/lumeflow_rag/agent_flow.py`
- `lumecode/apps/lumeflow_rag/README.md`
- `lumecode/apps/lumeflow_rag/CHAT_AGENT.md`

Tests:

- regression run for updated sync and ingest flows

Completion gate:

- No app path depends on legacy agent flow wiring.

## Cross-PR Requirements

- Python naming conventions from `AGENTS.md`:
  - methods `camelCase`
  - async methods `async_camelCase`
  - tests `test_camelCase`
- Use canonical proto type URLs when constructing payload types.
- Return `Result(ok=False, ...)` for fail-flag nacks.
- Log `WARN` once on discovery failure and `WARN` per request while failed.

## Risks and Mitigations

1. Embedding mismatch between index and query spaces.
   - Mitigation: shared discovery/selection utility and explicit model logging in both operators.

2. Looped DAG regressions.
   - Mitigation: dedicated DAG tests plus flow validation with `dag.allow_loops = True`.

3. Cold-start delays.
   - Mitigation: eager constructor discovery and clear startup logs.

4. Silent degraded behavior.
   - Mitigation: strict fail-flag with explicit WARN+nack behavior.

## Rollback Plan

- Revert app DAG wiring to legacy sync flow builder.
- Keep new operators in tree but not referenced by launchers until issues are fixed.
