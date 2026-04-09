from __future__ import annotations

from dataclasses import dataclass

from example_apps.lumeflow_rag.ingest.image_descriptors.chromadb_indexer_operator_nomic_embed_text_v1_5 import (
    _IMAGE_DESCRIPTOR as _CHROMADB_INDEXER_IMAGE_DESCRIPTOR,
)
from example_apps.lumeflow_rag.ingest.image_descriptors.chunker_operator import (
    _IMAGE_DESCRIPTOR as _CHUNKER_IMAGE_DESCRIPTOR,
)
from example_apps.lumeflow_rag.ingest.image_descriptors.structure_extractor_operator import (
    _IMAGE_DESCRIPTOR as _STRUCTURE_EXTRACTOR_IMAGE_DESCRIPTOR,
)
from example_apps.lumeflow_rag.common.rag_uri import parseRagUri
from example_apps.lumeflow_rag.common import operators_pb2
from lumesof.lumeflow import Graph
from lumesof.lumeflow import OperatorImageDescriptor
from lumesof.lumeflow import Proto
from lumesof.lumeflow import graph_type
from lumesof.lumeflow import materialize


_STRUCTURE_EXTRACTOR_OPERATOR_NAME = "rag-structure-extractor"
_CHUNKER_OPERATOR_NAME = "rag-chunker"
_CHROMADB_INDEXER_OPERATOR_NAME = "rag-chromadb-indexer"
_CANONICAL_ANY_TYPE_PREFIX = "type.googleapis.com/"
_DEFAULT_ASYNC_INGEST_LIGHT_DAG_NAME = "lumeflow-rag-async-ingest-light-v1"


@dataclass(frozen=True)
class AsyncIngestOperatorImages:
    structure_extractor_image_url: str
    chunker_image_url: str
    chromadb_indexer_image_url: str


@graph_type("async")
class AsyncIngestLightGraph(Graph):
    def __init__(
        self,
        *,
        images: AsyncIngestOperatorImages,
        dag_name: str = _DEFAULT_ASYNC_INGEST_LIGHT_DAG_NAME,
    ) -> None:
        self._images = images
        self._dagName = dag_name

    @materialize
    def buildDag(self) -> Proto.dag.Dag:
        self.setDagName(self._dagName)

        self.createOperatorFromImageDescriptor(
            name=_STRUCTURE_EXTRACTOR_OPERATOR_NAME,
            descriptor=_buildOperatorDescriptor(
                imageUrl=self._images.structure_extractor_image_url,
                defaultDescriptor=_STRUCTURE_EXTRACTOR_IMAGE_DESCRIPTOR,
            ),
        )
        self.createOperatorFromImageDescriptor(
            name=_CHUNKER_OPERATOR_NAME,
            descriptor=_buildOperatorDescriptor(
                imageUrl=self._images.chunker_image_url,
                defaultDescriptor=_CHUNKER_IMAGE_DESCRIPTOR,
            ),
        )
        self.createOperatorFromImageDescriptor(
            name=_CHROMADB_INDEXER_OPERATOR_NAME,
            descriptor=_buildOperatorDescriptor(
                imageUrl=self._images.chromadb_indexer_image_url,
                defaultDescriptor=_CHROMADB_INDEXER_IMAGE_DESCRIPTOR,
            ),
        )

        self.setIngress(
            to={
                "operator_name": _STRUCTURE_EXTRACTOR_OPERATOR_NAME,
                "port_name": "extract",
            },
            linkName="link-input",
        )
        self.connect(
            from_={
                "operator_name": _STRUCTURE_EXTRACTOR_OPERATOR_NAME,
                "port_name": "chunk",
            },
            to={
                "operator_name": _CHUNKER_OPERATOR_NAME,
                "port_name": "chunk",
            },
            linkName="link-extract-to-chunk",
        )
        self.connect(
            from_={
                "operator_name": _CHUNKER_OPERATOR_NAME,
                "port_name": "store",
            },
            to={
                "operator_name": _CHROMADB_INDEXER_OPERATOR_NAME,
                "port_name": "store",
            },
            linkName="link-chunk-to-store",
        )

        return self.createDag()


