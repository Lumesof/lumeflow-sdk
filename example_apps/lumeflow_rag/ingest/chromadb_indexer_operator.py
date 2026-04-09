import argparse
import asyncio
import hashlib
import json
import logging
import os
import subprocess
import time
from typing import Any, Callable, Optional, Sequence
from urllib.parse import urlparse

import chromadb  # type: ignore
import requests

from example_apps.lumeflow_rag.common import operators_pb2
from example_apps.lumeflow_rag.common.ollama_model_discovery import OllamaModelDiscovery
from example_apps.lumeflow_rag.common.ollama_model_discovery import OperatorModelDiscoveryFailureState
from lumesof.lumeflow import Operator, Proto, operator_ports
opnet_types_pb2 = Proto.opnet_types
Result = Proto.opnet_types.Result

LOG = logging.getLogger(__name__)
on_ingress = Operator.on_ingress

DEFAULT_REQUEST_TIMEOUT_SEC = 30 * 60
DEFAULT_EMBEDDED_OLLAMA_HOST = "127.0.0.1"
DEFAULT_EMBEDDED_OLLAMA_PORT = 40001
DEFAULT_EMBEDDED_OLLAMA_STARTUP_TIMEOUT_SEC = 10.0
DEFAULT_EMBEDDED_OLLAMA_STARTUP_POLL_INTERVAL_SEC = 0.5
DEFAULT_EMBEDDED_SERVING_URI = f"http://{DEFAULT_EMBEDDED_OLLAMA_HOST}:{DEFAULT_EMBEDDED_OLLAMA_PORT}"
DEFAULT_MODEL_DISCOVERY_TIMEOUT_SEC = 30.0
DEFAULT_PREFERRED_EMBEDDING_MODELS = (
    "nomic-embed-text:v1.5",
    "nomic-embed-text:v2-moe",
    "mxbai-embed-large",
)

OPERATOR_PORTS = {
    "ingress": [
        {
            "name": "store",
            "serialization_format": opnet_types_pb2.OpNetPayloadType.PROTO,
            "type_url": "example_apps.operators.StoreRequest",
        },
    ],
    "egress": [],
}


