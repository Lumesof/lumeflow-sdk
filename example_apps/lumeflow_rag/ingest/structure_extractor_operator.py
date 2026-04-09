import argparse
import asyncio
import importlib.util
import logging
import os
import subprocess
import sys
import threading
from typing import Dict, List, Optional
from urllib.parse import urlparse

from example_apps.lumeflow_rag.common import operators_pb2
from lumesof.lumeflow import Operator, Proto, operator_ports
opnet_types_pb2 = Proto.opnet_types
Result = Proto.opnet_types.Result

LOG = logging.getLogger(__name__)
on_ingress = Operator.on_ingress
DEFAULT_HTTP_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
HTTP_USER_AGENT_ENV = "LUMEFLOW_STRUCTURE_EXTRACTOR_USER_AGENT"
SPACY_MODEL_NAME = "en_core_web_sm"
SPACY_MODEL_VERSION = "3.8.0"
DEFAULT_SPACY_MODEL_INSTALL_DIR = "/tmp/lumeflow_spacy_models"
SPACY_MODEL_INSTALL_DIR_ENV = "LUMEFLOW_STRUCTURE_EXTRACTOR_SPACY_MODEL_DIR"
SPACY_MODEL_WHEEL_URL_ENV = "LUMEFLOW_STRUCTURE_EXTRACTOR_SPACY_MODEL_WHEEL_URL"
DEFAULT_SPACY_MODEL_WHEEL_URL = (
    "https://github.com/explosion/spacy-models/releases/download/"
    "en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
)

OPERATOR_PORTS = {
    "ingress": [
        {
            "name": "extract",
            "serialization_format": opnet_types_pb2.OpNetPayloadType.PROTO,
            "type_url": "example_apps.operators.StructureExtractorRequest",
        },
    ],
    "egress": [
        {
            "name": "chunk",
            "serialization_format": opnet_types_pb2.OpNetPayloadType.PROTO,
            "type_url": "example_apps.operators.ChunkRequest",
        },
    ],
}


