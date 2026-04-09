from __future__ import annotations

import logging
import os
import subprocess
import unittest
from pathlib import Path
from typing import Any, Sequence

from google.protobuf import json_format, struct_pb2
from google.protobuf.any_pb2 import Any as AnyMessage
from python.runfiles import Runfiles

from example_apps.lumeflow_rag.common import operators_pb2
from lumesof.lumeflow import Proto
from lumesof.lumeflow import Test as lumeflow_test

_LLM_AGENT_IMAGE = "localhost/lumeflow-llm-agent:latest"
_RAG_URI = "http://127.0.0.1:39999/itest-rag"
_INITIAL_MESSAGE_ID = "msg-initial-1"
_ENRICHED_MESSAGE_ID = "msg-enriched-1"
_INITIAL_TRACE_ID = "trace-initial_request-input"
_ENRICHED_TRACE_ID = "trace-enriched_request-input"
_INITIAL_CONVERSATION_ID = "conv-initial"
_ENRICHED_CONVERSATION_ID = "conv-enriched"
_INITIAL_RESPONSE_ID = "resp-initial"
_ENRICHED_RESPONSE_ID = "resp-enriched"


def _buildStruct(*, payload: dict[str, Any]) -> struct_pb2.Struct:
    result = struct_pb2.Struct()
    result.update(payload)
    return result


class _InitialRequestIngressGenerator(lumeflow_test.IngressGenerator):
    def __init__(self, *, ragUri: str) -> None:
        self._ragUri = ragUri

    def generate(self, *, writableFilePath: Path) -> Sequence[tuple[str, str]]:
        request = operators_pb2.AgentRequest(
            rag_uri=self._ragUri,
            serving_uri="",
            prompt="How many words are there in this sentence?",
            conversation_id=_INITIAL_CONVERSATION_ID,
            response_id=_INITIAL_RESPONSE_ID,
        )
        packed = AnyMessage()
        packed.Pack(request)

        deliver = Proto.operator.DeliverRequest(
            port="initial_request",
            message_id=_INITIAL_MESSAGE_ID,
        )
        deliver.message_context.trace_id = _INITIAL_TRACE_ID
        deliver.payload.CopyFrom(packed)

        writableFilePath.write_bytes(
            lumeflow_test.encodeDelimitedMessage(deliver.SerializeToString())
        )
        return [(_INITIAL_MESSAGE_ID, "ack")]


class _EnrichedRequestIngressGenerator(lumeflow_test.IngressGenerator):
    def __init__(self, *, ragUri: str) -> None:
        self._ragUri = ragUri

    def generate(self, *, writableFilePath: Path) -> Sequence[tuple[str, str]]:
        request = operators_pb2.EnrichedRequest(
            request_id="req-enriched-1",
            conversation_id=_ENRICHED_CONVERSATION_ID,
            rag_uri=self._ragUri,
            serving_uri="",
            prompt="How many words are there in this sentence?",
            model="",
            response_id=_ENRICHED_RESPONSE_ID,
            endpoint="",
        )
        request.messages.append(
            _buildStruct(
                payload={
                    "role": "system",
                    "content": "Answer briefly and clearly.",
                }
            )
        )
        request.messages.append(
            _buildStruct(
                payload={
                    "role": "user",
                    "content": "How many words are there in this sentence?",
                }
            )
        )

        packed = AnyMessage()
        packed.Pack(request)

        deliver = Proto.operator.DeliverRequest(
            port="enriched_request",
            message_id=_ENRICHED_MESSAGE_ID,
        )
        deliver.message_context.trace_id = _ENRICHED_TRACE_ID
        deliver.payload.CopyFrom(packed)

        writableFilePath.write_bytes(
            lumeflow_test.encodeDelimitedMessage(deliver.SerializeToString())
        )
        return [(_ENRICHED_MESSAGE_ID, "ack")]


