from __future__ import annotations

import argparse
import asyncio
import contextlib
from pathlib import Path
import subprocess
import time
import uuid
from urllib.parse import urlparse

from example_apps.lumeflow_rag.ingest.image_descriptors.chromadb_indexer_operator_nomic_embed_text_v1_5 import (
    _IMAGE_DESCRIPTOR as _CHROMADB_INDEXER_IMAGE_DESCRIPTOR,
)
from example_apps.lumeflow_rag.ingest.image_descriptors.chunker_operator import (
    _IMAGE_DESCRIPTOR as _CHUNKER_IMAGE_DESCRIPTOR,
)
from example_apps.lumeflow_rag.ingest.image_descriptors.structure_extractor_operator import (
    _IMAGE_DESCRIPTOR as _STRUCTURE_EXTRACTOR_IMAGE_DESCRIPTOR,
)
from example_apps.lumeflow_rag.ingest.ingest_flow_light import AsyncIngestOperatorImages
from example_apps.lumeflow_rag.ingest.ingest_flow_light import buildAsyncIngestLightInjectMessageRequest
from example_apps.lumeflow_rag.ingest.ingest_flow_light import buildAsyncIngestLightSubmitJobRequest
from example_apps.lumeflow_rag.common.rag_uri import parseRagUri
from lumesof.lumeflow import ConfigQueryClientError
from lumesof.lumeflow import async_getRequiredStringConfigValue
from lumesof.lumeflow import Client
from lumesof.lumeflow import Job
from lumesof.lumeflow import JobResolutionError
from lumesof.lumeflow import Proto
from lumesof.pylib import Net

_DEFAULT_CLUSTER_ID = "11111111-1111-1111-1111-111111111111"
_DEFAULT_OWNER = "lumeflow-rag-indexer-light"
_DEFAULT_JOB_ID_FILE = "/tmp/lumeflow-rag-indexer-light-v1.job_id"
_DEFAULT_RAG_ENDPOINT = "http://chromadb:8000"
_DEFAULT_RAG_COLLECTION = "default"
_DEFAULT_CHROMA_NODE_PORT = 30090
_DEFAULT_FLOW_SERVER_NODEPORT = 30070
_DEFAULT_CONFIG_SERVICE_NODEPORT = 30074
_DEFAULT_STRUCTURE_EXTRACTOR_IMAGE_URL = _STRUCTURE_EXTRACTOR_IMAGE_DESCRIPTOR.imageUrl
_DEFAULT_CHUNKER_IMAGE_URL = _CHUNKER_IMAGE_DESCRIPTOR.imageUrl
_DEFAULT_CHROMADB_INDEXER_IMAGE_URL = _CHROMADB_INDEXER_IMAGE_DESCRIPTOR.imageUrl


def _normalizeTarget(value: str) -> str:
    trimmed = value.strip()
    if trimmed.startswith("tcp://") or trimmed.startswith("unix:///"):
        return trimmed
    return f"tcp://{trimmed}"


def _resolveMinikubeHost() -> str:
    result = subprocess.run(
        ["minikube", "ip"],
        capture_output=True,
        text=True,
        check=True,
    )
    host = result.stdout.strip()
    if not host:
        raise ValueError("minikube ip returned an empty host")
    return host


def _resolveServiceTargets(args: argparse.Namespace) -> tuple[str, str, str]:
    publicHost = args.public_host.strip() if args.public_host else ""
    flowServerFlag = args.flow_server.strip() if args.flow_server else ""
    configServiceFlag = args.config_service.strip() if args.config_service else ""

    if flowServerFlag and configServiceFlag:
        flowServerTarget = _normalizeTarget(flowServerFlag)
        configServiceTarget = _normalizeTarget(configServiceFlag)
    elif not flowServerFlag and not configServiceFlag:
        if not publicHost:
            publicHost = _resolveMinikubeHost()
        flowServerTarget = f"tcp://{publicHost}:{_DEFAULT_FLOW_SERVER_NODEPORT}"
        configServiceTarget = f"tcp://{publicHost}:{_DEFAULT_CONFIG_SERVICE_NODEPORT}"
    else:
        raise ValueError("Provide both --flow-server and --config-service, or neither.")

    if not publicHost:
        parsed = Net.NetTarget.parseTarget(flowServerTarget)
        host = parsed.getHost()
        if host is None:
            raise ValueError("Unable to infer --public-host from --flow-server target")
        publicHost = host

    return (flowServerTarget, configServiceTarget, publicHost)


