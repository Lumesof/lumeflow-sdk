from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from example_apps.lumeflow_rag.common.collection_naming import buildCollectionName
from example_apps.lumeflow_rag.common.rag_uri import ParsedRagUri, parseRagUri, validateRagUriEndpointAllowed


_DEFAULT_TOP_K = 15
_DEFAULT_REQUEST_TIMEOUT_SEC = 300


@dataclass(frozen=True)
class IngestPayload:
    source_uri: str
    rag_uri: str
    embedding_model: str
    content: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class AgentPayload:
    rag_uri: str
    prompt: str
    serving_uri: str
    model: str
    conversation_id: str
    response_id: str
    top_k: int
    request_timeout_sec: int


@dataclass(frozen=True)
class ChromaConfigDefaults:
    endpoint: str
    default_collection: str


@dataclass(frozen=True)
class RagRequestPolicy:
    allowed_rag_endpoints: frozenset[str] = field(default_factory=frozenset)
    max_ingest_content_chars: int = 1_000_000
    max_prompt_chars: int = 32_000
    max_id_chars: int = 256
    min_top_k: int = 1
    max_top_k: int = 100
    min_request_timeout_sec: int = 1
    max_request_timeout_sec: int = 1_800


_DEFAULT_POLICY = RagRequestPolicy()


def normalizeIngestPayload(
    *,
    payload: Mapping[str, Any],
    policy: RagRequestPolicy = _DEFAULT_POLICY,
) -> IngestPayload:
    _rejectInfraMessageId(payload=payload)
    source_uri = _requiredString(payload=payload, field_name="source_uri")
    rag_uri = _requiredString(payload=payload, field_name="rag_uri")
    _validateRagUriWithPolicy(
        rag_uri=rag_uri,
        allowed_rag_endpoints=policy.allowed_rag_endpoints,
    )
    embedding_model = _requiredString(payload=payload, field_name="embedding_model")
    content = _requiredString(payload=payload, field_name="content")
    _validateBoundedLength(
        value=content,
        field_name="content",
        max_length=policy.max_ingest_content_chars,
    )
    metadata_value = payload.get("metadata")
    metadata = dict(metadata_value) if isinstance(metadata_value, Mapping) else {}
    return IngestPayload(
        source_uri=source_uri,
        rag_uri=rag_uri,
        embedding_model=embedding_model,
        content=content,
        metadata=metadata,
    )


def normalizeAgentPayload(
    *,
    payload: Mapping[str, Any],
    default_model: str,
    policy: RagRequestPolicy = _DEFAULT_POLICY,
) -> AgentPayload:
    _rejectInfraMessageId(payload=payload)
    rag_uri = _requiredString(payload=payload, field_name="rag_uri")
    _validateRagUriWithPolicy(
        rag_uri=rag_uri,
        allowed_rag_endpoints=policy.allowed_rag_endpoints,
    )
    prompt = _requiredString(payload=payload, field_name="prompt")
    _validateBoundedLength(
        value=prompt,
        field_name="prompt",
        max_length=policy.max_prompt_chars,
    )
    serving_uri = _requiredString(payload=payload, field_name="serving_uri")
    model = _optionalString(payload=payload, field_name="model") or default_model
    conversation_id = _optionalString(payload=payload, field_name="conversation_id")
    response_id = _optionalString(payload=payload, field_name="response_id")
    _validateBoundedLength(
        value=conversation_id,
        field_name="conversation_id",
        max_length=policy.max_id_chars,
    )
    _validateBoundedLength(
        value=response_id,
        field_name="response_id",
        max_length=policy.max_id_chars,
    )
    top_k = _optionalBoundedInt(
        payload=payload,
        field_name="top_k",
        default_value=_DEFAULT_TOP_K,
        min_value=policy.min_top_k,
        max_value=policy.max_top_k,
    )
    request_timeout_sec = _optionalBoundedInt(
        payload=payload,
        field_name="request_timeout_sec",
        default_value=_DEFAULT_REQUEST_TIMEOUT_SEC,
        min_value=policy.min_request_timeout_sec,
        max_value=policy.max_request_timeout_sec,
    )
    return AgentPayload(
        rag_uri=rag_uri,
        prompt=prompt,
        serving_uri=serving_uri,
        model=model,
        conversation_id=conversation_id,
        response_id=response_id,
        top_k=top_k,
        request_timeout_sec=request_timeout_sec,
    )


