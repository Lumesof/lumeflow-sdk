import argparse
import asyncio
import logging
import os
import subprocess
import time
import uuid
from typing import Any, Callable, Optional, Sequence
from urllib.parse import urlparse

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
DEFAULT_MAX_TOOL_CALLS_PER_TURN = 1
DEFAULT_MAX_PROMPT_CHARS = 32_000
DEFAULT_EMBEDDED_OLLAMA_HOST = "127.0.0.1"
DEFAULT_EMBEDDED_OLLAMA_PORT = 40001
DEFAULT_EMBEDDED_OLLAMA_STARTUP_TIMEOUT_SEC = 10.0
DEFAULT_EMBEDDED_OLLAMA_STARTUP_POLL_INTERVAL_SEC = 0.5
DEFAULT_EMBEDDED_SERVING_URI = f"http://{DEFAULT_EMBEDDED_OLLAMA_HOST}:{DEFAULT_EMBEDDED_OLLAMA_PORT}"
DEFAULT_MODEL_DISCOVERY_TIMEOUT_SEC = 30.0
DEFAULT_PREFERRED_LLM_MODELS = (
    "qwen2.5:3b",
    "qwen2.5:1.5b",
    "qwen2.5:7b",
)

OPERATOR_PORTS = {
    "ingress": [
        {
            "name": "initial_request",
            "serialization_format": opnet_types_pb2.OpNetPayloadType.PROTO,
            "type_url": "example_apps.operators.AgentRequest",
        },
        {
            "name": "enriched_request",
            "serialization_format": opnet_types_pb2.OpNetPayloadType.PROTO,
            "type_url": "example_apps.operators.EnrichedRequest",
        },
    ],
    "egress": [
        {
            "name": "retrieval_command",
            "serialization_format": opnet_types_pb2.OpNetPayloadType.PROTO,
            "type_url": "example_apps.operators.RetrievalCommandRequest",
        },
        {
            "name": "final_text",
            "serialization_format": opnet_types_pb2.OpNetPayloadType.PROTO,
            "type_url": "example_apps.operators.RespondRequest",
        },
    ],
}


