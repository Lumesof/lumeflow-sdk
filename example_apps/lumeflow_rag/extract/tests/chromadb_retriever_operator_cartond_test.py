from __future__ import annotations

import http.server
import json
import logging
import os
import re
import subprocess
import threading
import unittest
import uuid
from pathlib import Path
from typing import Any, Optional, Sequence

from google.protobuf import json_format
from google.protobuf.any_pb2 import Any as AnyMessage
from python.runfiles import Runfiles

from example_apps.lumeflow_rag.common import operators_pb2
from lumesof.lumeflow import Proto
from lumesof.lumeflow import Test as lumeflow_test

_RETRIEVER_IMAGE = "localhost/lumeflow-chromadb-retriever:latest"


class _JsonServer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
    ) -> None:
        self._host = host
        self._port = port
        self._httpServer: Optional[http.server.ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._httpServer is not None:
            return

        parent = self

        class _RequestHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parent._handleRequest(handler=self, method="GET")

            def do_POST(self) -> None:  # noqa: N802
                parent._handleRequest(handler=self, method="POST")

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
                return

        try:
            self._httpServer = http.server.ThreadingHTTPServer(
                (self._host, self._port),
                _RequestHandler,
            )
        except OSError as exc:
            raise RuntimeError(f"failed to bind fake server {self._host}:{self._port}: {exc}") from exc

        self._thread = threading.Thread(target=self._httpServer.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpServer is None:
            return
        self._httpServer.shutdown()
        self._httpServer.server_close()
        self._httpServer = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def address(self) -> tuple[str, int]:
        if self._httpServer is None:
            return self._host, self._port
        host, port = self._httpServer.server_address
        return str(host), int(port)

    def _readJsonBody(self, *, handler: http.server.BaseHTTPRequestHandler) -> Any:
        contentLengthRaw = handler.headers.get("Content-Length", "0")
        contentLength = int(contentLengthRaw) if contentLengthRaw else 0
        if contentLength <= 0:
            return {}
        rawBody = handler.rfile.read(contentLength)
        if not rawBody:
            return {}
        return json.loads(rawBody.decode("utf-8"))

    def _writeJson(
        self,
        *,
        handler: http.server.BaseHTTPRequestHandler,
        status: int,
        payload: Any,
    ) -> None:
        responseBody = json.dumps(payload).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(responseBody)))
        handler.end_headers()
        handler.wfile.write(responseBody)

    def _handleRequest(
        self,
        *,
        handler: http.server.BaseHTTPRequestHandler,
        method: str,
    ) -> None:
        raise NotImplementedError