def buildRagUri(
    *,
    endpoint: str,
    tenant_or_project: str,
    kb_or_index: str,
    version: str,
) -> str:
    collection_name = buildCollectionName(
        tenant_or_project=tenant_or_project,
        kb_or_index=kb_or_index,
        version=version,
    )
    cleaned = endpoint.rstrip("/")
    candidate = f"{cleaned}/{collection_name}"
    parsed = parseRagUri(rag_uri=candidate)
    return _serializeParsedRagUri(parsed=parsed)


def resolveChromaConfigDefaults(*, configByKey: Mapping[str, Any]) -> ChromaConfigDefaults:
    endpoint = _readConfigString(
        configByKey=configByKey,
        key="rag.chroma.endpoint",
        default_value="http://chromadb:8000",
    )
    default_collection = _readConfigString(
        configByKey=configByKey,
        key="rag.chroma.default_collection",
        default_value="default",
    )
    return ChromaConfigDefaults(
        endpoint=endpoint,
        default_collection=default_collection,
    )


def buildRagUriFromConfig(
    *,
    configByKey: Mapping[str, Any],
    tenant_or_project: str,
    kb_or_index: str,
    version: str,
) -> str:
    defaults = resolveChromaConfigDefaults(configByKey=configByKey)
    return buildRagUri(
        endpoint=defaults.endpoint,
        tenant_or_project=tenant_or_project,
        kb_or_index=kb_or_index,
        version=version,
    )


def _serializeParsedRagUri(*, parsed: ParsedRagUri) -> str:
    return f"{parsed.endpoint()}/{parsed.collection}"


def _validateRagUriWithPolicy(
    *,
    rag_uri: str,
    allowed_rag_endpoints: frozenset[str],
) -> None:
    parseRagUri(rag_uri=rag_uri)
    if allowed_rag_endpoints:
        validateRagUriEndpointAllowed(
            rag_uri=rag_uri,
            allowed_endpoints=set(allowed_rag_endpoints),
        )


def _rejectInfraMessageId(*, payload: Mapping[str, Any]) -> None:
    if "message_id" in payload:
        raise ValueError("message_id is infra-internal; use trace_id or payload-owned IDs")


def _requiredString(*, payload: Mapping[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optionalString(*, payload: Mapping[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string when provided")
    return value.strip()


def _optionalBoundedInt(
    *,
    payload: Mapping[str, Any],
    field_name: str,
    default_value: int,
    min_value: int,
    max_value: int,
) -> int:
    raw_value = payload.get(field_name)
    if raw_value is None:
        value = default_value
    elif isinstance(raw_value, bool):
        raise ValueError(f"{field_name} must be an integer")
    elif isinstance(raw_value, int):
        value = raw_value
    elif isinstance(raw_value, float):
        if not raw_value.is_integer():
            raise ValueError(f"{field_name} must be an integer")
        value = int(raw_value)
    elif isinstance(raw_value, str):
        cleaned = raw_value.strip()
        if not cleaned:
            raise ValueError(f"{field_name} must not be blank when provided")
        try:
            value = int(cleaned)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an integer") from exc
    else:
        raise ValueError(f"{field_name} must be an integer")

    if value < min_value or value > max_value:
        raise ValueError(
            f"{field_name} out of policy bounds: received {value}, allowed [{min_value}, {max_value}]"
        )
    return value


def _validateBoundedLength(*, value: str, field_name: str, max_length: int) -> None:
    if not value:
        return
    if len(value) > max_length:
        raise ValueError(
            f"{field_name} exceeds policy max length: received {len(value)}, max {max_length}"
        )


def _readConfigString(
    *,
    configByKey: Mapping[str, Any],
    key: str,
    default_value: str,
) -> str:
    raw = configByKey.get(key)
    if raw is None:
        return default_value
    if not isinstance(raw, str):
        raise ValueError(f"{key} must be a string when provided")
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError(f"{key} must be non-empty when provided")
    return cleaned