@operator_ports(OPERATOR_PORTS)
class LlmAgentOperator(Operator):
    def __init__(
        self,
        *,
        httpPost: Optional[Callable[..., Any]] = None,
        requestTimeoutSec: int = DEFAULT_REQUEST_TIMEOUT_SEC,
        maxToolCallsPerTurn: int = DEFAULT_MAX_TOOL_CALLS_PER_TURN,
        maxPromptChars: int = DEFAULT_MAX_PROMPT_CHARS,
        embeddedServingUri: str = DEFAULT_EMBEDDED_SERVING_URI,
        embeddedStartupTimeoutSec: float = DEFAULT_EMBEDDED_OLLAMA_STARTUP_TIMEOUT_SEC,
        embeddedStartupPollIntervalSec: float = DEFAULT_EMBEDDED_OLLAMA_STARTUP_POLL_INTERVAL_SEC,
        preferredLlmModels: Sequence[str] = DEFAULT_PREFERRED_LLM_MODELS,
        modelDiscovery: Optional[OllamaModelDiscovery] = None,
        autoStartEmbeddedOllama: bool = True,
    ) -> None:
        super().__init__()
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

        self._httpPost: Callable[..., Any] = httpPost or requests.post
        self._requestTimeoutSec = requestTimeoutSec
        self._maxToolCallsPerTurn = max(1, maxToolCallsPerTurn)
        self._maxPromptChars = maxPromptChars
        self._embeddedServingUri = embeddedServingUri.strip() or DEFAULT_EMBEDDED_SERVING_URI
        self._embeddedStartupTimeoutSec = embeddedStartupTimeoutSec
        self._embeddedStartupPollIntervalSec = embeddedStartupPollIntervalSec
        self._preferredLlmModels = tuple(model.strip() for model in preferredLlmModels if model.strip())

        self._embeddedOllamaProcess: Optional[subprocess.Popen[Any]] = None
        self._embeddedStartupError: Optional[str] = None
        self._embeddedOllamaStartLock = asyncio.Lock()
        self._discoveryFailureState = OperatorModelDiscoveryFailureState()
        self._discoveredLlmModel = ""

        if autoStartEmbeddedOllama:
            self._startEmbeddedOllamaEager()
        self._discoverLlmModel(modelDiscovery=modelDiscovery)

    @on_ingress(
        "initial_request",
    )
    async def async_initialRequest(
        self,
        *,
        input_port: str,
        message: operators_pb2.AgentRequest,
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

        requestTraceId = self._currentTraceId()
        requestId = requestTraceId or str(uuid.uuid4())
        responseId = message.response_id or requestId
        servingUri = self._resolveServingUri(message.serving_uri)
        model = self._discoveredLlmModel
        if not model:
            return Result(ok=False, message="no llm model available")

        try:
            await self._async_ensureServingReady(servingUri=servingUri)
            endpoint, messages, toolCalls = await self._async_queryServingWithTools(
                servingUri=servingUri,
                model=model,
                prompt=message.prompt,
            )
        except Exception as exc:
            LOG.exception("Initial request failed while preparing retrieval command")
            return Result(ok=False, message=str(exc))

        retrievalCommand = operators_pb2.RetrievalCommandRequest(
            request_id=requestId,
            conversation_id=message.conversation_id,
            rag_uri=message.rag_uri,
            serving_uri=servingUri,
            prompt=message.prompt,
            model=model,
            response_id=responseId,
            endpoint=endpoint,
        )
        for packedMessage in messages:
            retrievalCommand.messages.append(self._structFromDict(payload=packedMessage))
        for toolCall in toolCalls:
            retrievalCommand.tool_calls.append(self._structFromDict(payload=toolCall))

        await self.async_emit(
            output_port="retrieval_command",
            message=retrievalCommand,
        )
        return Result(ok=True, message=f"retrieval command emitted: {requestId}")

    @on_ingress(
        "enriched_request",
    )
    async def async_enrichedRequest(
        self,
        *,
        input_port: str,
        message: operators_pb2.EnrichedRequest,
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

        servingUri = self._resolveServingUri(message.serving_uri)
        model = self._discoveredLlmModel
        if not model:
            return Result(ok=False, message="no llm model available")

        endpoint = message.endpoint.strip() or self._buildServingChatUri(servingUri)
        messages = [self._dictFromStruct(payload=packedMessage) for packedMessage in message.messages]
        requestPayload = {
            "model": model,
            "messages": messages,
            "stream": False,
        }
        try:
            await self._async_ensureServingReady(servingUri=servingUri)
            response = await asyncio.to_thread(
                self._httpPost,
                endpoint,
                json=requestPayload,
                timeout=self._requestTimeoutSec,
            )
            response.raise_for_status()
            payload = response.json()
            responseMessage = self._extractChatMessage(payload=payload, endpoint=endpoint)
            completion = self._extractMessageContent(messagePayload=responseMessage)
            if not completion:
                return Result(ok=False, message=f"serving endpoint returned no response text: {endpoint}")
        except Exception as exc:
            LOG.exception("Enriched request failed while generating final response")
            return Result(ok=False, message=str(exc))

        finalResponse = operators_pb2.RespondRequest(
            conversation_id=message.conversation_id,
            response_id=message.response_id,
            prompt=message.prompt,
            response=completion,
        )
        await self.async_emit(
            output_port="final_text",
            message=finalResponse,
        )
        return Result(ok=True, message=completion)

    def _rejectIfDiscoveryFailed(self) -> Optional[Result]:
        if not self._discoveryFailureState.shouldRejectRequests():
            return None
        warningMessage = self._discoveryFailureState.rejectionMessage(operatorName="llm_agent_operator")
        LOG.warning(warningMessage)
        return Result(ok=False, message=warningMessage)

    def _discoverLlmModel(self, *, modelDiscovery: Optional[OllamaModelDiscovery]) -> None:
        discovery: Optional[OllamaModelDiscovery] = None
        try:
            discovery = modelDiscovery or OllamaModelDiscovery(
                servingUri=self._embeddedServingUri,
                timeoutSec=DEFAULT_MODEL_DISCOVERY_TIMEOUT_SEC,
            )
            model, reason = discovery.discoverLlmModel(preferredModels=self._preferredLlmModels)
            if model:
                self._discoveredLlmModel = model
                LOG.info("Discovered LLM model for llm_agent_operator: %s", model)
                return
            failureReason = reason or "no LLM model discovered"
            self._discoveryFailureState.markFailed(modelKind="llm", reason=failureReason)
            self._logAvailableModelsOnDiscoveryFailure(discovery=discovery)
            LOG.warning(
                "LLM model discovery failed in llm_agent_operator; fail-flag enabled: %s",
                failureReason,
            )
        except Exception as exc:
            self._discoveryFailureState.markFailed(modelKind="llm", reason=str(exc))
            self._logAvailableModelsOnDiscoveryFailure(discovery=discovery)
            LOG.warning(
                "LLM model discovery failed in llm_agent_operator with exception; fail-flag enabled: %s",
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
                "Unable to list available models for llm_agent_operator after discovery failure: %s",
                exc,
            )
            return
        LOG.info(
            "Available models for llm_agent_operator discovery failure: %s",
            availableModels,
        )

    async def _async_queryServingWithTools(
        self,
        *,
        servingUri: str,
        model: str,
        prompt: str,
    ) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
        endpoint = self._buildServingChatUri(servingUri)
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are a retrieval assistant. You must generate at most one retrieve_context tool call "
                    "before final answer generation."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        firstRequestPayload = {
            "model": model,
            "messages": messages,
            "tools": self._buildToolDefinitions(),
            "stream": False,
        }
        response = await asyncio.to_thread(
            self._httpPost,
            endpoint,
            json=firstRequestPayload,
            timeout=self._requestTimeoutSec,
        )
        response.raise_for_status()
        payload = response.json()
        firstMessage = self._extractChatMessage(payload=payload, endpoint=endpoint)
        toolCalls = self._extractToolCalls(messagePayload=firstMessage)

        if toolCalls:
            messages.append(firstMessage)
        else:
            forcedToolCall = self._buildForcedRetrieveToolCall(prompt=prompt)
            toolCalls = [forcedToolCall]
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": toolCalls,
                }
            )

        self._validateToolCallBudget(toolCalls=toolCalls)
        return endpoint, messages, toolCalls

    def _validateToolCallBudget(self, *, toolCalls: list[dict[str, Any]]) -> None:
        if len(toolCalls) <= self._maxToolCallsPerTurn:
            return
        raise RuntimeError(
            f"tool call limit exceeded: received {len(toolCalls)}, max {self._maxToolCallsPerTurn}"
        )

    def _buildToolDefinitions(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "retrieve_context",
                    "description": "Retrieve relevant context from ChromaDB for a query.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "top_k": {"type": "integer"},
                        },
                        "required": ["query"],
                    },
                },
            }
        ]

    def _buildForcedRetrieveToolCall(self, *, prompt: str) -> dict[str, Any]:
        return {
            "function": {
                "name": "retrieve_context",
                "arguments": {
                    "query": prompt,
                    "top_k": 15,
                },
            }
        }

    def _resolveServingUri(self, candidate: str) -> str:
        normalized = candidate.strip()
        if normalized:
            return normalized
        return self._embeddedServingUri

    def _buildServingChatUri(self, servingUri: str) -> str:
        parsed = urlparse(servingUri)
        if parsed.scheme not in {"http", "https"}:
            raise RuntimeError(f"servingUri scheme must be http or https: {servingUri}")
        if parsed.hostname is None or parsed.port is None:
            raise RuntimeError(f"servingUri must include host and port: {servingUri}")
        trimmed = servingUri.rstrip("/")
        if trimmed.endswith("/api/chat"):
            return trimmed
        return f"{trimmed}/api/chat"

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

    def _currentTraceId(self) -> str:
        messageContext = self.currentMessageContext()
        if messageContext is None or not messageContext.trace_id:
            return ""
        return messageContext.trace_id

    def _extractChatMessage(self, *, payload: dict[str, Any], endpoint: str) -> dict[str, Any]:
        messagePayload = payload.get("message")
        if not isinstance(messagePayload, dict):
            raise RuntimeError(f"serving endpoint returned unexpected payload shape: {endpoint}")
        return messagePayload

    def _extractToolCalls(self, *, messagePayload: dict[str, Any]) -> list[dict[str, Any]]:
        toolCalls = messagePayload.get("tool_calls")
        if toolCalls is None:
            return []
        if not isinstance(toolCalls, list):
            raise RuntimeError("tool_calls must be a list")
        extracted: list[dict[str, Any]] = []
        for item in toolCalls:
            if isinstance(item, dict):
                extracted.append(item)
        return extracted

    def _extractMessageContent(self, *, messagePayload: dict[str, Any]) -> str:
        content = messagePayload.get("content")
        if isinstance(content, str):
            return content.strip()
        return ""

    def _structFromDict(self, *, payload: dict[str, Any]) -> struct_pb2.Struct:
        structured = struct_pb2.Struct()
        structured.update(payload)
        return structured

    def _dictFromStruct(self, *, payload: struct_pb2.Struct) -> dict[str, Any]:
        parsed = json_format.MessageToDict(payload)
        if isinstance(parsed, dict):
            return parsed
        return {}


async def _asyncMain(sidecarUri: str) -> None:
    operator = LlmAgentOperator()
    await operator.async_runUntilStopped(sidecarUri)


def main(argv: Optional[list[str]] = None) -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(
        description="Start an LlmAgentOperator and connect it to an OperatorSidecar."
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
        LOG.info("LlmAgentOperator interrupted, shutting down.")


if __name__ == "__main__":
    main()


__all__ = ["LlmAgentOperator"]
