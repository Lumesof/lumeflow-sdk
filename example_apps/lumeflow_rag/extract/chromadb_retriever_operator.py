import argparse
import asyncio
import json
import logging
import os
import subprocess
import time
from typing import Any, Callable, Optional, Sequence
from urllib.parse import urlparse

import chromadb  # type: ignore
import requests
from google.protobuf import json_format, struct_pb2

from example_apps.lumeflow_rag.common import operators_pb2
from example_apps.lumeflow_rag.common.ollama_model_discovery import OllamaModelDiscovery
from example_apps.lumeflow_rag.common.ollama_model_discovery import OperatorModelDiscoveryFailureState
from lumesof.lumeflow import Operator, Proto, operator_ports
opnet_types_pb2 = Proto.opnet_types
Result = Proto.opnet_types.Result

LOG = logging.getLogger(__name__)
on_ingress = Operator.on_ingress

DEFAULT_REQUEST_TIMEOUT_SEC = 30 * 60
DEFAULT_TOP_K = 15
DEFAULT_MAX_TOP_K = 100
DEFAULT_MAX_PROMPT_CHARS = 32_000
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
            "name": "retrieval_command",
            "serialization_format": opnet_types_pb2.OpNetPayloadType.PROTO,
            "type_url": "example_apps.operators.RetrievalCommandRequest",
        },
    ],
    "egress": [
        {
            "name": "enriched_request",
            "serialization_format": opnet_types_pb2.OpNetPayloadType.PROTO,
            "type_url": "example_apps.operators.EnrichedRequest",
        },
    ],
}


