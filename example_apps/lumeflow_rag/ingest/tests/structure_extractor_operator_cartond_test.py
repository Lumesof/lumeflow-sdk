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

_STRUCTURE_EXTRACTOR_IMAGE = "localhost/lumeflow-structure-extractor:latest"
_WIKIPEDIA_URI = "https://en.wikipedia.org/wiki/High-temperature_superconductivity"


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


class _StructureExtractorIngressGenerator(lumeflow_test.IngressGenerator):
    def generate(self, *, writableFilePath: Path) -> Sequence[tuple[str, str]]:
        requestMessage = operators_pb2.StructureExtractorRequest(
            uri=_WIKIPEDIA_URI,
            batch_size=16,
            embedding_model=operators_pb2.EMBEDDING_MODEL_ALL_MINILM_L6_V2,
        )
        packed = AnyMessage()
        packed.Pack(requestMessage)
        deliver = Proto.operator.DeliverRequest(
            port="extract",
            message_id="msg-wikipedia",
        )
        deliver.payload.CopyFrom(packed)
        writableFilePath.write_bytes(
            lumeflow_test.encodeDelimitedMessage(deliver.SerializeToString())
        )
        return [("msg-wikipedia", "ack")]


class _ChunkNonEmptyVerifier(lumeflow_test.EgressVerifier):
    def verify(self, *, readableFilePath: Path) -> None:
        payload = readableFilePath.read_bytes() if readableFilePath.exists() else b""
        if len(payload) == 0:
            raise AssertionError("waiting for chunk output")
        deliveries = lumeflow_test.decodeDelimitedDeliverRequests(
            payload=payload,
            filePath=str(readableFilePath),
            portName="chunk",
        )
        if len(deliveries) == 0:
            raise AssertionError("no chunk deliveries yet")

        totalDocuments = 0
        for delivery in deliveries:
            chunkRequest = operators_pb2.ChunkRequest()
            if not delivery.payload.Unpack(chunkRequest):
                raise RuntimeError(
                    f"Failed to unpack ChunkRequest for port={delivery.port} "
                    f"message_id={delivery.message_id or '<none>'} "
                    f"type_url={delivery.payload.type_url}"
                )
            totalDocuments += len(chunkRequest.documents)
        if totalDocuments <= 0:
            raise AssertionError("expected at least one extracted document")


class StructureExtractorOperatorCartondTests(unittest.IsolatedAsyncioTestCase):
    async def test_structureExtractorEmitsDocumentsFromWikipedia(self) -> None:
        logging.basicConfig(level=logging.INFO)

        runfiles = Runfiles.Create()
        if runfiles is None:
            raise RuntimeError("Failed to initialize Bazel runfiles")

        loadScript = _resolveRunfilePath(
            runfiles=runfiles,
            candidateRunfilesPaths=(
                "_main/lumecode/apps/lumeflow_rag/ingest/structured_extractor_operator_test_load.sh",
                "_main/lumecode/apps/lumeflow_rag/ingest/structured_extractor_operator_test_load/load.sh",
                "lumecode/apps/lumeflow_rag/ingest/structured_extractor_operator_test_load.sh",
                "lumecode/apps/lumeflow_rag/ingest/structured_extractor_operator_test_load/load.sh",
            ),
        )
        _runCommand(argv=[loadScript], timeoutSeconds=600.0, check=True)

        extractTypeUrl = (
            f"type.googleapis.com/{operators_pb2.StructureExtractorRequest.DESCRIPTOR.full_name}"
        )
        chunkTypeUrl = f"type.googleapis.com/{operators_pb2.ChunkRequest.DESCRIPTOR.full_name}"

        driver = lumeflow_test.StandaloneOperatorTestDriver(
            operatorDockerImage=_STRUCTURE_EXTRACTOR_IMAGE,
            operatorName="structure-extractor",
            opnetNodeId="structure-extractor-itest-node",
            timeoutSeconds=600.0,
            statusPollSeconds=0.5,
        )
        driver.addIngressGenerator(
            port=_buildPort(
                portName="extract",
                portType=Proto.opnet_types.OpNetPort.PortType.INGRESS,
                typeUrl=extractTypeUrl,
            ),
            generator=_StructureExtractorIngressGenerator(),
        )
        driver.addEgressVerifier(
            port=_buildPort(
                portName="chunk",
                portType=Proto.opnet_types.OpNetPort.PortType.EGRESS,
                typeUrl=chunkTypeUrl,
            ),
            verifier=_ChunkNonEmptyVerifier(),
        )

        await driver.async_run()


if __name__ == "__main__":
    unittest.main()
