from __future__ import annotations

import argparse
import asyncio
import contextlib
import subprocess
import sys
import textwrap
import time
import uuid
from dataclasses import dataclass
from shutil import get_terminal_size
from typing import Callable
from typing import TextIO
from urllib.parse import urlparse

from example_apps.lumeflow_rag.extract.image_descriptors.chromadb_retriever_operator_nomic_embed_text_v1_5 import (
    _IMAGE_DESCRIPTOR as _CHROMADB_RETRIEVER_IMAGE_DESCRIPTOR,
)
from example_apps.lumeflow_rag.extract.image_descriptors.llm_agent_operator_qwen25_1_5b import (
    _IMAGE_DESCRIPTOR as _LLM_AGENT_IMAGE_DESCRIPTOR,
)
from example_apps.lumeflow_rag.extract.agent_flow_light import buildSyncAgentLightSubmitJobRequest
from example_apps.lumeflow_rag.extract.agent_flow_light import SyncAgentOperatorImages
from example_apps.lumeflow_rag.common.rag_uri import parseRagUri
from example_apps.lumeflow_rag.common import operators_pb2
from lumesof.lumeflow import ConfigQueryClientError
from lumesof.lumeflow import async_getRequiredStringConfigValue
from lumesof.lumeflow import Client
from lumesof.lumeflow import Job
from lumesof.lumeflow import Proto
from lumesof.pylib import Net


_DEFAULT_CLUSTER_ID = "11111111-1111-1111-1111-111111111111"
_DEFAULT_OWNER = "lumeflow-rag-agent-light"
_DEFAULT_APP_ID = "lumeflow-rag-agent-light-local"
_DEFAULT_RAG_ENDPOINT = "http://chromadb:8000"
_DEFAULT_RAG_COLLECTION = "default"
_DEFAULT_CHROMA_NODE_PORT = 30090
_DEFAULT_FLOW_SERVER_NODEPORT = 30070
_DEFAULT_CONFIG_SERVICE_NODEPORT = 30074
_DEFAULT_RPC_TIMEOUT_MS = 300_000
_DEFAULT_LLM_AGENT_IMAGE_URL = _LLM_AGENT_IMAGE_DESCRIPTOR.imageUrl
_DEFAULT_CHROMADB_RETRIEVER_IMAGE_URL = _CHROMADB_RETRIEVER_IMAGE_DESCRIPTOR.imageUrl
_CANONICAL_ANY_TYPE_PREFIX = "type.googleapis.com/"

_BRIDGE_NODEPORTS = {
    "rpc-opnet-bridge-shard0-replica0-endpoint": 30080,
    "rpc-opnet-bridge-shard0-replica1-endpoint": 30081,
}


@dataclass(frozen=True)
class ChatBootstrapResult:
    appId: str
    jobId: str
    ragUri: str
    flowServerTarget: str
    configServiceTarget: str
    publicHost: str


@dataclass(frozen=True)
class ChatTranscriptEntry:
    role: str
    text: str


def _statusName(*, status: int) -> str:
    try:
        return Proto.flow_server.JobStatus.Name(status)
    except ValueError:
        return str(status)


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

    return flowServerTarget, configServiceTarget, publicHost


def _resolveOwnerAndAppId(args: argparse.Namespace) -> tuple[str, str]:
    owner = args.owner.strip() if args.owner else _DEFAULT_OWNER
    if not owner:
        owner = _DEFAULT_OWNER
    appId = args.app_id.strip() if args.app_id else _DEFAULT_APP_ID
    if not appId:
        appId = _DEFAULT_APP_ID
    return owner, appId


def _resolveLightAgentImages(args: argparse.Namespace) -> SyncAgentOperatorImages:
    return SyncAgentOperatorImages(
        llm_agent_image_url=(
            args.llm_agent_image.strip()
            if args.llm_agent_image
            else _DEFAULT_LLM_AGENT_IMAGE_URL
        ),
        chromadb_retriever_image_url=(
            args.chromadb_retriever_image.strip()
            if args.chromadb_retriever_image
            else _DEFAULT_CHROMADB_RETRIEVER_IMAGE_URL
        ),
    )


