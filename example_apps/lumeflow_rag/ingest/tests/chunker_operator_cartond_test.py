from __future__ import annotations

import logging
import os
import subprocess
import sys
import types
import unittest
from pathlib import Path
from typing import Sequence

from google.protobuf.any_pb2 import Any as AnyMessage
from python.runfiles import Runfiles

from example_apps.lumeflow_rag.common import operators_pb2

if "requests" not in sys.modules:
    _requestsStub = types.ModuleType("requests")
    _requestsStub.Session = object  # type: ignore[attr-defined]
    sys.modules["requests"] = _requestsStub

from lumesof.lumeflow import Proto
from lumesof.lumeflow import Test as lumeflow_test

_CHUNKER_IMAGE = "localhost/lumeflow-chunker:latest"
_RAG_URI = "http://example.invalid/rag-itest"


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


class _ChunkIngressGenerator(lumeflow_test.IngressGenerator):
    def generate(self, *, writableFilePath: Path) -> Sequence[tuple[str, str]]:
        requestMessage = operators_pb2.ChunkRequest(
            documents=[
                operators_pb2.DocumentProto(
                    text="A" * 2100,
                    metadata={
                        "id": "doc-1",
                        "category": "NarrativeText",
                    },
                )
            ],
            batch_size=2,
            embedding_model=operators_pb2.EMBEDDING_MODEL_ALL_MINILM_L6_V2,
            rag_uri=_RAG_URI,
        )
        packed = AnyMessage()
        packed.Pack(requestMessage)
        deliver = Proto.operator.DeliverRequest(
            port="chunk",
            message_id="msg-chunk-1",
        )
        deliver.payload.CopyFrom(packed)
        writableFilePath.write_bytes(
            lumeflow_test.encodeDelimitedMessage(deliver.SerializeToString())
        )
        return [("msg-chunk-1", "ack")]


class _StoreEgressVerifier(lumeflow_test.EgressVerifier):
    def verify(self, *, readableFilePath: Path) -> None:
        payload = readableFilePath.read_bytes() if readableFilePath.exists() else b""
        if len(payload) == 0:
            raise AssertionError("waiting for store output")
        deliveries = lumeflow_test.decodeDelimitedDeliverRequests(
            payload=payload,
            filePath=str(readableFilePath),
            portName="store",
        )
        if len(deliveries) < 2:
            raise AssertionError(f"waiting for two store deliveries, got={len(deliveries)}")
        if len(deliveries) != 2:
            raise RuntimeError(f"expected exactly two store deliveries, got={len(deliveries)}")

        storeRequests: list[operators_pb2.StoreRequest] = []
        for delivery in deliveries:
            storeRequest = operators_pb2.StoreRequest()
            if not delivery.payload.Unpack(storeRequest):
                raise RuntimeError(
                    f"Failed to unpack StoreRequest for port={delivery.port} "
                    f"message_id={delivery.message_id or '<none>'} "
                    f"type_url={delivery.payload.type_url}"
                )
            storeRequests.append(storeRequest)

        for request in storeRequests:
            if request.embedding_model != operators_pb2.EMBEDDING_MODEL_ALL_MINILM_L6_V2:
                raise RuntimeError(
                    f"unexpected embedding model: {request.embedding_model}"
                )
            if request.rag_uri != _RAG_URI:
                raise RuntimeError(
                    f"unexpected rag_uri: {request.rag_uri}"
                )

        batchSizes = [len(request.documents) for request in storeRequests]
        if batchSizes != [2, 1]:
            raise AssertionError(f"expected store batch sizes [2, 1], got={batchSizes}")

        chunkDocs = [doc for request in storeRequests for doc in request.documents]
        expectedChunkIds = ["doc-1-chunk-1", "doc-1-chunk-2", "doc-1-chunk-3"]
        actualChunkIds = [doc.metadata.get("id", "") for doc in chunkDocs]
        if actualChunkIds != expectedChunkIds:
            raise AssertionError(f"expected chunk ids={expectedChunkIds}, got={actualChunkIds}")

        actualRoles = [doc.metadata.get("structure_role", "") for doc in chunkDocs]
        if actualRoles != ["paragraph", "paragraph", "paragraph"]:
            raise AssertionError(f"expected paragraph roles, got={actualRoles}")

        actualLengths = [len(doc.text) for doc in chunkDocs]
        if actualLengths != [1000, 1000, 300]:
            raise AssertionError(f"expected chunk lengths [1000, 1000, 300], got={actualLengths}")


class ChunkerOperatorCartondTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunkerEmitsStoreBatchesFromChunkRequest(self) -> None:
        logging.basicConfig(level=logging.INFO)

        runfiles = Runfiles.Create()
        if runfiles is None:
            raise RuntimeError("Failed to initialize Bazel runfiles")

        loadScript = _resolveRunfilePath(
            runfiles=runfiles,
            candidateRunfilesPaths=(
                "_main/lumecode/apps/lumeflow_rag/ingest/chunker_operator_test_load.sh",
                "_main/lumecode/apps/lumeflow_rag/ingest/chunker_operator_test_load/load.sh",
                "lumecode/apps/lumeflow_rag/ingest/chunker_operator_test_load.sh",
                "lumecode/apps/lumeflow_rag/ingest/chunker_operator_test_load/load.sh",
            ),
        )
        _runCommand(argv=[loadScript], timeoutSeconds=600.0, check=True)

        chunkTypeUrl = f"type.googleapis.com/{operators_pb2.ChunkRequest.DESCRIPTOR.full_name}"
        storeTypeUrl = f"type.googleapis.com/{operators_pb2.StoreRequest.DESCRIPTOR.full_name}"

        driver = lumeflow_test.StandaloneOperatorTestDriver(
            operatorDockerImage=_CHUNKER_IMAGE,
            operatorName="chunker",
            opnetNodeId="chunker-itest-node",
            timeoutSeconds=180.0,
            statusPollSeconds=0.5,
        )
        driver.addIngressGenerator(
            port=_buildPort(
                portName="chunk",
                portType=Proto.opnet_types.OpNetPort.PortType.INGRESS,
                typeUrl=chunkTypeUrl,
            ),
            generator=_ChunkIngressGenerator(),
        )
        driver.addEgressVerifier(
            port=_buildPort(
                portName="store",
                portType=Proto.opnet_types.OpNetPort.PortType.EGRESS,
                typeUrl=storeTypeUrl,
            ),
            verifier=_StoreEgressVerifier(),
        )

        await driver.async_run()


if __name__ == "__main__":
    unittest.main()
