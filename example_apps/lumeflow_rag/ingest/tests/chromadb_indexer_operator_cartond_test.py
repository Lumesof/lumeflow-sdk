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

from google.protobuf.any_pb2 import Any as AnyMessage
from python.runfiles import Runfiles

from example_apps.lumeflow_rag.common import operators_pb2
from lumesof.lumeflow import Proto
from lumesof.lumeflow import Test as lumeflow_test

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

    def baseUrl(self) -> str:
        host, port = self.address()
        return f"http://{host}:{port}"

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


class _FakeChromaServer(_JsonServer):
    def __init__(self, *, failAdd: bool = False) -> None:
        # Bind on all interfaces so containers can reach this fake through
        # host.docker.internal -> host-gateway.
        super().__init__(host="0.0.0.0", port=0)
        self._tenant = "default_tenant"
        self._database = "default_database"
        self._databaseId = str(uuid.uuid4())
        self._collectionId = str(uuid.uuid4())
        self._collectionName = ""
        self._addPayloads: list[dict[str, Any]] = []
        self._failAdd = failAdd
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

    def addPayloads(self) -> list[dict[str, Any]]:
        return list(self._addPayloads)

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

        addPathPattern = (
            rf"^/api/v2/tenants/{re.escape(self._tenant)}/databases/{re.escape(self._database)}"
            rf"/collections/{re.escape(self._collectionId)}/add$"
        )
        if method == "POST" and re.match(addPathPattern, path):
            body = self._readJsonBody(handler=handler)
            if isinstance(body, dict):
                self._addPayloads.append(body)
            if self._failAdd:
                self._writeJson(
                    handler=handler,
                    status=500,
                    payload={"error": "InternalError", "message": "intentional add failure"},
                )
            else:
                self._writeJson(handler=handler, status=200, payload={})
            return

        self._unknownRequests.append((method, path))
        self._writeJson(
            handler=handler,
            status=404,
            payload={"error": f"unsupported chroma endpoint method={method} path={path}"},
        )


class _StoreIngressGenerator(lumeflow_test.IngressGenerator):
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
        documents = [
            operators_pb2.DocumentProto(
                text="alpha",
                metadata={"id": "doc-alpha", "source": "itest"},
            ),
            operators_pb2.DocumentProto(
                text="beta",
                metadata={"id": "doc-beta", "source": "itest"},
            ),
        ]
        requestMessage = operators_pb2.StoreRequest(
            documents=documents,
            embedding_model=operators_pb2.EMBEDDING_MODEL_ALL_MINILM_L6_V2,
            rag_uri=self._ragUri,
        )
        packed = AnyMessage()
        packed.Pack(requestMessage)
        deliver = Proto.operator.DeliverRequest(
            port="store",
            message_id=self._messageId,
        )
        deliver.payload.CopyFrom(packed)
        writableFilePath.write_bytes(
            lumeflow_test.encodeDelimitedMessage(deliver.SerializeToString())
        )
        return [(self._messageId, self._expectedAckStatus)]


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


class ChromaDbIndexerOperatorCartondTests(unittest.IsolatedAsyncioTestCase):
    async def test_chromadbIndexerStoresDocumentsInFakeChroma(self) -> None:
        logging.basicConfig(level=logging.INFO)

        runfiles = Runfiles.Create()
        if runfiles is None:
            raise RuntimeError("Failed to initialize Bazel runfiles")

        loadScript = _resolveRunfilePath(
            runfiles=runfiles,
            candidateRunfilesPaths=(
                "_main/lumecode/apps/lumeflow_rag/ingest/chromadb_indexer_operator_test_load.sh",
                "_main/lumecode/apps/lumeflow_rag/ingest/chromadb_indexer_operator_test_load/load.sh",
                "lumecode/apps/lumeflow_rag/ingest/chromadb_indexer_operator_test_load.sh",
                "lumecode/apps/lumeflow_rag/ingest/chromadb_indexer_operator_test_load/load.sh",
            ),
        )
        _runCommand(argv=[loadScript], timeoutSeconds=600.0, check=True)

        chromaFake = _FakeChromaServer()
        chromaFake.start()
        try:
            storeTypeUrl = f"type.googleapis.com/{operators_pb2.StoreRequest.DESCRIPTOR.full_name}"
            collectionName = "itest-collection"
            driver = lumeflow_test.StandaloneOperatorTestDriver(
                operatorDockerImage="localhost/lumeflow-chromadb-indexer:latest",
                operatorName="chromadb-indexer",
                opnetNodeId="chromadb-indexer-itest-node",
                timeoutSeconds=180.0,
                statusPollSeconds=0.5,
            )
            driver.addIngressGenerator(
                port=_buildPort(
                    portName="store",
                    portType=Proto.opnet_types.OpNetPort.PortType.INGRESS,
                    typeUrl=storeTypeUrl,
                ),
                generator=_StoreIngressGenerator(
                    ragUri=chromaFake.collectionUri(
                        collectionName=collectionName,
                        hostForClients="host.docker.internal",
                    ),
                    messageId="msg-store-1",
                    expectedAckStatus="ack",
                ),
            )

            await driver.async_run()

            addPayloads = chromaFake.addPayloads()
            self.assertEqual(len(addPayloads), 1)
            addPayload = addPayloads[0]
            self.assertEqual(addPayload.get("ids"), ["doc-alpha", "doc-beta"])
            self.assertEqual(addPayload.get("documents"), ["alpha", "beta"])

            metadatas = addPayload.get("metadatas")
            self.assertIsInstance(metadatas, list)
            self.assertEqual(len(metadatas), 2)
            self.assertEqual(metadatas[0].get("id"), "doc-alpha")
            self.assertEqual(metadatas[1].get("id"), "doc-beta")

            embeddings = addPayload.get("embeddings")
            self.assertIsInstance(embeddings, list)
            self.assertEqual(len(embeddings), 2)

            self.assertEqual(chromaFake.unknownRequests(), [])
        finally:
            chromaFake.stop()


if __name__ == "__main__":
    unittest.main()
