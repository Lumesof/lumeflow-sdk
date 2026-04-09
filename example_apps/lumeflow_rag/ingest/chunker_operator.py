import argparse
import asyncio
import logging
import uuid
from typing import List, Optional

from example_apps.lumeflow_rag.common import operators_pb2
from lumesof.lumeflow import Operator, Proto, operator_ports
opnet_types_pb2 = Proto.opnet_types
Result = Proto.opnet_types.Result

LOG = logging.getLogger(__name__)
on_ingress = Operator.on_ingress

OPERATOR_PORTS = {
    "ingress": [
        {
            "name": "chunk",
            "serialization_format": opnet_types_pb2.OpNetPayloadType.PROTO,
            "type_url": "example_apps.operators.ChunkRequest",
        },
    ],
    "egress": [
        {
            "name": "store",
            "serialization_format": opnet_types_pb2.OpNetPayloadType.PROTO,
            "type_url": "example_apps.operators.StoreRequest",
        },
    ],
}

@operator_ports(OPERATOR_PORTS)
class ChunkerOperator(Operator):
    """Chunk unstructured elements."""

    def __init__(self, max_chars: int = 1000, overlap: int = 100) -> None:
        super().__init__()
        self.max_chars = max_chars
        self.overlap = overlap

    @on_ingress(
        "chunk",
    )
    async def async_chunk(
        self,
        *,
        input_port: str,
        message: operators_pb2.ChunkRequest,
    ) -> Result:
        _ = input_port
        documents = list(message.documents)
        if not documents:
            return Result(ok=True, message="no documents to chunk")

        message_context = self.currentMessageContext()
        request_trace_id = (
            message_context.trace_id
            if message_context is not None and message_context.trace_id
            else ""
        )
        request_correlation_id = request_trace_id or str(uuid.uuid4())
        inputDocumentSizesCsv = ",".join(str(len(document.text or "")) for document in documents)
        LOG.debug(
            "chunker input batch received documents=%d document_sizes=%s batch_size_hint=%d rag_uri=%s trace_id=%s",
            len(documents),
            inputDocumentSizesCsv,
            message.batch_size,
            message.rag_uri,
            request_trace_id or "<none>",
        )
        chunk_lists = await asyncio.gather(
            *(
                self.async_chunkElement(
                    doc,
                    document_index=document_index,
                    request_correlation_id=request_correlation_id,
                )
                for document_index, doc in enumerate(documents, start=1)
            )
        )
        all_chunks: List[operators_pb2.DocumentProto] = [chunk for chunks in chunk_lists for chunk in chunks]
        batch_size = message.batch_size if message.batch_size > 0 else len(all_chunks)
        outputBatchCount = (len(all_chunks) + batch_size - 1) // batch_size if batch_size > 0 else 0

        for idx in range(0, len(all_chunks), batch_size):
            batch = all_chunks[idx : idx + batch_size]
            outputBatchIndex = (idx // batch_size) + 1
            outputDocumentSizesCsv = ",".join(str(len(document.text or "")) for document in batch)
            store_request = operators_pb2.StoreRequest(
                documents=batch,
                embedding_model=message.embedding_model,
                rag_uri=message.rag_uri,
            )
            await self.async_emit(
                output_port="store",
                message=store_request,
            )
            LOG.debug(
                "chunker output batch emitted output_batch=%d/%d documents=%d document_sizes=%s trace_id=%s",
                outputBatchIndex,
                outputBatchCount,
                len(batch),
                outputDocumentSizesCsv,
                request_trace_id or "<none>",
            )

        return Result(ok=True, message=f"chunked {len(documents)} documents into {len(all_chunks)} chunks")

    async def async_chunkElement(
        self,
        document: operators_pb2.DocumentProto,
        *,
        document_index: int,
        request_correlation_id: str,
    ) -> List[operators_pb2.DocumentProto]:
        """
        Async chunker that mirrors rag.chunker.Chunker.chunk behavior.
        """
        raw_text = document.text or ""
        meta = dict(document.metadata)
        source_id = meta.get("id")
        category = meta.get("category", meta.get("type", "")).lower()
        meta.setdefault("structure_type", meta.get("category", meta.get("type", "text")))

        results: List[operators_pb2.DocumentProto] = []
        fallback_id_prefix = f"{request_correlation_id}-doc-{document_index}"

        def buildChunkId(*, chunk_index: int, total_chunks: int) -> str:
            if source_id:
                if total_chunks == 1:
                    return source_id
                return f"{source_id}-chunk-{chunk_index}"
            return f"{fallback_id_prefix}-chunk-{chunk_index}"

        if category == "title":
            out_meta = dict(meta)
            out_meta["structure_role"] = "title"
            out_meta["id"] = buildChunkId(chunk_index=1, total_chunks=1)
            results.append(operators_pb2.DocumentProto(text=raw_text.strip(), metadata=out_meta))
            return results

        if category == "table":
            out_meta = dict(meta)
            out_meta["structure_role"] = "table"
            out_meta["id"] = buildChunkId(chunk_index=1, total_chunks=1)
            results.append(operators_pb2.DocumentProto(text=raw_text, metadata=out_meta))
            return results

        if category == "listitem":
            out_meta = dict(meta)
            out_meta["structure_role"] = "list-item"
            out_meta["id"] = buildChunkId(chunk_index=1, total_chunks=1)
            normalized = raw_text.strip()
            if normalized and normalized[0] not in {"-", "*", "•"}:
                normalized = f"• {normalized}"
            results.append(operators_pb2.DocumentProto(text=normalized, metadata=out_meta))
            return results

        # Narrative text and any other fallback element types are chunked with overlap.
        role = "paragraph" if category == "narrativetext" else "text"
        stride = max(1, self.max_chars - self.overlap)
        chunk_count = 0
        for _ in range(0, len(raw_text), stride):
            chunk_count += 1

        for chunk_index, i in enumerate(range(0, len(raw_text), stride), start=1):
            chunk_text = raw_text[i : i + self.max_chars]
            out_meta = dict(meta)
            out_meta["structure_role"] = role
            out_meta["id"] = buildChunkId(chunk_index=chunk_index, total_chunks=chunk_count)
            results.append(operators_pb2.DocumentProto(text=chunk_text, metadata=out_meta))

        return results


async def _asyncMain(sidecar_uri: str) -> None:
    operator = ChunkerOperator()
    await operator.async_runUntilStopped(sidecar_uri)


def main(argv: Optional[list[str]] = None) -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(
        description="Start a ChunkerOperator and connect it to an OperatorSidecar."
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
        LOG.info("ChunkerOperator interrupted, shutting down.")


if __name__ == "__main__":
    main()


__all__ = ["ChunkerOperator"]