def _shouldRemapChromaHost(*, host: str | None) -> bool:
    if host is None:
        return False
    loweredHost = host.strip().lower()
    if loweredHost in {"chromadb", "localhost", "127.0.0.1", "host.minikube.internal"}:
        return True
    return (
        loweredHost.endswith(".svc")
        or loweredHost.endswith(".svc.cluster.local")
        or loweredHost.endswith(".cluster.local")
    )


def _remapEndpointForExternalExecutors(
    *,
    endpoint: str,
    publicHost: str,
    chromaNodePort: int,
) -> str:
    parsed = urlparse(endpoint)
    if not _shouldRemapChromaHost(host=parsed.hostname):
        return endpoint
    scheme = parsed.scheme if parsed.scheme else "http"
    return f"{scheme}://{publicHost}:{chromaNodePort}"


async def _async_resolveRagUri(
    *,
    args: argparse.Namespace,
    configServiceTarget: str,
    publicHost: str,
) -> str:
    if args.rag_uri and args.rag_uri.strip():
        candidate = args.rag_uri.strip()
        parseRagUri(rag_uri=candidate)
        return candidate

    endpoint = args.chroma_endpoint.strip() if args.chroma_endpoint else ""
    hasExplicitEndpointOverride = bool(endpoint)
    if not endpoint:
        try:
            endpoint = await async_getRequiredStringConfigValue(
                configServiceTarget=configServiceTarget,
                key="rag.chroma.endpoint",
            )
        except ConfigQueryClientError:
            endpoint = _DEFAULT_RAG_ENDPOINT
    if not hasExplicitEndpointOverride:
        endpoint = _remapEndpointForExternalExecutors(
            endpoint=endpoint,
            publicHost=publicHost,
            chromaNodePort=int(args.chroma_node_port),
        )

    collection = args.collection.strip() if args.collection else ""
    if not collection:
        try:
            collection = await async_getRequiredStringConfigValue(
                configServiceTarget=configServiceTarget,
                key="rag.chroma.default_collection",
            )
        except ConfigQueryClientError:
            collection = _DEFAULT_RAG_COLLECTION

    candidate = f"{endpoint.rstrip('/')}/{collection}"
    parseRagUri(rag_uri=candidate)
    return candidate


