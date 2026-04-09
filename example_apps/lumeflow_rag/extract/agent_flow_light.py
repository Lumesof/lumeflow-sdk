from __future__ import annotations

from dataclasses import dataclass

from example_apps.lumeflow_rag.extract.image_descriptors.chromadb_retriever_operator_nomic_embed_text_v1_5 import (
    _IMAGE_DESCRIPTOR as _CHROMADB_RETRIEVER_IMAGE_DESCRIPTOR,
)
from example_apps.lumeflow_rag.extract.image_descriptors.llm_agent_operator_qwen25_1_5b import (
    _IMAGE_DESCRIPTOR as _LLM_AGENT_IMAGE_DESCRIPTOR,
)
from example_apps.lumeflow_rag.common.rag_uri import parseRagUri
from example_apps.lumeflow_rag.common import operators_pb2
from lumesof.lumeflow import Graph
from lumesof.lumeflow import OperatorImageDescriptor
from lumesof.lumeflow import Proto
from lumesof.lumeflow import graph_type
from lumesof.lumeflow import materialize


_LLM_AGENT_OPERATOR_NAME = "rag-llm-agent"
_CHROMADB_RETRIEVER_OPERATOR_NAME = "rag-chromadb-retriever"
_CANONICAL_ANY_TYPE_PREFIX = "type.googleapis.com/"
_DEFAULT_SYNC_AGENT_LIGHT_DAG_NAME = "lumeflow-rag-sync-chat-loop-light-v1"


@dataclass(frozen=True)
class SyncAgentOperatorImages:
    llm_agent_image_url: str
    chromadb_retriever_image_url: str


@graph_type("sync")
class SyncAgentLightGraph(Graph):
    def __init__(
        self,
        *,
        images: SyncAgentOperatorImages,
        dag_name: str = _DEFAULT_SYNC_AGENT_LIGHT_DAG_NAME,
    ) -> None:
        self._images = images
        self._dagName = dag_name

    @materialize
    def buildDag(self) -> Proto.dag.Dag:
        self.setDagName(self._dagName)
        self.setAllowLoops(True)

        self.createOperatorFromImageDescriptor(
            name=_LLM_AGENT_OPERATOR_NAME,
            descriptor=_buildOperatorDescriptor(
                imageUrl=self._images.llm_agent_image_url,
                defaultDescriptor=_LLM_AGENT_IMAGE_DESCRIPTOR,
            ),
        )
        self.createOperatorFromImageDescriptor(
            name=_CHROMADB_RETRIEVER_OPERATOR_NAME,
            descriptor=_buildOperatorDescriptor(
                imageUrl=self._images.chromadb_retriever_image_url,
                defaultDescriptor=_CHROMADB_RETRIEVER_IMAGE_DESCRIPTOR,
            ),
        )

        self.setIngress(
            to={
                "operator_name": _LLM_AGENT_OPERATOR_NAME,
                "port_name": "initial_request",
            },
            linkName="link-input",
        )
        self.connect(
            from_={
                "operator_name": _LLM_AGENT_OPERATOR_NAME,
                "port_name": "retrieval_command",
            },
            to={
                "operator_name": _CHROMADB_RETRIEVER_OPERATOR_NAME,
                "port_name": "retrieval_command",
            },
            linkName="link-retrieval-command",
        )
        self.connect(
            from_={
                "operator_name": _CHROMADB_RETRIEVER_OPERATOR_NAME,
                "port_name": "enriched_request",
            },
            to={
                "operator_name": _LLM_AGENT_OPERATOR_NAME,
                "port_name": "enriched_request",
            },
            linkName="link-enriched-request",
        )
        self.setEgress(
            from_={
                "operator_name": _LLM_AGENT_OPERATOR_NAME,
                "port_name": "final_text",
            },
            linkName="link-output",
        )

        return self.createDag()


def buildSyncAgentLightDag(
    *,
    images: SyncAgentOperatorImages,
    dag_name: str = _DEFAULT_SYNC_AGENT_LIGHT_DAG_NAME,
) -> Proto.dag.Dag:
    return SyncAgentLightGraph(images=images, dag_name=dag_name).materializeDag()


def buildSyncAgentLightSubmitJobRequest(
    *,
    owner: str,
    cluster_id: str,
    app_id: str,
    images: SyncAgentOperatorImages,
    dag_name: str = _DEFAULT_SYNC_AGENT_LIGHT_DAG_NAME,
) -> Proto.flow_server.SubmitJobRequest:
    if not owner.strip():
        raise ValueError("owner must not be empty")
    if not cluster_id.strip():
        raise ValueError("cluster_id must not be empty")
    if not app_id.strip():
        raise ValueError("app_id must not be empty for synchronous jobs")
    return Proto.flow_server.SubmitJobRequest(
        owner=owner.strip(),
        cluster_id=cluster_id.strip(),
        app_id=app_id.strip(),
        job_dag=buildSyncAgentLightDag(images=images, dag_name=dag_name),
    )


def buildSyncAgentLightStartJobRequest(*, job_id: str) -> Proto.flow_server.StartJobRequest:
    if not job_id.strip():
        raise ValueError("job_id must not be empty")
    return Proto.flow_server.StartJobRequest(job_id=job_id.strip())


def buildSyncAgentLightInjectMessageRequest(
    *,
    job_id: str,
    rag_uri: str,
    serving_uri: str = "",
    prompt: str,
    conversation_id: str = "",
    response_id: str = "",
    trace_id: str = "",
) -> Proto.flow_server.InjectMessageRequest:
    if not job_id.strip():
        raise ValueError("job_id must not be empty")
    parseRagUri(rag_uri=rag_uri)
    if not prompt.strip():
        raise ValueError("prompt must not be empty")

    request = operators_pb2.AgentRequest(
        rag_uri=rag_uri,
        serving_uri=serving_uri.strip(),
        prompt=prompt.strip(),
        conversation_id=conversation_id.strip(),
        response_id=response_id.strip(),
    )
    opnetMessage = Proto.opnet_types.OpNetMessage(
        payload_type=_buildPayloadType(type_url=operators_pb2.AgentRequest.DESCRIPTOR.full_name),
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