def buildAsyncIngestLightDag(
    *,
    images: AsyncIngestOperatorImages,
    dag_name: str = _DEFAULT_ASYNC_INGEST_LIGHT_DAG_NAME,
) -> Proto.dag.Dag:
    return AsyncIngestLightGraph(images=images, dag_name=dag_name).materializeDag()


def buildAsyncIngestLightSubmitJobRequest(
    *,
    owner: str,
    cluster_id: str,
    images: AsyncIngestOperatorImages,
    dag_name: str = _DEFAULT_ASYNC_INGEST_LIGHT_DAG_NAME,
) -> Proto.flow_server.SubmitJobRequest:
    if not owner.strip():
        raise ValueError("owner must not be empty")
    if not cluster_id.strip():
        raise ValueError("cluster_id must not be empty")
    return Proto.flow_server.SubmitJobRequest(
        owner=owner.strip(),
        cluster_id=cluster_id.strip(),
        app_id="",
        job_dag=buildAsyncIngestLightDag(images=images, dag_name=dag_name),
    )


def buildAsyncIngestLightStartJobRequest(*, job_id: str) -> Proto.flow_server.StartJobRequest:
    if not job_id.strip():
        raise ValueError("job_id must not be empty")
    return Proto.flow_server.StartJobRequest(job_id=job_id.strip())


def buildAsyncIngestLightInjectMessageRequest(
    *,
    job_id: str,
    uri: str,
    rag_uri: str,
    embedding_model: int = operators_pb2.EMBEDDING_MODEL_ALL_MINILM_L6_V2,
    batch_size: int = 0,
    infer_tables: bool = True,
    extract_images: bool = False,
    trace_id: str = "",
) -> Proto.flow_server.InjectMessageRequest:
    if not job_id.strip():
        raise ValueError("job_id must not be empty")
    if not uri.strip():
        raise ValueError("uri must not be empty")
    parseRagUri(rag_uri=rag_uri)

    request = operators_pb2.StructureExtractorRequest(
        uri=uri.strip(),
        infer_tables=infer_tables,
        strategy=operators_pb2.STRUCTURE_EXTRACTOR_STRATEGY_FAST,
        extract_images=extract_images,
        batch_size=max(0, int(batch_size)),
        embedding_model=embedding_model,
        rag_uri=rag_uri,
    )
    opnetMessage = Proto.opnet_types.OpNetMessage(
        payload_type=_buildPayloadType(
            type_url=operators_pb2.StructureExtractorRequest.DESCRIPTOR.full_name
        ),
        payload=request.SerializeToString(),
    )
    if trace_id.strip():
        opnetMessage.message_context.trace_id = trace_id.strip()
    return Proto.flow_server.InjectMessageRequest(
        job_id=job_id.strip(),
        message=opnetMessage,
    )


def _buildPayloadType(*, type_url: str) -> Proto.opnet_types.OpNetPayloadType:
    canonicalTypeUrl = _canonicalizeTypeUrl(type_url=type_url)
    return Proto.opnet_types.OpNetPayloadType(
        serialization_format=Proto.opnet_types.OpNetPayloadType.PROTO,
        type_url=canonicalTypeUrl,
    )


def _canonicalizeTypeUrl(*, type_url: str) -> str:
    normalized = type_url.strip()
    if not normalized:
        return normalized
    if normalized.startswith(_CANONICAL_ANY_TYPE_PREFIX):
        return normalized
    if "/" in normalized:
        return normalized
    return f"{_CANONICAL_ANY_TYPE_PREFIX}{normalized}"


def _buildOperatorDescriptor(
    *,
    imageUrl: str,
    defaultDescriptor: OperatorImageDescriptor,
) -> OperatorImageDescriptor:
    normalizedImageUrl = imageUrl.strip()
    if not normalizedImageUrl:
        raise ValueError("imageUrl must not be empty")
    return OperatorImageDescriptor(
        imageUrl=normalizedImageUrl,
        ports=defaultDescriptor.ports,
    )