def _loadJobId(*, path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _storeJobId(*, path: Path, jobId: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{jobId}\n", encoding="utf-8")


def _statusName(*, status: int) -> str:
    try:
        return Proto.flow_server.JobStatus.Name(status)
    except ValueError:
        return str(status)


async def _async_startJobWithProgress(
    *,
    job: Job,
    timeoutSeconds: float,
    pollSeconds: float = 2.0,
) -> None:
    stopEvent = asyncio.Event()
    startMono = time.monotonic()

    async def _async_reportProgress() -> None:
        lastStatus: int | None = None
        while not stopEvent.is_set():
            try:
                statusResponse = await job.async_getStatus()
                status = statusResponse.status
                if status != lastStatus:
                    elapsedSeconds = time.monotonic() - startMono
                    print(
                        f"[indexer-light] start-wait status={_statusName(status=status)} "
                        f"elapsed_s={elapsedSeconds:.1f}"
                    )
                    lastStatus = status
            except Exception as exc:
                print(f"[indexer-light] status poll warning: {exc}")

            try:
                await asyncio.wait_for(stopEvent.wait(), timeout=pollSeconds)
            except TimeoutError:
                pass

    reporterTask = asyncio.create_task(_async_reportProgress())
    try:
        print(f"[indexer-light] waiting for job {job.id()} to reach STARTED (timeout={timeoutSeconds:.0f}s)")
        await job.async_startWhenReady(timeoutSeconds=timeoutSeconds)
        finalStatus = await job.async_getStatus()
        print(
            f"[indexer-light] job {job.id()} started with status={_statusName(status=finalStatus.status)}"
        )
    finally:
        stopEvent.set()
        reporterTask.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reporterTask


async def _async_maybeUseExistingJob(*, client: Client, jobId: str) -> Job | None:
    if not jobId.strip():
        print("[indexer-light] no cached job id found; creating a new async ingest job")
        return None
    print(f"[indexer-light] found cached job id {jobId}; checking if it can be reused")
    try:
        job = await client.async_getJob(jobId=jobId)
    except JobResolutionError:
        print(f"[indexer-light] cached job id {jobId} no longer exists; creating a new job")
        return None
    if job.type() != "async":
        print(f"[indexer-light] cached job id {jobId} is not async; creating a new job")
        return None

    statusResponse = await job.async_getStatus()
    print(f"[indexer-light] cached job status={_statusName(status=statusResponse.status)}")
    if statusResponse.status in {
        Proto.flow_server.JOB_STATUS_FAILED,
        Proto.flow_server.JOB_STATUS_CANCELLED,
        Proto.flow_server.JOB_STATUS_CANCEL_PENDING,
    }:
        print(f"[indexer-light] cached job {jobId} is not reusable; creating a new job")
        return None
    if statusResponse.status != Proto.flow_server.JOB_STATUS_STARTED:
        await _async_startJobWithProgress(job=job, timeoutSeconds=120.0)
    print(f"[indexer-light] reusing async indexer job {job.id()}")
    return job


def _resolveIndexerLightImages(args: argparse.Namespace) -> AsyncIngestOperatorImages:
    return AsyncIngestOperatorImages(
        structure_extractor_image_url=(
            args.structure_extractor_image.strip()
            if args.structure_extractor_image
            else _DEFAULT_STRUCTURE_EXTRACTOR_IMAGE_URL
        ),
        chunker_image_url=(
            args.chunker_image.strip()
            if args.chunker_image
            else _DEFAULT_CHUNKER_IMAGE_URL
        ),
        chromadb_indexer_image_url=(
            args.chromadb_indexer_image.strip()
            if args.chromadb_indexer_image
            else _DEFAULT_CHROMADB_INDEXER_IMAGE_URL
        ),
    )


async def _async_getOrCreateIndexerLightJob(
    *,
    client: Client,
    owner: str,
    clusterId: str,
    images: AsyncIngestOperatorImages,
    jobIdFile: Path,
    allowReuseExistingJob: bool,
) -> Job:
    if allowReuseExistingJob:
        existing = await _async_maybeUseExistingJob(client=client, jobId=_loadJobId(path=jobIdFile))
        if existing is not None:
            return existing
    else:
        print("[indexer-light] reuse disabled; submitting a fresh async ingest DAG")

    print("[indexer-light] submitting async ingest DAG")
    submitRequest = buildAsyncIngestLightSubmitJobRequest(
        owner=owner,
        cluster_id=clusterId,
        images=images,
    )
    job = await client.async_submitJob(submitRequest, startJob=False)
    print(f"[indexer-light] submitted job {job.id()}; waiting for readiness/start")
    await _async_startJobWithProgress(job=job, timeoutSeconds=120.0)
    _storeJobId(path=jobIdFile, jobId=job.id())
    print(f"[indexer-light] wrote cached job id to {jobIdFile}")
    return job


async def async_runIndexerLight(args: argparse.Namespace) -> int:
    flowServerTarget, configServiceTarget, publicHost = _resolveServiceTargets(args)
    print(f"[indexer-light] flow_server={flowServerTarget}")
    print(f"[indexer-light] config_service={configServiceTarget}")
    owner = args.owner.strip() if args.owner else _DEFAULT_OWNER
    if not owner:
        owner = _DEFAULT_OWNER

    sdk = Client(
        flowServerTarget=flowServerTarget,
        configServiceTarget=configServiceTarget,
    )
    try:
        print("[indexer-light] resolving rag_uri")
        ragUri = await _async_resolveRagUri(
            args=args,
            configServiceTarget=configServiceTarget,
            publicHost=publicHost,
        )
        print(f"[indexer-light] rag_uri={ragUri}")
        images = _resolveIndexerLightImages(args)
        print("[indexer-light] resolved operator images:")
        print(f"  structure_extractor={images.structure_extractor_image_url}")
        print(f"  chunker={images.chunker_image_url}")
        print(f"  chromadb_indexer={images.chromadb_indexer_image_url}")
        jobIdFile = Path(args.job_id_file.strip())
        job = await _async_getOrCreateIndexerLightJob(
            client=sdk,
            owner=owner,
            clusterId=args.cluster_id.strip(),
            images=images,
            jobIdFile=jobIdFile,
            allowReuseExistingJob=bool(args.reuse_existing_job),
        )

        traceId = args.trace_id.strip() if args.trace_id else f"rag-index-light-{uuid.uuid4()}"
        print(f"[indexer-light] injecting URL into job {job.id()} with trace_id={traceId}")
        injectRequest = buildAsyncIngestLightInjectMessageRequest(
            job_id=job.id(),
            uri=args.url.strip(),
            rag_uri=ragUri,
            batch_size=max(0, int(args.batch_size)),
            trace_id=traceId,
        )
        asyncClient = job.asyncClient()
        await asyncClient.async_injectMessage(message=injectRequest.message)
        print("[indexer-light] injectMessage completed")

        print(f"Indexer job: {job.id()}")
        print(f"Trace ID: {traceId}")
        print(f"RAG URI: {ragUri}")
        print(f"Indexed URL: {args.url.strip()}")
        return 0
    finally:
        await sdk.async_close()


def _buildParser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Launch or reuse a light async Lumeflow RAG indexer job and ingest one URL.",
    )
    parser.add_argument("--url", required=True, help="Document URL to ingest")
    parser.add_argument("--public-host", default="", help="Cluster public host (defaults to minikube ip)")
    parser.add_argument("--flow-server", default="", help="Flow server target host:port or parseTarget")
    parser.add_argument("--config-service", default="", help="Config service target host:port or parseTarget")
    parser.add_argument("--cluster-id", default=_DEFAULT_CLUSTER_ID, help="Cluster UUID")
    parser.add_argument("--owner", default=_DEFAULT_OWNER, help="SubmitJob owner")
    parser.add_argument("--rag-uri", default="", help="Full rag_uri (overrides endpoint+collection resolution)")
    parser.add_argument("--chroma-endpoint", default="", help="Chroma endpoint override, e.g. http://host:8000")
    parser.add_argument("--chroma-node-port", type=int, default=_DEFAULT_CHROMA_NODE_PORT, help="Chroma NodePort")
    parser.add_argument("--collection", default="", help="Collection name override")
    parser.add_argument("--batch-size", type=int, default=32, help="Chunk batching hint for structure extractor")
    parser.add_argument("--trace-id", default="", help="Optional trace_id for the ingest message")
    parser.add_argument("--job-id-file", default=_DEFAULT_JOB_ID_FILE, help="Local file used to reuse indexer job id")
    parser.add_argument(
        "--reuse-existing-job",
        action="store_true",
        help="Reuse cached async ingest job from --job-id-file instead of always submitting a fresh DAG.",
    )
    parser.add_argument("--structure-extractor-image", default="", help="Override structure extractor image URL")
    parser.add_argument("--chunker-image", default="", help="Override chunker image URL")
    parser.add_argument("--chromadb-indexer-image", default="", help="Override chromadb indexer image URL")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _buildParser()
    args = parser.parse_args(argv)
    if not args.url.strip():
        raise SystemExit("--url must not be blank")
    if args.chroma_node_port <= 0 or args.chroma_node_port > 65535:
        raise SystemExit("--chroma-node-port must be in [1, 65535]")
    try:
        return asyncio.run(async_runIndexerLight(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