@operator_ports(OPERATOR_PORTS)
class ChromaDbRetrieverOperator(Operator):
    def __init__(
        self,
        *,
        client: Optional[object] = None,
        httpPost: Optional[Callable[..., Any]] = None,
        topK: int = DEFAULT_TOP_K,
        maxTopK: int = DEFAULT_MAX_TOP_K,
        requestTimeoutSec: int = DEFAULT_REQUEST_TIMEOUT_SEC,
        maxPromptChars: int = DEFAULT_MAX_PROMPT_CHARS,
        embeddedServingUri: str = DEFAULT_EMBEDDED_SERVING_URI,
        embeddedStartupTimeoutSec: float = DEFAULT_EMBEDDED_OLLAMA_STARTUP_TIMEOUT_SEC,
        embeddedStartupPollIntervalSec: float = DEFAULT_EMBEDDED_OLLAMA_STARTUP_POLL_INTERVAL_SEC,
        preferredEmbeddingModels: Sequence[str] = DEFAULT_PREFERRED_EMBEDDING_MODELS,
        modelDiscovery: Optional[OllamaModelDiscovery] = None,
        autoStartEmbeddedOllama: bool = True,
    ) -> None:
        super().__init__()
        if maxTopK < 1:
            raise ValueError("maxTopK must be >= 1")
        if topK < 1 or topK > maxTopK:
            raise ValueError(f"topK must be within [1, {maxTopK}]")
        if requestTimeoutSec < 1 or requestTimeoutSec > DEFAULT_REQUEST_TIMEOUT_SEC:
            raise ValueError(
                f"requestTimeoutSec must be within [1, {DEFAULT_REQUEST_TIMEOUT_SEC}]"
            )
        if maxPromptChars < 1:
            raise ValueError("maxPromptChars must be >= 1")
        if embeddedStartupTimeoutSec <= 0:
            raise ValueError("embeddedStartupTimeoutSec must be > 0")
        if embeddedStartupPollIntervalSec <= 0:
            raise ValueError("embeddedStartupPollIntervalSec must be > 0")

        self._client = client
        self._collection: Optional[object] = None
        self._collectionRagUri = ""
        self._httpPost: Callable[..., Any] = httpPost or requests.post
        self._topK = topK
        self._maxTopK = maxTopK
        self._requestTimeoutSec = requestTimeoutSec
        self._maxPromptChars = maxPromptChars
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

        if autoStartEmbeddedOllama:
            self._startEmbeddedOllamaEager()
        self._discoverEmbeddingModel(modelDiscovery=modelDiscovery)

    @on_ingress(
        "retrieval_command",
    )
    async def async_retrievalCommand(
        self,
        *,
        input_port: str,
        message: operators_pb2.RetrievalCommandRequest,
    ) -> Result:
        _ = input_port
        rejectionResult = self._rejectIfDiscoveryFailed()
        if rejectionResult is not None:
            return rejectionResult
        if not message.prompt:
            return Result(ok=False, message="prompt is required")
        if len(message.prompt) > self._maxPromptChars:
            return Result(
                ok=False,
                message=(
                    f"prompt exceeds policy max length: received {len(message.prompt)}, "
                    f"max {self._maxPromptChars}"
                ),
            )
        if not message.rag_uri:
            return Result(ok=False, message="rag_uri is required")

        collection = await self._async_getCollection(ragUri=message.rag_uri)
        if collection is None:
            return Result(ok=False, message=f"invalid rag_uri: {message.rag_uri}")

        try:
            await self._async_ensureServingReady(servingUri=self._embeddedServingUri)
            messages = [self._dictFromStruct(packed) for packed in message.messages]
            messages = [self._normalizeJsonNumbers(value=packed) for packed in messages]
            toolCalls = [self._dictFromStruct(packed) for packed in message.tool_calls]
            toolCalls = [self._normalizeJsonNumbers(value=packed) for packed in toolCalls]

            if not toolCalls:
                toolCalls = [self._buildForcedRetrieveToolCall(prompt=message.prompt)]
                messages.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": toolCalls,
                    }
                )

            for toolCall in toolCalls:
                toolResponse = await self._async_executeToolCall(
                    toolCall=toolCall,
                    collection=collection,
                    defaultQuery=message.prompt,
                )
                messages.append(
                    {
                        "role": "tool",
                        "name": "retrieve_context",
                        "content": toolResponse,
                    }
                )
        except Exception as exc:
            LOG.exception("retrieval command execution failed")
            return Result(ok=False, message=str(exc))

        enrichedRequest = operators_pb2.EnrichedRequest(
            request_id=message.request_id,
            conversation_id=message.conversation_id,
            rag_uri=message.rag_uri,
            serving_uri=message.serving_uri,
            prompt=message.prompt,
            model=message.model,
            response_id=message.response_id,
            endpoint=message.endpoint,
        )
        for packedMessage in messages:
            enrichedRequest.messages.append(self._structFromDict(payload=packedMessage))

        await self.async_emit(output_port="enriched_request", message=enrichedRequest)
        return Result(ok=True, message=f"enriched request emitted: {message.request_id}")

    def _rejectIfDiscoveryFailed(self) -> Optional[Result]:
        if not self._discoveryFailureState.shouldRejectRequests():
            return None
        warningMessage = self._discoveryFailureState.rejectionMessage(
            operatorName="chromadb_retriever_operator"
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
                LOG.info(
                    "Discovered embedding model for chromadb_retriever_operator: %s",
                    model,
                )
                return
            failureReason = reason or "no embedding model discovered"
            self._discoveryFailureState.markFailed(modelKind="embedding", reason=failureReason)
            self._logAvailableModelsOnDiscoveryFailure(discovery=discovery)
            LOG.warning(
                "Embedding model discovery failed in chromadb_retriever_operator; fail-flag enabled: %s",
                failureReason,
            )
        except Exception as exc:
            self._discoveryFailureState.markFailed(modelKind="embedding", reason=str(exc))
            self._logAvailableModelsOnDiscoveryFailure(discovery=discovery)
            LOG.warning(
                "Embedding model discovery failed in chromadb_retriever_operator with exception; "
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
                "Unable to list available models for chromadb_retriever_operator after discovery failure: %s",
                exc,
            )
            return
        LOG.info(
            "Available models for chromadb_retriever_operator discovery failure: %s",
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

    async def _async_executeToolCall(
        self,
        *,
        toolCall: dict[str, Any],
        collection: object,
        defaultQuery: str,
    ) -> str:
        functionPayload = toolCall.get("function")
        if not isinstance(functionPayload, dict):
            return "tool call missing function payload"
        functionName = functionPayload.get("name")
        if functionName != "retrieve_context":
            return f"unsupported tool: {functionName}"

        rawArguments = functionPayload.get("arguments", {})
        arguments = self._parseToolArguments(rawArguments)
        query = str(arguments.get("query") or defaultQuery)
        if len(query) > self._maxPromptChars:
            raise RuntimeError(
                f"query exceeds policy max length: received {len(query)}, max {self._maxPromptChars}"
            )
        rawTopK = arguments.get("top_k")
        toolTopK = self._parseTopK(value=rawTopK, defaultValue=self._topK)
        context = await self._async_retrieveContext(
            collection=collection,
            prompt=query,
            topK=toolTopK,
        )
        if context:
            return context
        return "No matching context found."

    async def _async_retrieveContext(
        self,
        *,
        collection: object,
        prompt: str,
        topK: int,
    ) -> str:
        embedding = await self._async_embedQuery(query=prompt)
        result = await asyncio.to_thread(
            collection.query,
            query_embeddings=[embedding],
            n_results=topK,
        )
        documents = self._extractDocuments(queryResult=result)
        if not documents:
            return ""
        return "\n\n".join(f"[{idx}] {doc}" for idx, doc in enumerate(documents, start=1))

    async def _async_embedQuery(self, *, query: str) -> list[float]:
        if not self._discoveredEmbeddingModel:
            raise RuntimeError("no embedding model available")
        endpoint = self._buildEmbedUri(servingUri=self._embeddedServingUri)
        payload = {
            "model": self._discoveredEmbeddingModel,
            "input": query,
        }
        response = await asyncio.to_thread(
            self._httpPost,
            endpoint,
            json=payload,
            timeout=self._requestTimeoutSec,
        )
        response.raise_for_status()
        parsed = response.json()
        embeddings = parsed.get("embeddings")
        if not isinstance(embeddings, list) or not embeddings:
            raise RuntimeError("embed response did not contain embeddings")
        first = embeddings[0]
        if not isinstance(first, list):
            raise RuntimeError("embed response contained invalid embedding payload")
        return [float(value) for value in first]

    def _buildEmbedUri(self, *, servingUri: str) -> str:
        parsed = urlparse(servingUri)
        if parsed.scheme not in {"http", "https"}:
            raise RuntimeError(f"servingUri scheme must be http or https: {servingUri}")
        if parsed.hostname is None or parsed.port is None:
            raise RuntimeError(f"servingUri must include host and port: {servingUri}")
        return f"{servingUri.rstrip('/')}/api/embed"

    def _buildForcedRetrieveToolCall(self, *, prompt: str) -> dict[str, Any]:
        return {
            "function": {
                "name": "retrieve_context",
                "arguments": {
                    "query": prompt,
                    "top_k": self._topK,
                },
            }
        }

    def _parseToolArguments(self, rawArguments: Any) -> dict[str, Any]:
        if isinstance(rawArguments, dict):
            return rawArguments
        if isinstance(rawArguments, str):
            try:
                parsed = json.loads(rawArguments)
            except json.JSONDecodeError:
                return {}
            if isinstance(parsed, dict):
                return parsed
        return {}

    def _parseTopK(self, *, value: Any, defaultValue: int) -> int:
        parsedValue: int
        if value is None:
            parsedValue = defaultValue
        elif isinstance(value, int):
            parsedValue = value
        elif isinstance(value, float):
            if not value.is_integer():
                raise RuntimeError("top_k must be an integer when provided")
            parsedValue = int(value)
        elif isinstance(value, str):
            try:
                parsedValue = int(value)
            except ValueError as exc:
                raise RuntimeError("top_k must be an integer when provided") from exc
        else:
            raise RuntimeError("top_k must be an integer when provided")

        if parsedValue < 1 or parsedValue > self._maxTopK:
            raise RuntimeError(
                f"top_k out of policy bounds: received {parsedValue}, allowed [1, {self._maxTopK}]"
            )
        return parsedValue

    def _extractDocuments(self, *, queryResult: Any) -> list[str]:
        if not isinstance(queryResult, dict):
            return []
        documents = queryResult.get("documents")
        if not isinstance(documents, list) or not documents:
            return []
        firstBatch = documents[0]
        if not isinstance(firstBatch, list):
            return []
        return [str(item) for item in firstBatch if item]

    def _structFromDict(self, *, payload: dict[str, Any]) -> struct_pb2.Struct:
        structured = struct_pb2.Struct()
        structured.update(payload)
        return structured

    def _dictFromStruct(self, payload: struct_pb2.Struct) -> dict[str, Any]:
        parsed = json_format.MessageToDict(payload)
        if isinstance(parsed, dict):
            return parsed
        return {}

    def _normalizeJsonNumbers(self, *, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self._normalizeJsonNumbers(value=item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._normalizeJsonNumbers(value=item) for item in value]
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value

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
    operator = ChromaDbRetrieverOperator()
    await operator.async_runUntilStopped(sidecarUri)


def main(argv: Optional[list[str]] = None) -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(
        description="Start a ChromaDbRetrieverOperator and connect it to an OperatorSidecar."
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
        LOG.info("ChromaDbRetrieverOperator interrupted, shutting down.")


if __name__ == "__main__":
    main()


__all__ = ["ChromaDbRetrieverOperator"]