def _remapBridgeEndpoint(*, addr: str, host: str) -> str:
    normalized = _normalizeTarget(addr)
    parsed = Net.NetTarget.parseTarget(normalized)
    serviceHost = parsed.getHost()
    if serviceHost is None:
        return addr
    nodePort = _BRIDGE_NODEPORTS.get(serviceHost)
    if nodePort is None:
        return addr
    return f"{host}:{nodePort}"


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


async def _async_findActiveSyncJobByAppId(*, client: Client, appId: str) -> Job | None:
    listResponse = await client.async_listJobs()
    for listedJob in listResponse.jobs:
        if listedJob.app_id != appId:
            continue
        normalizedType = (listedJob.job_type or "").strip().upper()
        if normalizedType not in {"SYNC", "SYNCHRONOUS"}:
            continue
        return await client.async_getJob(jobId=listedJob.job_id)
    return None


async def _async_waitForJobStatusWithProgress(
    *,
    job: Job,
    targetStatus: int,
    timeoutSeconds: float,
    phase: str,
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
                        f"[agent-light] {phase} status={_statusName(status=status)} "
                        f"elapsed_s={elapsedSeconds:.1f}"
                    )
                    lastStatus = status
            except Exception as exc:
                print(f"[agent-light] status poll warning during {phase}: {exc}")

            try:
                await asyncio.wait_for(stopEvent.wait(), timeout=pollSeconds)
            except TimeoutError:
                pass

    reporterTask = asyncio.create_task(_async_reportProgress())
    try:
        targetName = _statusName(status=targetStatus)
        print(f"[agent-light] waiting for {targetName} (timeout={timeoutSeconds:.0f}s)")
        await job.async_waitForStatus(
            targetStatus=targetStatus,
            timeoutSeconds=timeoutSeconds,
            pollIntervalSeconds=pollSeconds,
        )
        print(f"[agent-light] reached {targetName}")
    finally:
        stopEvent.set()
        reporterTask.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reporterTask


async def _async_startSyncJobWithProgress(
    *,
    client: Client,
    job: Job,
    timeoutSeconds: float,
) -> None:
    initialStatus = await job.async_getStatus()
    if initialStatus.status == Proto.flow_server.JOB_STATUS_STARTED:
        print(f"[agent-light] job {job.id()} already STARTED")
        return

    if initialStatus.status != Proto.flow_server.JOB_STATUS_CREATED:
        await _async_waitForJobStatusWithProgress(
            job=job,
            targetStatus=Proto.flow_server.JOB_STATUS_CREATED,
            timeoutSeconds=timeoutSeconds,
            phase="wait-create",
        )

    print(f"[agent-light] start job {job.id()}")
    await client.async_startJob(jobId=job.id())
    print(f"[agent-light] start rpc accepted for job {job.id()}")

    await _async_waitForJobStatusWithProgress(
        job=job,
        targetStatus=Proto.flow_server.JOB_STATUS_STARTED,
        timeoutSeconds=timeoutSeconds,
        phase="wait-started",
    )


async def _async_ensureSyncLightAgentJob(
    *,
    client: Client,
    appId: str,
    owner: str,
    clusterId: str,
    images: SyncAgentOperatorImages,
    reuseExisting: bool,
) -> Job:
    existingJob = await _async_findActiveSyncJobByAppId(client=client, appId=appId)
    if existingJob is not None:
        if not reuseExisting:
            raise RuntimeError(
                f"Found active sync job {existingJob.id()} for app_id={appId}. "
                "Refusing to reuse by default; pass --reuse-existing to reuse it "
                "or pick a new --app-id."
            )
        statusResponse = await existingJob.async_getStatus()
        if statusResponse.status != Proto.flow_server.JOB_STATUS_STARTED:
            await _async_startSyncJobWithProgress(
                client=client,
                job=existingJob,
                timeoutSeconds=120.0,
            )
        else:
            print(f"[agent-light] reusing started sync job {existingJob.id()}")
        return existingJob

    print("[agent-light] submit sync DAG")
    submitRequest = buildSyncAgentLightSubmitJobRequest(
        owner=owner,
        cluster_id=clusterId,
        app_id=appId,
        images=images,
    )
    job = await client.async_submitJob(submitRequest, startJob=False)
    print(f"[agent-light] submit done job_id={job.id()}")
    await _async_startSyncJobWithProgress(
        client=client,
        job=job,
        timeoutSeconds=120.0,
    )
    print(f"[agent-light] job {job.id()} is STARTED")
    return job