class _RetrievalCommandVerifier(lumeflow_test.EgressVerifier):
    def __init__(self, *, expectedRagUri: str) -> None:
        self._expectedRagUri = expectedRagUri

    def verify(self, *, readableFilePath: Path) -> None:
        payload = readableFilePath.read_bytes() if readableFilePath.exists() else b""
        if len(payload) == 0:
            raise AssertionError("waiting for retrieval_command output")

        deliveries = lumeflow_test.decodeDelimitedDeliverRequests(
            payload=payload,
            filePath=str(readableFilePath),
            portName="retrieval_command",
        )
        if len(deliveries) == 0:
            raise AssertionError("waiting for retrieval_command delivery")
        if len(deliveries) != 1:
            raise RuntimeError(
                f"expected exactly one retrieval_command delivery, got={len(deliveries)}"
            )

        delivery = deliveries[0]
        traceId = (
            delivery.message_context.trace_id
            if delivery.HasField("message_context") and delivery.message_context.trace_id
            else ""
        )
        if traceId != _INITIAL_TRACE_ID:
            raise AssertionError(
                f"retrieval_command trace_id mismatch expected={_INITIAL_TRACE_ID} actual={traceId}"
            )

        request = operators_pb2.RetrievalCommandRequest()
        if not delivery.payload.Unpack(request):
            raise RuntimeError(
                f"failed to unpack RetrievalCommandRequest type_url={delivery.payload.type_url}"
            )

        if request.request_id != _INITIAL_TRACE_ID:
            raise AssertionError(
                f"request_id mismatch expected={_INITIAL_TRACE_ID} actual={request.request_id}"
            )
        if request.conversation_id != _INITIAL_CONVERSATION_ID:
            raise AssertionError(
                f"conversation_id mismatch expected={_INITIAL_CONVERSATION_ID} actual={request.conversation_id}"
            )
        if request.response_id != _INITIAL_RESPONSE_ID:
            raise AssertionError(
                f"response_id mismatch expected={_INITIAL_RESPONSE_ID} actual={request.response_id}"
            )
        if request.rag_uri != self._expectedRagUri:
            raise AssertionError(
                f"rag_uri mismatch expected={self._expectedRagUri} actual={request.rag_uri}"
            )
        if request.serving_uri != "http://127.0.0.1:40001":
            raise AssertionError(
                f"serving_uri mismatch expected=http://127.0.0.1:40001 actual={request.serving_uri}"
            )
        if not request.endpoint.endswith("/api/chat"):
            raise AssertionError(f"unexpected endpoint={request.endpoint}")
        if not request.model:
            raise AssertionError("retrieval command model should not be empty")

        toolCallFound = False
        for toolCallStruct in request.tool_calls:
            toolCall = json_format.MessageToDict(toolCallStruct)
            function = toolCall.get("function")
            if isinstance(function, dict) and function.get("name") == "retrieve_context":
                toolCallFound = True
                break
        if not toolCallFound:
            raise AssertionError("expected at least one retrieve_context tool call")


class _FinalTextVerifier(lumeflow_test.EgressVerifier):
    def verify(self, *, readableFilePath: Path) -> None:
        payload = readableFilePath.read_bytes() if readableFilePath.exists() else b""
        if len(payload) == 0:
            raise AssertionError("waiting for final_text output")

        deliveries = lumeflow_test.decodeDelimitedDeliverRequests(
            payload=payload,
            filePath=str(readableFilePath),
            portName="final_text",
        )
        if len(deliveries) == 0:
            raise AssertionError("waiting for final_text delivery")
        if len(deliveries) != 1:
            raise RuntimeError(f"expected exactly one final_text delivery, got={len(deliveries)}")

        delivery = deliveries[0]
        traceId = (
            delivery.message_context.trace_id
            if delivery.HasField("message_context") and delivery.message_context.trace_id
            else ""
        )
        if traceId != _ENRICHED_TRACE_ID:
            raise AssertionError(
                f"final_text trace_id mismatch expected={_ENRICHED_TRACE_ID} actual={traceId}"
            )

        response = operators_pb2.RespondRequest()
        if not delivery.payload.Unpack(response):
            raise RuntimeError(f"failed to unpack RespondRequest type_url={delivery.payload.type_url}")

        if response.conversation_id != _ENRICHED_CONVERSATION_ID:
            raise AssertionError(
                f"conversation_id mismatch expected={_ENRICHED_CONVERSATION_ID} actual={response.conversation_id}"
            )
        if response.response_id != _ENRICHED_RESPONSE_ID:
            raise AssertionError(
                f"response_id mismatch expected={_ENRICHED_RESPONSE_ID} actual={response.response_id}"
            )
        if response.prompt != "How many words are there in this sentence?":
            raise AssertionError(f"prompt mismatch actual={response.prompt}")
        if len(response.response.strip()) == 0:
            raise AssertionError("final_text response should not be empty")


