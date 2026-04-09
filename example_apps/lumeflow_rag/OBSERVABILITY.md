# Lumeflow RAG Observability Notes

## Structured Metrics

RAG operators emit structured log lines prefixed with `RAG_METRIC` and a JSON payload.

Current query-side metrics (`OllamaAgentOperator`):

1. `rag.query.latency_ms`
2. `rag.query.error_count`
3. `rag.query.validation_error_count`
4. `rag.query.limit_hit_count`
5. `rag.query.miss_count`
6. `rag.query.retry_count`

Current ingest-side metrics (`ChromaDbInsertOperator`):

1. `rag.ingest.latency_ms`
2. `rag.ingest.document_count`
3. `rag.ingest.error_count`

## Suggested SLO Starter Set

1. Query latency: `rag.query.latency_ms` p95 < 5000 ms
2. Query error ratio: `rag.query.error_count / query requests` < 1%
3. Query miss ratio: `rag.query.miss_count / query requests` tracked and alerted on sustained spikes
4. Ingest latency: `rag.ingest.latency_ms` p95 < 3000 ms for normal document sizes
5. Ingest error ratio: `rag.ingest.error_count / ingest requests` < 1%

## Dashboard Pointers

1. Create a "RAG Query" panel group:
   1. latency p50/p95 (`rag.query.latency_ms`)
   2. errors (`rag.query.error_count`)
   3. limit hits (`rag.query.limit_hit_count`)
   4. misses (`rag.query.miss_count`)
2. Create a "RAG Ingest" panel group:
   1. latency p50/p95 (`rag.ingest.latency_ms`)
   2. ingest volume (`rag.ingest.document_count`)
   3. errors (`rag.ingest.error_count`)

## Runbook Pointers

1. Rising `rag.query.limit_hit_count`: inspect prompt/tool-call behavior and model responses.
2. Rising `rag.query.miss_count`: inspect Chroma collection freshness and embedding consistency.
3. Rising `rag.ingest.error_count`: inspect Chroma endpoint health and rag URI endpoint policy.