def requireInteractiveTerminal(
    *,
    stdin: TextIO,
    stdout: TextIO,
) -> None:
    if not stdin.isatty() or not stdout.isatty():
        raise RuntimeError(
            "chat_agent_light_bin requires an interactive terminal (TTY) on stdin and stdout."
        )


async def async_bootstrapLightChatSession(args: argparse.Namespace) -> ChatBootstrapResult:
    flowServerTarget, configServiceTarget, publicHost = _resolveServiceTargets(args)
    owner, appId = _resolveOwnerAndAppId(args)
    sdk = Client(
        flowServerTarget=flowServerTarget,
        configServiceTarget=configServiceTarget,
        endpointRemapper=lambda addr: _remapBridgeEndpoint(addr=addr, host=publicHost),
    )
    try:
        ragUri = await _async_resolveRagUri(
            args=args,
            configServiceTarget=configServiceTarget,
            publicHost=publicHost,
        )
        images = _resolveLightAgentImages(args)
        job = await _async_ensureSyncLightAgentJob(
            client=sdk,
            appId=appId,
            owner=owner,
            clusterId=args.cluster_id.strip(),
            images=images,
            reuseExisting=bool(args.reuse_existing),
        )
        return ChatBootstrapResult(
            appId=appId,
            jobId=job.id(),
            ragUri=ragUri,
            flowServerTarget=flowServerTarget,
            configServiceTarget=configServiceTarget,
            publicHost=publicHost,
        )
    finally:
        await sdk.async_close()


def _renderTranscript(
    *,
    bootstrap: ChatBootstrapResult,
    transcript: list[ChatTranscriptEntry],
    stdout: TextIO,
) -> None:
    terminalSize = get_terminal_size(fallback=(120, 40))
    width = max(60, terminalSize.columns)
    height = max(20, terminalSize.lines)
    reservedLines = 8
    transcriptLinesBudget = max(4, height - reservedLines)

    renderedLines: list[str] = []
    for entry in transcript:
        prefix = "You: " if entry.role == "user" else "Agent: "
        wrapped = textwrap.wrap(
            entry.text.strip() if entry.text.strip() else "(empty)",
            width=max(10, width - len(prefix)),
        )
        if not wrapped:
            wrapped = ["(empty)"]
        renderedLines.append(f"{prefix}{wrapped[0]}")
        for continuation in wrapped[1:]:
            renderedLines.append(f"{' ' * len(prefix)}{continuation}")

    if len(renderedLines) > transcriptLinesBudget:
        renderedLines = renderedLines[-transcriptLinesBudget:]

    stdout.write("\x1b[2J\x1b[H")
    stdout.write("Lumeflow RAG Chat (Light)\n")
    stdout.write(f"App ID: {bootstrap.appId}\n")
    stdout.write(f"Job ID: {bootstrap.jobId}\n")
    stdout.write(f"RAG URI: {bootstrap.ragUri}\n")
    stdout.write("Type /quit to exit.\n")
    stdout.write("-" * min(width, 120))
    stdout.write("\n")
    if not renderedLines:
        stdout.write("(no messages yet)\n")
    else:
        for line in renderedLines:
            stdout.write(f"{line}\n")
    stdout.flush()


async def _async_sendPrompt(
    *,
    args: argparse.Namespace,
    prompt: str,
    bootstrap: ChatBootstrapResult,
) -> str:
    sdk = Client(
        flowServerTarget=bootstrap.flowServerTarget,
        configServiceTarget=bootstrap.configServiceTarget,
        endpointRemapper=lambda addr: _remapBridgeEndpoint(addr=addr, host=bootstrap.publicHost),
    )
    try:
        ragUri = await _async_resolveRagUri(
            args=args,
            configServiceTarget=bootstrap.configServiceTarget,
            publicHost=bootstrap.publicHost,
        )
        job = await sdk.async_getJob(jobId=bootstrap.jobId)
        statusResponse = await job.async_getStatus()
        if statusResponse.status != Proto.flow_server.JOB_STATUS_STARTED:
            await _async_startSyncJobWithProgress(
                client=sdk,
                job=job,
                timeoutSeconds=120.0,
            )
        request = operators_pb2.AgentRequest(
            rag_uri=ragUri,
            prompt=prompt,
            response_id=f"response-{uuid.uuid4()}",
            conversation_id=args.conversation_id.strip(),
        )
        syncClient = job.syncClient()
        try:
            payload = await syncClient.async_call(
                payload=request.SerializeToString(),
                payloadTypeUrl=f"{_CANONICAL_ANY_TYPE_PREFIX}{operators_pb2.AgentRequest.DESCRIPTOR.full_name}",
                payloadSerializationFormat=int(Proto.opnet_types.OpNetPayloadType.PROTO),
                timeoutMs=max(1, int(args.timeout_ms)),
            )
        finally:
            await syncClient.async_close()

        response = operators_pb2.RespondRequest()
        response.ParseFromString(payload)
        return response.response
    finally:
        await sdk.async_close()


