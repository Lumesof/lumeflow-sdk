import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence
from urllib.parse import urlparse

import requests

LOG = logging.getLogger(__name__)

_DEFAULT_DISCOVERY_TIMEOUT_SEC = 2.0


@dataclass
class OperatorModelDiscoveryFailureState:
    isFailed: bool = False
    modelKind: str = ""
    reason: str = ""

    def markFailed(self, *, modelKind: str, reason: str) -> None:
        normalizedReason = reason.strip() or "unknown discovery failure"
        self.isFailed = True
        self.modelKind = modelKind.strip()
        self.reason = normalizedReason

    def shouldRejectRequests(self) -> bool:
        return self.isFailed

    def rejectionMessage(self, *, operatorName: str) -> str:
        normalizedOperatorName = operatorName.strip() or "operator"
        if not self.isFailed:
            return ""
        kind = self.modelKind or "model"
        return (
            f"{normalizedOperatorName} is rejecting requests because {kind} model discovery failed: "
            f"{self.reason}"
        )


class OllamaModelDiscovery:
    def __init__(
        self,
        *,
        servingUri: str,
        httpGet: Optional[Callable[..., Any]] = None,
        httpPost: Optional[Callable[..., Any]] = None,
        timeoutSec: float = _DEFAULT_DISCOVERY_TIMEOUT_SEC,
    ) -> None:
        normalizedServingUri = servingUri.strip()
        if not normalizedServingUri:
            raise ValueError("servingUri must not be empty")
        parsed = urlparse(normalizedServingUri)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"servingUri scheme must be http or https: {servingUri}")
        if parsed.hostname is None or parsed.port is None:
            raise ValueError(f"servingUri must include host and port: {servingUri}")
        if timeoutSec <= 0:
            raise ValueError("timeoutSec must be > 0")

        self._servingUri = normalizedServingUri.rstrip("/")
        self._httpGet = httpGet or requests.get
        self._httpPost = httpPost or requests.post
        self._timeoutSec = timeoutSec

    def listAvailableModels(self) -> list[str]:
        response = self._httpGet(
            self._buildTagsUri(),
            timeout=self._timeoutSec,
        )
        self._raiseForStatus(response=response)
        payload = self._parseJsonResponse(response=response)
        models = payload.get("models", [])
        if not isinstance(models, list):
            raise RuntimeError("ollama /api/tags response did not contain a models list")

        discovered: list[str] = []
        for item in models:
            if not isinstance(item, dict):
                continue
            rawName = item.get("name", item.get("model", ""))
            modelName = str(rawName).strip()
            if modelName and modelName not in discovered:
                discovered.append(modelName)
        return discovered

    def discoverLlmModel(
        self,
        *,
        preferredModels: Sequence[str],
    ) -> tuple[Optional[str], Optional[str]]:
        try:
            availableModels = self.listAvailableModels()
        except Exception as exc:
            return None, f"failed to list models from ollama: {exc}"

        if not availableModels:
            return None, "ollama returned no available models (available_models=[])"

        preferredByName = [model.strip() for model in preferredModels if model.strip()]
        availableByLower = {model.lower(): model for model in availableModels}
        for preferred in preferredByName:
            matched = availableByLower.get(preferred.lower())
            if matched:
                LOG.info("Model discovery succeeded kind=llm model=%s", matched)
                return matched, None

        for model in availableModels:
            if not self._looksLikeEmbeddingModel(modelName=model):
                LOG.info("Model discovery succeeded kind=llm model=%s", model)
                return model, None

        return None, (
            "no non-embedding models discovered in ollama tags response "
            f"(available_models={availableModels})"
        )

    def discoverEmbeddingModel(
        self,
        *,
        preferredModels: Sequence[str],
    ) -> tuple[Optional[str], Optional[str]]:
        try:
            availableModels = self.listAvailableModels()
        except Exception as exc:
            return None, f"failed to list models from ollama: {exc}"

        if not availableModels:
            return None, "ollama returned no available models (available_models=[])"

        preferredByName = [model.strip() for model in preferredModels if model.strip()]
        orderedCandidates: list[str] = []
        availableByLower = {model.lower(): model for model in availableModels}
        for preferred in preferredByName:
            matched = availableByLower.get(preferred.lower())
            if matched and matched not in orderedCandidates:
                orderedCandidates.append(matched)

        for model in availableModels:
            if model not in orderedCandidates and self._looksLikeEmbeddingModel(modelName=model):
                orderedCandidates.append(model)
        for model in availableModels:
            if model not in orderedCandidates:
                orderedCandidates.append(model)

        probeErrors: list[str] = []
        for model in orderedCandidates:
            supported, reason = self._probeEmbeddingSupport(modelName=model)
            if supported:
                LOG.info("Model discovery succeeded kind=embedding model=%s", model)
                return model, None
            probeErrors.append(f"{model}: {reason}")

        return None, (
            "no embedding-capable model discovered "
            f"(available_models={availableModels}); probe failures: " + "; ".join(probeErrors)
        )

    def _probeEmbeddingSupport(self, *, modelName: str) -> tuple[bool, str]:
        probePayload = {
            "model": modelName,
            "input": "model-discovery-probe",
        }

        embedResponse = self._httpPost(
            self._buildEmbedUri(),
            json=probePayload,
            timeout=self._timeoutSec,
        )
        if self._statusCode(embedResponse) >= 400:
            return False, f"/api/embed returned status={self._statusCode(embedResponse)}"
        try:
            payload = self._parseJsonResponse(response=embedResponse)
        except Exception as exc:
            return False, f"/api/embed returned invalid json: {exc}"
        embeddings = payload.get("embeddings")
        if isinstance(embeddings, list) and embeddings:
            return True, ""
        if isinstance(embeddings, list):
            return True, ""
        return False, "/api/embed response did not contain embeddings"

    def _buildTagsUri(self) -> str:
        return f"{self._servingUri}/api/tags"

    def _buildEmbedUri(self) -> str:
        return f"{self._servingUri}/api/embed"

    def _looksLikeEmbeddingModel(self, *, modelName: str) -> bool:
        normalized = modelName.lower()
        indicators = ("embed", "embedding", "bge", "minilm")
        return any(indicator in normalized for indicator in indicators)

    def _raiseForStatus(self, *, response: Any) -> None:
        raiseForStatus = getattr(response, "raise_for_status", None)
        if callable(raiseForStatus):
            raiseForStatus()
            return
        status = self._statusCode(response)
        if status >= 400:
            raise RuntimeError(f"http status={status}")

    def _statusCode(self, response: Any) -> int:
        status = getattr(response, "status_code", 0)
        try:
            return int(status)
        except Exception:
            return 0

    def _parseJsonResponse(self, *, response: Any) -> dict[str, Any]:
        jsonMethod = getattr(response, "json", None)
        if not callable(jsonMethod):
            raise RuntimeError("response has no json() method")
        payload = jsonMethod()
        if not isinstance(payload, dict):
            raise RuntimeError("json payload is not an object")
        return payload


__all__ = [
    "OllamaModelDiscovery",
    "OperatorModelDiscoveryFailureState",
]