def _buildPort(
    *,
    portName: str,
    portType: int,
    typeUrl: str,
) -> Proto.opnet_types.OpNetPort:
    return Proto.opnet_types.OpNetPort(
        port_name=portName,
        port_type=portType,
        payload_type=Proto.opnet_types.OpNetPayloadType(
            type_url=typeUrl,
            serialization_format=Proto.opnet_types.OpNetPayloadType.SerializationFormat.PROTO,
        ),
    )


def _resolveRunfilePath(*, runfiles: Runfiles, candidateRunfilesPaths: Sequence[str]) -> str:
    candidates: list[str] = []
    for runfilePath in candidateRunfilesPaths:
        resolved = runfiles.Rlocation(runfilePath) or ""
        if resolved:
            candidates.append(resolved)
        candidates.append(runfilePath)
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    raise RuntimeError(f"runfile path not found: checked={candidates}")


def _runCommand(
    *,
    argv: Sequence[str],
    timeoutSeconds: float = 120.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeoutSeconds,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed exit={result.returncode} argv={list(argv)} "
            f"stdout={result.stdout.strip()} stderr={result.stderr.strip()}"
        )
    return result


class LlmAgentOperatorCartondTests(unittest.IsolatedAsyncioTestCase):
    async def test_llmAgentRoutesByInputPortTraceId(self) -> None:
        logging.basicConfig(level=logging.INFO)

        runfiles = Runfiles.Create()
        if runfiles is None:
            raise RuntimeError("Failed to initialize Bazel runfiles")

        loadScript = _resolveRunfilePath(
            runfiles=runfiles,
            candidateRunfilesPaths=(
                "_main/lumecode/apps/lumeflow_rag/extract/llm_agent_operator_test_load.sh",
                "_main/lumecode/apps/lumeflow_rag/extract/llm_agent_operator_test_load/load.sh",
                "lumecode/apps/lumeflow_rag/extract/llm_agent_operator_test_load.sh",
                "lumecode/apps/lumeflow_rag/extract/llm_agent_operator_test_load/load.sh",
            ),
        )
        _runCommand(argv=[loadScript], timeoutSeconds=600.0, check=True)

        initialRequestTypeUrl = f"type.googleapis.com/{operators_pb2.AgentRequest.DESCRIPTOR.full_name}"
        enrichedRequestTypeUrl = (
            f"type.googleapis.com/{operators_pb2.EnrichedRequest.DESCRIPTOR.full_name}"
        )
        retrievalCommandTypeUrl = (
            f"type.googleapis.com/{operators_pb2.RetrievalCommandRequest.DESCRIPTOR.full_name}"
        )
        finalTextTypeUrl = f"type.googleapis.com/{operators_pb2.RespondRequest.DESCRIPTOR.full_name}"

        driver = lumeflow_test.StandaloneOperatorTestDriver(
            operatorDockerImage=_LLM_AGENT_IMAGE,
            operatorName="llm-agent",
            opnetNodeId="llm-agent-itest-node",
            timeoutSeconds=300.0,
            statusPollSeconds=0.5,
        )
        driver.addIngressGenerator(
            port=_buildPort(
                portName="initial_request",
                portType=Proto.opnet_types.OpNetPort.PortType.INGRESS,
                typeUrl=initialRequestTypeUrl,
            ),
            generator=_InitialRequestIngressGenerator(ragUri=_RAG_URI),
        )
        driver.addIngressGenerator(
            port=_buildPort(
                portName="enriched_request",
                portType=Proto.opnet_types.OpNetPort.PortType.INGRESS,
                typeUrl=enrichedRequestTypeUrl,
            ),
            generator=_EnrichedRequestIngressGenerator(ragUri=_RAG_URI),
        )
        driver.addEgressVerifier(
            port=_buildPort(
                portName="retrieval_command",
                portType=Proto.opnet_types.OpNetPort.PortType.EGRESS,
                typeUrl=retrievalCommandTypeUrl,
            ),
            verifier=_RetrievalCommandVerifier(expectedRagUri=_RAG_URI),
        )
        driver.addEgressVerifier(
            port=_buildPort(
                portName="final_text",
                portType=Proto.opnet_types.OpNetPort.PortType.EGRESS,
                typeUrl=finalTextTypeUrl,
            ),
            verifier=_FinalTextVerifier(),
        )

        await driver.async_run()


if __name__ == "__main__":
    unittest.main()