def _runChatLoop(
    *,
    args: argparse.Namespace,
    bootstrap: ChatBootstrapResult,
    stdin: TextIO,
    stdout: TextIO,
    inputFn: Callable[[str], str] = input,
) -> int:
    transcript: list[ChatTranscriptEntry] = []

    while True:
        _renderTranscript(bootstrap=bootstrap, transcript=transcript, stdout=stdout)
        try:
            userMessage = inputFn("You> ").strip()
        except EOFError:
            stdout.write("\n")
            stdout.flush()
            return 0
        except KeyboardInterrupt:
            stdout.write("\n")
            stdout.flush()
            return 130
        if not userMessage:
            continue
        if userMessage in {"/quit", "/exit", "quit", "exit"}:
            return 0

        transcript.append(ChatTranscriptEntry(role="user", text=userMessage))
        try:
            response = asyncio.run(_async_sendPrompt(args=args, prompt=userMessage, bootstrap=bootstrap))
            transcript.append(ChatTranscriptEntry(role="assistant", text=response))
        except KeyboardInterrupt:
            return 130
        except Exception as exc:  # pragma: no cover - network/runtime dependent
            transcript.append(ChatTranscriptEntry(role="assistant", text=f"[error] {exc}"))


def buildParser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an interactive Lumeflow RAG chat session over the light sync loop DAG.",
    )
    parser.add_argument("--public-host", default="", help="Cluster public host (defaults to minikube ip)")
    parser.add_argument("--flow-server", default="", help="Flow server target host:port or parseTarget")
    parser.add_argument("--config-service", default="", help="Config service target host:port or parseTarget")
    parser.add_argument("--cluster-id", default=_DEFAULT_CLUSTER_ID, help="Cluster UUID")
    parser.add_argument("--owner", default=_DEFAULT_OWNER, help="SubmitJob owner")
    parser.add_argument("--app-id", default=_DEFAULT_APP_ID, help="Sync app id used for bridge routing")
    parser.add_argument(
        "--reuse-existing",
        action="store_true",
        help="Reuse an active sync job with the same --app-id instead of failing.",
    )
    parser.add_argument("--rag-uri", default="", help="Full rag_uri (overrides endpoint+collection resolution)")
    parser.add_argument("--chroma-endpoint", default="", help="Chroma endpoint override, e.g. http://host:8000")
    parser.add_argument("--chroma-node-port", type=int, default=_DEFAULT_CHROMA_NODE_PORT, help="Chroma NodePort")
    parser.add_argument("--collection", default="", help="Collection name override")
    parser.add_argument("--conversation-id", default="", help="Optional conversation id")
    parser.add_argument("--timeout-ms", type=int, default=_DEFAULT_RPC_TIMEOUT_MS, help="Bridge RPC timeout in ms")
    parser.add_argument("--llm-agent-image", default="", help="Override llm agent operator image URL")
    parser.add_argument(
        "--chromadb-retriever-image",
        default="",
        help="Override chromadb retriever operator image URL",
    )
    parser.add_argument(
        "--skip-tty-check",
        action="store_true",
        help="Allow non-interactive execution (for tests and diagnostics).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = buildParser()
    args = parser.parse_args(argv)
    if args.chroma_node_port <= 0 or args.chroma_node_port > 65535:
        raise SystemExit("--chroma-node-port must be in [1, 65535]")
    if not args.skip_tty_check:
        try:
            requireInteractiveTerminal(stdin=sys.stdin, stdout=sys.stdout)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
    try:
        result = asyncio.run(async_bootstrapLightChatSession(args))
    except KeyboardInterrupt:
        return 130
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    return _runChatLoop(args=args, bootstrap=result, stdin=sys.stdin, stdout=sys.stdout)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