class _FakeChromaReadServer(_JsonServer):
    def __init__(self) -> None:
        super().__init__(host="0.0.0.0", port=0)
        self._tenant = "default_tenant"
        self._database = "default_database"
        self._databaseId = str(uuid.uuid4())
        self._collectionId = str(uuid.uuid4())
        self._collectionName = ""
        self._queryCount = 0
        self._unknownRequests: list[tuple[str, str]] = []

    def collectionUri(
        self,
        *,
        collectionName: str,
        hostForClients: Optional[str] = None,
    ) -> str:
        self._collectionName = collectionName
        host, port = self.address()
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        resolvedHost = (hostForClients or host).strip() or host
        return f"http://{resolvedHost}:{port}/{collectionName}"

    def queryCount(self) -> int:
        return self._queryCount

    def unknownRequests(self) -> list[tuple[str, str]]:
        return list(self._unknownRequests)

    def _handleRequest(
        self,
        *,
        handler: http.server.BaseHTTPRequestHandler,
        method: str,
    ) -> None:
        path = handler.path

        if method == "GET" and path == "/api/v2/auth/identity":
            self._writeJson(
                handler=handler,
                status=200,
                payload={
                    "user_id": "itest-user",
                    "tenant": self._tenant,
                    "databases": [self._database],
                },
            )
            return

        if method == "GET" and path == f"/api/v2/tenants/{self._tenant}":
            self._writeJson(handler=handler, status=200, payload={"name": self._tenant})
            return

        if method == "GET" and path == f"/api/v2/tenants/{self._tenant}/databases/{self._database}":
            self._writeJson(
                handler=handler,
                status=200,
                payload={
                    "id": self._databaseId,
                    "name": self._database,
                    "tenant": self._tenant,
                },
            )
            return

        if method == "GET" and path == "/api/v2/pre-flight-checks":
            self._writeJson(
                handler=handler,
                status=200,
                payload={
                    "max_batch_size": 4096,
                    "supports_base64_encoding": False,
                },
            )
            return

        if method == "POST" and path == f"/api/v2/tenants/{self._tenant}/databases/{self._database}/collections":
            body = self._readJsonBody(handler=handler)
            if isinstance(body, dict) and isinstance(body.get("name"), str):
                self._collectionName = body["name"].strip()
            self._writeJson(
                handler=handler,
                status=200,
                payload={
                    "id": self._collectionId,
                    "name": self._collectionName,
                    "configuration_json": {},
                    "metadata": body.get("metadata") if isinstance(body, dict) else None,
                    "dimension": None,
                    "tenant": self._tenant,
                    "database": self._database,
                    "version": 0,
                    "log_position": 0,
                },
            )
            return

        queryPathPattern = (
            rf"^/api/v2/tenants/{re.escape(self._tenant)}/databases/{re.escape(self._database)}"
            rf"/collections/{re.escape(self._collectionId)}/query$"
        )
        if method == "POST" and re.match(queryPathPattern, path):
            self._queryCount += 1
            _ = self._readJsonBody(handler=handler)
            self._writeJson(
                handler=handler,
                status=200,
                payload={
                    "ids": [["doc-1", "doc-2"]],
                    "documents": [[
                        "High-temperature superconductivity appears above liquid nitrogen temperatures.",
                        "Cuprate compounds are a common family studied in this area.",
                    ]],
                    "metadatas": [[
                        {"id": "doc-1", "source": "itest"},
                        {"id": "doc-2", "source": "itest"},
                    ]],
                    "distances": [[0.01, 0.02]],
                    "embeddings": None,
                    "uris": None,
                    "included": ["documents", "metadatas", "distances"],
                },
            )
            return

        self._unknownRequests.append((method, path))
        self._writeJson(
            handler=handler,
            status=404,
            payload={"error": f"unsupported chroma endpoint method={method} path={path}"},
        )


class _RetrievalIngressGenerator(lumeflow_test.IngressGenerator):
    def __init__(
        self,
        *,
        ragUri: str,
        messageId: str,
        expectedAckStatus: str,
    ) -> None:
        self._ragUri = ragUri
        self._messageId = messageId
        self._expectedAckStatus = expectedAckStatus

    def generate(self, *, writableFilePath: Path) -> Sequence[tuple[str, str]]:
        request = operators_pb2.RetrievalCommandRequest(
            request_id="req-1",
            conversation_id="conv-1",
            rag_uri=self._ragUri,
            serving_uri="http://unused.example:11434",
            prompt="What is high-temperature superconductivity?",
            model="qwen2.5:1.5b",
            response_id="resp-1",
            endpoint="/api/chat",
        )
        packed = AnyMessage()
        packed.Pack(request)
        deliver = Proto.operator.DeliverRequest(
            port="retrieval_command",
            message_id=self._messageId,
        )
        deliver.payload.CopyFrom(packed)
        writableFilePath.write_bytes(
            lumeflow_test.encodeDelimitedMessage(deliver.SerializeToString())
        )
        return [(self._messageId, self._expectedAckStatus)]