@operator_ports(OPERATOR_PORTS)
class ChromaDbIndexerOperator(Operator):
    def __init__(
        self,
        *,
        client: Optional[object] = None,
        httpPost: Optional[Callable[..., Any]] = None,
        requestTimeoutSec: int = DEFAULT_REQUEST_TIMEOUT_SEC,
        embeddedServingUri: str = DEFAULT_EMBEDDED_SERVING_URI,
        embeddedStartupTimeoutSec: float = DEFAULT_EMBEDDED_OLLAMA_STARTUP_TIMEOUT_SEC,
        embeddedStartupPollIntervalSec: float = DEFAULT_EMBEDDED_OLLAMA_STARTUP_POLL_INTERVAL_SEC,
        preferredEmbeddingModels: Sequence[str] = DEFAULT_PREFERRED_EMBEDDING_MODELS,
        modelDiscovery: Optional[OllamaModelDiscovery] = None,
        autoStartEmbeddedOllama: bool = True,
    ) -> None:
        super().__init__()
        if requestTimeoutSec < 1 or requestTimeoutSec > DEFAULT_REQUEST_TIMEOUT_SEC:
            raise ValueError(
                f"requestTimeoutSec must be within [1, {DEFAULT_REQUEST_TIMEOUT_SEC}]"
            )
        if embeddedStartupTimeoutSec <= 0:
            raise ValueError("embeddedStartupTimeoutSec must be > 0")
        if embeddedStartupPollIntervalSec <= 0:
            raise ValueError("embeddedStartupPollIntervalSec must be > 0")

        self._client = client
        self._collection: Optional[object] = None
        self._collectionRagUri = ""
        self._httpPost: Callable[..., Any] = httpPost or requests.post
        self._requestTimeoutSec = requestTimeoutSec
        self._embeddedServingUri = embeddedServingUri.strip() or DEFAULT_EMBEDDED_SERVING_URI
        self._embeddedStartupTimeoutSec = embeddedStartupTimeoutSec
        self._embeddedStartupPollIntervalSec = embeddedStartupPollIntervalSec
        self._preferredEmbeddingModels = tuple(
            model.strip() for model in preferredEmbeddingModels if model.strip()
        )

        self._embeddedOllamaProcess: Optional[subprocess.Popen[Any]] = None
        self._embeddedStartupError: Optional[str] = None
        self._embeddedOllamaStartLock = asyncio.Lock()
        self._discoveryFailureState = OperatorModelDiscoveryFailureState()
        self._discoveredEmbeddingModel = ""
        self._seenDocumentHashes: set[str] = set()
        self._seenDocumentHashesLock = asyncio.Lock()

        if autoStartEmbeddedOllama:
            self._startEmbeddedOllamaEager()
        self._discoverEmbeddingModel(modelDiscovery=modelDiscovery)

    @on_ingress(
        "store",
    )
    async def async_store(
        self,
        *,
        input_port: str,
        message: operators_pb2.StoreRequest,
    ) -> Result:
        _ = input_port
        rejectionResult = self._rejectIfDiscoveryFailed()
        if rejectionResult is not None:
            return rejectionResult

        messageContext = self.currentMessageContext()
        traceId = ""
        if messageContext is not None and messageContext.trace_id:
            traceId = messageContext.trace_id

        batchHashes = {self._computeDocumentHash(document=document) for document in message.documents}
        batchSize = len(message.documents)
        documentSizesCsv = ",".join(str(len(document.text or "")) for document in message.documents)
        uniqueInBatch = len(batchHashes)
        duplicatesInBatch = batchSize - uniqueInBatch
        async with self._seenDocumentHashesLock:
            seenBeforeInBatch = sum(1 for docHash in batchHashes if docHash in self._seenDocumentHashes)
            self._seenDocumentHashes.update(batchHashes)
            uniqueSeenTotal = len(self._seenDocumentHashes)

        LOG.debug(
            "indexer batch received documents=%d document_sizes=%s unique_in_batch=%d duplicates_in_batch=%d "
            "seen_before_in_batch=%d unique_seen_total=%d rag_uri=%s trace_id=%s",
            batchSize,
            documentSizesCsv,
            uniqueInBatch,
            duplicatesInBatch,
            seenBeforeInBatch,
            uniqueSeenTotal,
            message.rag_uri,
            traceId or "<none>",
        )

        if not message.documents:
            return Result(ok=True, message="no documents to index")

        collection = await self._async_getCollection(ragUri=message.rag_uri)
        if collection is None:
            return Result(ok=False, message=f"invalid rag_uri: {message.rag_uri}")

        try:
            await self._async_ensureServingReady(servingUri=self._embeddedServingUri)
            payload = self._buildPayload(documents=message.documents)
            embeddings = await self._async_embedDocuments(documents=payload["documents"])
            if len(embeddings) != len(payload["documents"]):
                raise RuntimeError(
                    "embedding count mismatch for store request: "
                    f"docs={len(payload['documents'])}, embeddings={len(embeddings)}"
                )
            await asyncio.to_thread(
                collection.add,
                documents=payload["documents"],
                embeddings=embeddings,
                metadatas=payload["metadatas"],
                ids=payload["ids"],
            )
        except Exception as exc:
            LOG.exception("indexer store execution failed")
            return Result(ok=False, message=str(exc))

        return Result(ok=True, message=f"indexed {len(message.documents)} documents")

    def _computeDocumentHash(self, *, document: operators_pb2.DocumentProto) -> str:
        payload = {
            "text": document.text,
            "metadata": dict(document.metadata),
        }
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _rejectIfDiscoveryFailed(self) -> Optional[Result]:
        if not self._discoveryFailureState.shouldRejectRequests():
            return None
        warningMessage = self._discoveryFailureState.rejectionMessage(
            operatorName="chromadb_indexer_operator"
        )
        LOG.warning(warningMessage)
        return Result(ok=False, message=warningMessage)

    def _discoverEmbeddingModel(self, *, modelDiscovery: Optional[OllamaModelDiscovery]) -> None:
        discovery: Optional[OllamaModelDiscovery] = None
        try:
            discovery = modelDiscovery or OllamaModelDiscovery(
                servingUri=self._embeddedServingUri,
                timeoutSec=DEFAULT_MODEL_DISCOVERY_TIMEOUT_SEC,
            )
            model, reason = discovery.discoverEmbeddingModel(
                preferredModels=self._preferredEmbeddingModels
            )
            if model:
                self._discoveredEmbeddingModel = model
                LOG.info("Discovered embedding model for chromadb_indexer_operator: %s", model)
                return
            failureReason = reason or "no embedding model discovered"
            self._discoveryFailureState.markFailed(modelKind="embedding", reason=failureReason)
            self._logAvailableModelsOnDiscoveryFailure(discovery=discovery)
            LOG.warning(
                "Embedding model discovery failed in chromadb_indexer_operator; fail-flag enabled: %s",
                failureReason,
            )
        except Exception as exc:
            self._discoveryFailureState.markFailed(modelKind="embedding", reason=str(exc))
            self._logAvailableModelsOnDiscoveryFailure(discovery=discovery)
            LOG.warning(
                "Embedding model discovery failed in chromadb_indexer_operator with exception; "
                "fail-flag enabled: %s",
                exc,
            )

    def _logAvailableModelsOnDiscoveryFailure(
        self,
        *,
        discovery: Optional[OllamaModelDiscovery],
    ) -> None:
        if discovery is None:
            return
        try:
            availableModels = discovery.listAvailableModels()
        except Exception as exc:
            LOG.info(
                "Unable to list available models for chromadb_indexer_operator after discovery failure: %s",
                exc,
            )
            return
        LOG.info(
            "Available models for chromadb_indexer_operator discovery failure: %s",
            availableModels,
        )

    async def _async_getCollection(self, *, ragUri: str) -> Optional[object]:
        if self._collection is not None and ragUri == self._collectionRagUri:
            return self._collection

        parsed = self._parseRagUri(ragUri=ragUri)
        if parsed is None:
            return None
        host, port, useSsl, collectionName = parsed

        client = self._client
        if client is None:
            client = chromadb.HttpClient(host=host, port=port, ssl=useSsl)  # type: ignore[call-arg]
            self._client = client

        collection = await asyncio.to_thread(client.get_or_create_collection, collectionName)
        self._collection = collection
        self._collectionRagUri = ragUri
        return collection

    async def _async_embedDocuments(self, *, documents: list[str]) -> list[list[float]]:
        if not self._discoveredEmbeddingModel:
            raise RuntimeError("no embedding model available")
        endpoint = self._buildEmbedUri(servingUri=self._embeddedServingUri)
        payload = {
            "model": self._discoveredEmbeddingModel,
            "input": documents,
        }
        response = await asyncio.to_thread(
            self._httpPost,
            endpoint,
            json=payload,
            timeout=self._requestTimeoutSec,
        )
        response.raise_for_status()
        parsed = response.json()
        rawEmbeddings = parsed.get("embeddings")
        if not isinstance(rawEmbeddings, list):
            raise RuntimeError("embed response did not contain embeddings list")
        embeddings: list[list[float]] = []
        for raw in rawEmbeddings:
            if not isinstance(raw, list):
                raise RuntimeError("embed response contained invalid embedding payload")
            embeddings.append([float(value) for value in raw])
        return embeddings

    def _buildPayload(self, *, documents: Sequence[operators_pb2.DocumentProto]) -> dict[str, list[Any]]:
        texts: list[str] = []
        metadatas: list[dict[str, str]] = []
        ids: list[str] = []
        for idx, document in enumerate(documents, start=1):
            metadata = dict(document.metadata)
            docId = metadata.get("id") or f"doc-{idx}"
            texts.append(document.text)
            metadatas.append(metadata)
            ids.append(docId)
        return {
            "documents": texts,
            "metadatas": metadatas,
            "ids": ids,
        }

    def _buildEmbedUri(self, *, servingUri: str) -> str:
        parsed = urlparse(servingUri)
        if parsed.scheme not in {"http", "https"}:
            raise RuntimeError(f"servingUri scheme must be http or https: {servingUri}")
        if parsed.hostname is None or parsed.port is None:
            raise RuntimeError(f"servingUri must include host and port: {servingUri}")
        return f"{servingUri.rstrip('/')}/api/embed"

    def _parseRagUri(self, *, ragUri: str) -> Optional[tuple[str, int, bool, str]]:
        if not ragUri:
            LOG.warning("rag_uri must be provided in the form https://<host>:<port>/<collection>")
            return None

        parsed = urlparse(ragUri)
        if parsed.scheme not in {"http", "https"}:
            LOG.warning("rag_uri scheme must be http or https: %s", ragUri)
            return None
        if parsed.hostname is None or parsed.port is None:
            LOG.warning("rag_uri must include host and port: %s", ragUri)
            return None

        collectionName = parsed.path.lstrip("/")
        if not collectionName:
            LOG.warning("rag_uri must include the collection path: %s", ragUri)
            return None
        return parsed.hostname, parsed.port, parsed.scheme == "https", collectionName

    async def _async_ensureServingReady(self, *, servingUri: str) -> None:
        if not self._shouldUseEmbeddedServingUri(servingUri=servingUri):
            return
        if await self._async_isServingReady(servingUri=servingUri):
            return
        async with self._embeddedOllamaStartLock:
            if await self._async_isServingReady(servingUri=servingUri):
                return
            if self._embeddedOllamaProcess is None or self._embeddedOllamaProcess.poll() is not None:
                self._startEmbeddedOllamaProcess(servingUri=servingUri)
            await self._async_waitForServingReady(servingUri=servingUri)

    async def _async_waitForServingReady(self, *, servingUri: str) -> None:
        deadline = time.monotonic() + self._embeddedStartupTimeoutSec
        while time.monotonic() < deadline:
            if await self._async_isServingReady(servingUri=servingUri):
                self._embeddedStartupError = None
                return
            await asyncio.sleep(self._embeddedStartupPollIntervalSec)
        processStatus = self._embeddedProcessStatus()
        startupError = self._embeddedStartupError or "unknown startup failure"
        raise RuntimeError(
            f"embedded ollama did not become ready at {servingUri} within "
            f"{self._embeddedStartupTimeoutSec:.1f}s (process={processStatus}; error={startupError})"
        )

    async def _async_isServingReady(self, *, servingUri: str) -> bool:
        tagsUri = f"{servingUri.rstrip('/')}/api/tags"

        def isReady() -> bool:
            try:
                response = requests.get(tagsUri, timeout=1.0)
            except requests.RequestException:
                return False
            return response.status_code == 200

        return await asyncio.to_thread(isReady)

    def _startEmbeddedOllamaEager(self) -> None:
        if not self._shouldUseEmbeddedServingUri(servingUri=self._embeddedServingUri):
            return
        try:
            if self._isServingReadySync(servingUri=self._embeddedServingUri):
                return
            self._startEmbeddedOllamaProcess(servingUri=self._embeddedServingUri)
            self._waitForServingReadySync(servingUri=self._embeddedServingUri)
        except Exception as exc:
            self._embeddedStartupError = str(exc)
            LOG.warning("Embedded Ollama eager startup failed: %s", exc)

    def _startEmbeddedOllamaProcess(self, *, servingUri: str) -> None:
        parsed = urlparse(servingUri)
        if parsed.hostname is None or parsed.port is None:
            raise RuntimeError(f"embedded servingUri must include host and port: {servingUri}")
        if parsed.scheme not in {"http", "https"}:
            raise RuntimeError(f"embedded servingUri scheme must be http or https: {servingUri}")
        env = os.environ.copy()
        env["OLLAMA_HOST"] = f"{parsed.hostname}:{parsed.port}"
        try:
            process = subprocess.Popen(
                ["/usr/bin/ollama", "serve"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            self._embeddedStartupError = str(exc)
            raise RuntimeError(f"failed to launch embedded ollama process: {exc}") from exc
        self._embeddedOllamaProcess = process
        self._embeddedStartupError = None
        LOG.info("Started embedded Ollama process pid=%s host=%s", process.pid, env["OLLAMA_HOST"])

    def _waitForServingReadySync(self, *, servingUri: str) -> None:
        deadline = time.monotonic() + self._embeddedStartupTimeoutSec
        while time.monotonic() < deadline:
            if self._isServingReadySync(servingUri=servingUri):
                return
            time.sleep(self._embeddedStartupPollIntervalSec)
        processStatus = self._embeddedProcessStatus()
        raise RuntimeError(
            f"embedded ollama did not become ready at {servingUri} within "
            f"{self._embeddedStartupTimeoutSec:.1f}s (process={processStatus})"
        )

    def _isServingReadySync(self, *, servingUri: str) -> bool:
        tagsUri = f"{servingUri.rstrip('/')}/api/tags"
        try:
            response = requests.get(tagsUri, timeout=1.0)
        except requests.RequestException:
            return False
        return response.status_code == 200

    def _shouldUseEmbeddedServingUri(self, *, servingUri: str) -> bool:
        embedded = self._parseServingHostPort(servingUri=self._embeddedServingUri)
        candidate = self._parseServingHostPort(servingUri=servingUri)
        if embedded is None or candidate is None:
            return False
        return embedded == candidate

    def _parseServingHostPort(self, *, servingUri: str) -> Optional[tuple[str, int]]:
        parsed = urlparse(servingUri)
        if parsed.scheme not in {"http", "https"}:
            return None
        if parsed.hostname is None or parsed.port is None:
            return None
        return parsed.hostname, parsed.port

    def _embeddedProcessStatus(self) -> str:
        process = self._embeddedOllamaProcess
        if process is None:
            return "not-started"
        returnCode = process.poll()
        if returnCode is None:
            return f"running(pid={process.pid})"
        return f"exited(code={returnCode})"


async def _asyncMain(sidecarUri: str) -> None:
    operator = ChromaDbIndexerOperator()
    await operator.async_runUntilStopped(sidecarUri)


def main(argv: Optional[list[str]] = None) -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(
        description="Start a ChromaDbIndexerOperator and connect it to an OperatorSidecar."
    )
    parser.add_argument(
        "--sidecar-uri",
        default="tcp://127.0.0.1:50051",
        help="OperatorSidecar URI to connect to (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    try:
        asyncio.run(_asyncMain(args.sidecar_uri))
    except KeyboardInterrupt:
        LOG.info("ChromaDbIndexerOperator interrupted, shutting down.")


if __name__ == "__main__":
    main()


__all__ = ["ChromaDbIndexerOperator"]
