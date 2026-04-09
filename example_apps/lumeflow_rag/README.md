# Lumeflow RAG

`lumecode/apps/lumeflow_rag` provides canonical request/DAG builders for:

1. Async ingest (`structure-extractor -> chunker -> chromadb-indexer`)
2. Sync agent query (`llm-agent <-> chromadb-retriever` loop with bridge request/response)

## Async Ingest Example (submit/start/inject)

```python
import asyncio

from lumesof.lumeflow import Client, Proto
from example_apps.lumeflow_rag.ingest.ingest_flow_light import (
    AsyncIngestOperatorImages,
    buildAsyncIngestLightInjectMessageRequest,
    buildAsyncIngestLightSubmitJobRequest,
)


async def main() -> None:
    sdk = Client(
        flowServerTarget="tcp://127.0.0.1:50070",
        configServiceTarget="tcp://127.0.0.1:50074",
    )

    submit_request = buildAsyncIngestLightSubmitJobRequest(
        owner="rag-demo",
        cluster_id="11111111-1111-1111-1111-111111111111",
        images=AsyncIngestOperatorImages(
            structure_extractor_image_url="registry/structure-extractor:latest",
            chunker_image_url="registry/chunker:latest",
            chromadb_indexer_image_url="registry/chromadb-indexer:latest",
        ),
    )
    job = await sdk.async_submitJob(submit_request, startJob=False)
    await job.async_startWhenReady()

    inject_request = buildAsyncIngestLightInjectMessageRequest(
        job_id=job.id(),
        uri="https://example.com/handbook.pdf",
        rag_uri="http://chromadb:8000/acme_support-index_v1",
        trace_id="trace-ingest-1",
    )
    async_client = job.asyncClient()
    await async_client.async_injectMessage(message=inject_request.message)

    await sdk.async_close()


if __name__ == "__main__":
    asyncio.run(main())
```

## Sync Agent Example (submit/start/call)

```python
import asyncio

from lumesof.lumeflow import Client, Proto
from example_apps.lumeflow_rag.extract.agent_flow_light import (
    SyncAgentOperatorImages,
    buildSyncAgentLightSubmitJobRequest,
)
from example_apps.lumeflow_rag.common import operators_pb2


async def main() -> None:
    sdk = Client(
        flowServerTarget="tcp://127.0.0.1:50070",
        configServiceTarget="tcp://127.0.0.1:50074",
    )

    submit_request = buildSyncAgentLightSubmitJobRequest(
        owner="rag-demo",
        cluster_id="11111111-1111-1111-1111-111111111111",
        app_id="rag-sync-app",
        images=SyncAgentOperatorImages(
            llm_agent_image_url="registry/llm-agent:latest",
            chromadb_retriever_image_url="registry/chromadb-retriever:latest",
        ),
    )
    job = await sdk.async_submitJob(submit_request, startJob=False)
    await job.async_startWhenReady()

    sync_client = job.syncClient()
    agent_request = operators_pb2.AgentRequest(
        rag_uri="http://chromadb:8000/acme_support-index_v1",
        serving_uri="http://ollama:11434",
        prompt="Summarize the on-call policy.",
    )
    response_payload = await sync_client.async_call(
        payload=agent_request.SerializeToString(),
        payloadTypeUrl=operators_pb2.AgentRequest.DESCRIPTOR.full_name,
        payloadSerializationFormat=int(Proto.opnet_types.OpNetPayloadType.PROTO),
        timeoutMs=5000,
    )

    respond_request = operators_pb2.RespondRequest()
    respond_request.ParseFromString(response_payload)
    print(respond_request.response)

    await sdk.async_close()


if __name__ == "__main__":
    asyncio.run(main())
```

## Policy/Config Helpers

1. `common.payload_contracts.normalizeIngestPayload(...)` and `normalizeAgentPayload(...)` enforce fail-closed request policy bounds.
2. `common.payload_contracts.resolveChromaConfigDefaults(...)` consumes exported config keys:
   1. `rag.chroma.endpoint`
   2. `rag.chroma.default_collection`

## Local Cluster Launch Scripts

After the local cluster is created and deployed, launch the two light apps
(pinned operator digests + dedicated DAG names):

```bash
./lumecode/apps/lumeflow_rag/ingest/launch_indexer_light.sh --url=https://example.com/docs/page
./lumecode/apps/lumeflow_rag/extract/chat_agent_light.sh
```

Defaults:

1. `--public-host` defaults to `minikube ip`.
2. Flow/config targets default to `tcp://<public-host>:30070` and `tcp://<public-host>:30074`.
3. `rag_uri` defaults to `${rag.chroma.endpoint}/${rag.chroma.default_collection}` from config service, with fallback `http://chromadb:8000/default`.
4. Operator image refs default to digest-pinned OCI images and can be overridden via flags.
5. `launch_indexer_light.sh` submits DAG `lumeflow-rag-async-ingest-light-v1`.
6. `chat_agent_light.sh` submits DAG `lumeflow-rag-sync-chat-loop-light-v1`.

## Observability

See [OBSERVABILITY.md](./OBSERVABILITY.md) for metric names, SLO starter thresholds, and runbook pointers.

## Legacy Note

The legacy FAISS pipeline formerly in `lumecode/rag/rag.py` has been removed. New onboarding should use this `lumeflow_rag` package and operator-based Lumeflow jobs.