class _EnrichedRequestVerifier(lumeflow_test.EgressVerifier):
    def verify(self, *, readableFilePath: Path) -> None:
        payload = readableFilePath.read_bytes() if readableFilePath.exists() else b""
        if len(payload) == 0:
            raise AssertionError("waiting for enriched_request output")

        deliveries = lumeflow_test.decodeDelimitedDeliverRequests(
            payload=payload,
            filePath=str(readableFilePath),
            portName="enriched_request",
        )
        if len(deliveries) == 0:
            raise AssertionError("waiting for enriched_request delivery")
        if len(deliveries) != 1:
            raise RuntimeError(f"expected exactly one enriched_request delivery, got={len(deliveries)}")

        enriched = operators_pb2.EnrichedRequest()
        if not deliveries[0].payload.Unpack(enriched):
            raise RuntimeError(
                "Failed to unpack EnrichedRequest "
                f"message_id={deliveries[0].message_id or '<none>'} "
                f"type_url={deliveries[0].payload.type_url}"
            )

        if enriched.request_id != "req-1":
            raise AssertionError(f"unexpected request_id={enriched.request_id}")
        if enriched.conversation_id != "conv-1":
            raise AssertionError(f"unexpected conversation_id={enriched.conversation_id}")
        if enriched.response_id != "resp-1":
            raise AssertionError(f"unexpected response_id={enriched.response_id}")
        if len(enriched.messages) < 2:
            raise AssertionError(
                f"expected at least two enriched messages, got={len(enriched.messages)}"
            )

        parsedMessages = [json_format.MessageToDict(message) for message in enriched.messages]
        toolResponses = [
            message
            for message in parsedMessages
            if message.get("role") == "tool" and message.get("name") == "retrieve_context"
        ]
        if len(toolResponses) != 1:
            raise AssertionError(f"expected one retrieve_context tool response, got={len(toolResponses)}")

        content = str(toolResponses[0].get("content") or "")
        if "High-temperature superconductivity appears above liquid nitrogen temperatures." not in content:
            raise AssertionError(f"missing deterministic context in tool content: {content}")
        if "Cuprate compounds are a common family studied in this area." not in content:
            raise AssertionError(f"missing deterministic context in tool content: {content}")


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


class ChromaDbRetrieverOperatorCartondTests(unittest.IsolatedAsyncioTestCase):
    async def test_chromadbRetrieverEmitsDeterministicContextFromFakeChroma(self) -> None:
        logging.basicConfig(level=logging.INFO)

        runfiles = Runfiles.Create()
        if runfiles is None:
            raise RuntimeError("Failed to initialize Bazel runfiles")

        loadScript = _resolveRunfilePath(
            runfiles=runfiles,
            candidateRunfilesPaths=(
                "_main/lumecode/apps/lumeflow_rag/extract/chromadb_retriever_operator_test_load.sh",
                "_main/lumecode/apps/lumeflow_rag/extract/chromadb_retriever_operator_test_load/load.sh",
                "lumecode/apps/lumeflow_rag/extract/chromadb_retriever_operator_test_load.sh",
                "lumecode/apps/lumeflow_rag/extract/chromadb_retriever_operator_test_load/load.sh",
            ),
        )
        _runCommand(argv=[loadScript], timeoutSeconds=600.0, check=True)

        chromaFake = _FakeChromaReadServer()
        chromaFake.start()
        try:
            retrievalTypeUrl = (
                f"type.googleapis.com/{operators_pb2.RetrievalCommandRequest.DESCRIPTOR.full_name}"
            )
            enrichedTypeUrl = f"type.googleapis.com/{operators_pb2.EnrichedRequest.DESCRIPTOR.full_name}"

            driver = lumeflow_test.StandaloneOperatorTestDriver(
                operatorDockerImage=_RETRIEVER_IMAGE,
                operatorName="chromadb-retriever",
                opnetNodeId="chromadb-retriever-itest-node",
                timeoutSeconds=180.0,
                statusPollSeconds=0.5,
            )
            driver.addIngressGenerator(
                port=_buildPort(
                    portName="retrieval_command",
                    portType=Proto.opnet_types.OpNetPort.PortType.INGRESS,
                    typeUrl=retrievalTypeUrl,
                ),
                generator=_RetrievalIngressGenerator(
                    ragUri=chromaFake.collectionUri(
                        collectionName="itest-collection",
                        hostForClients="host.docker.internal",
                    ),
                    messageId="msg-retrieve-1",
                    expectedAckStatus="ack",
                ),
            )
            driver.addEgressVerifier(
                port=_buildPort(
                    portName="enriched_request",
                    portType=Proto.opnet_types.OpNetPort.PortType.EGRESS,
                    typeUrl=enrichedTypeUrl,
                ),
                verifier=_EnrichedRequestVerifier(),
            )

            await driver.async_run()

            self.assertEqual(chromaFake.queryCount(), 1)
            self.assertEqual(chromaFake.unknownRequests(), [])
        finally:
            chromaFake.stop()


if __name__ == "__main__":
    unittest.main()