@operator_ports(OPERATOR_PORTS)
class StructureExtractorOperator(Operator):
    """Extract structured elements from a URI and emit them in batches."""
    _spacyModelInstallLock = threading.Lock()
    _spacyModelReady = False

    @on_ingress(
        "extract",
    )
    async def async_extract(
        self,
        *,
        input_port: str,
        message: operators_pb2.StructureExtractorRequest,
    ) -> Result:
        _ = input_port
        if not message.uri:
            return Result(ok=False, message="uri is required")

        try:
            elements = self.loadFromUri(message.uri)
        except Exception as exc:
            LOG.exception("Failed to load elements from uri=%s", message.uri)
            return Result(ok=False, message=f"failed to retrieve uri={message.uri}: {exc}")

        extracted_documents = [self._elementToProto(elem, source_url=message.uri) for elem in elements]
        extracted_structure = [
            {"text": doc.text, "metadata": dict(doc.metadata)}
            for doc in extracted_documents
        ]
        LOG.info(f"Extracted structure from uri={message.uri}: {extracted_structure}")

        if not extracted_documents:
            return Result(ok=True, message="no elements to emit")

        batch_size = message.batch_size if message.batch_size > 0 else len(extracted_documents)
        total_batches = 0

        for idx in range(0, len(extracted_documents), batch_size):
            batch = extracted_documents[idx : idx + batch_size]

            chunk_msg = operators_pb2.ChunkRequest(
                documents=batch,
                batch_size=message.batch_size,
                embedding_model=message.embedding_model,
                rag_uri=message.rag_uri,
            )

            await self.async_emit(output_port="chunk", message=chunk_msg)
            total_batches += 1

        return Result(ok=True, message=f"emitted {len(extracted_documents)} elements in {total_batches} batches")

    @staticmethod
    def loadFromUri(uri: str) -> List["Element"]:
        """
        Load and partition remote content via the unstructured URI loader.
        """
        try:
            from unstructured.documents.elements import Element, Text
            from unstructured.partition.auto import partition
            from unstructured.partition.html import partition_html
            import requests
        except ImportError as exc:
            raise RuntimeError("unstructured must be installed to load from uri") from exc

        parsed = urlparse(uri)
        if parsed.scheme in ("http", "https"):
            response = requests.get(
                uri,
                timeout=10,
                headers=StructureExtractorOperator._buildHttpHeaders(),
            )
            response.raise_for_status()
            try:
                StructureExtractorOperator._ensureSpacyModelAvailable()
                elements = partition_html(text=response.text)
            except Exception as exc:
                # Unstructured may fail when optional runtime NLP models are unavailable.
                # Fall back to a best-effort HTML text extraction to preserve availability.
                LOG.warning("partition_html failed for uri=%s, falling back to plain text extraction: %s", uri, exc)
                try:
                    from bs4 import BeautifulSoup  # type: ignore

                    soup = BeautifulSoup(response.text, "html.parser")
                    textChunks = [chunk.strip() for chunk in soup.stripped_strings if chunk and chunk.strip()]
                except Exception:
                    textChunks = []
                if len(textChunks) == 0:
                    textChunks = [response.text.strip()]
                elements = [Text(text=chunk) for chunk in textChunks if chunk]
            return elements

        # Fallback: let partition handle local paths or other schemes it supports
        elements: List[Element] = partition(url=uri)
        return elements

    @staticmethod
    def _elementToProto(element: "Element", *, source_url: str) -> operators_pb2.DocumentProto:
        metadata = getattr(element, "metadata", None)
        meta_map: Dict[str, str] = {}

        elem_id = getattr(metadata, "id", None) or getattr(element, "id", None)
        if elem_id:
            meta_map["id"] = str(elem_id)

        elem_type = element.__class__.__name__
        meta_map["type"] = elem_type
        category = getattr(element, "category", elem_type)
        if category:
            meta_map["category"] = category

        filename = getattr(metadata, "filename", None)
        if filename:
            meta_map["filename"] = filename
        url = getattr(metadata, "url", None) or source_url
        if url:
            meta_map["url"] = url
        page_number = getattr(metadata, "page_number", None)
        if page_number is not None:
            meta_map["page_number"] = str(page_number)
        parent_id = getattr(metadata, "parent_id", None)
        if parent_id:
            meta_map["parent_id"] = str(parent_id)

        return operators_pb2.DocumentProto(
            text=str(element),
            metadata=meta_map,
        )

    @staticmethod
    def _buildChunkMetadata(self, elements: List["Element"], *, source_url: str) -> Dict[str, str]:
        # Metadata no longer attached to ChunkRequest; kept for compatibility if needed elsewhere.
        meta: Dict[str, str] = {"url": source_url}
        return meta

    @staticmethod
    def _buildHttpHeaders() -> Dict[str, str]:
        userAgent = os.getenv(HTTP_USER_AGENT_ENV, DEFAULT_HTTP_USER_AGENT).strip()
        if not userAgent:
            userAgent = DEFAULT_HTTP_USER_AGENT
        return {"User-Agent": userAgent}

    @staticmethod
    def _ensureSpacyModelAvailable() -> None:
        if StructureExtractorOperator._isSpacyModelImportable():
            StructureExtractorOperator._spacyModelReady = True
            return
        if StructureExtractorOperator._spacyModelReady:
            return
        with StructureExtractorOperator._spacyModelInstallLock:
            if StructureExtractorOperator._isSpacyModelImportable():
                StructureExtractorOperator._spacyModelReady = True
                return
            installDir = os.getenv(
                SPACY_MODEL_INSTALL_DIR_ENV,
                DEFAULT_SPACY_MODEL_INSTALL_DIR,
            ).strip()
            if not installDir:
                installDir = DEFAULT_SPACY_MODEL_INSTALL_DIR
            os.makedirs(installDir, exist_ok=True)
            if installDir not in sys.path:
                sys.path.insert(0, installDir)
            if StructureExtractorOperator._isSpacyModelImportable():
                StructureExtractorOperator._spacyModelReady = True
                return

            modelWheelUrl = os.getenv(
                SPACY_MODEL_WHEEL_URL_ENV,
                DEFAULT_SPACY_MODEL_WHEEL_URL,
            ).strip()
            if not modelWheelUrl:
                modelWheelUrl = DEFAULT_SPACY_MODEL_WHEEL_URL
            LOG.info(
                "Installing spaCy model %s into writable directory: %s",
                SPACY_MODEL_NAME,
                installDir,
            )
            try:
                subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        "--no-deps",
                        "--target",
                        installDir,
                        modelWheelUrl,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or "").strip()
                stdout = (exc.stdout or "").strip()
                details = stderr or stdout or str(exc)
                raise RuntimeError(
                    f"failed to install {SPACY_MODEL_NAME} into {installDir}: {details}"
                ) from exc

            if installDir not in sys.path:
                sys.path.insert(0, installDir)
            if not StructureExtractorOperator._isSpacyModelImportable():
                raise RuntimeError(
                    f"{SPACY_MODEL_NAME} still unavailable after install to {installDir}"
                )
            StructureExtractorOperator._spacyModelReady = True

    @staticmethod
    def _isSpacyModelImportable() -> bool:
        return importlib.util.find_spec(SPACY_MODEL_NAME) is not None


async def _asyncMain(sidecar_uri: str) -> None:
    operator = StructureExtractorOperator()
    await operator.async_runUntilStopped(sidecar_uri)


def main(argv: Optional[list[str]] = None) -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(
        description="Start a StructureExtractorOperator and connect it to an OperatorSidecar."
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
        LOG.info("StructureExtractorOperator interrupted, shutting down.")


if __name__ == "__main__":
    main()


__all__ = ["StructureExtractorOperator"]
