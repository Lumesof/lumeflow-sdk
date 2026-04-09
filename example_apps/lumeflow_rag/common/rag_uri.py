from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from urllib.parse import urlparse


@dataclass(frozen=True)
class ParsedRagUri:
    scheme: str
    host: str
    port: int
    collection: str

    def endpoint(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"


def parseRagUri(*, rag_uri: str) -> ParsedRagUri:
    trimmed = rag_uri.strip()
    if not trimmed:
        raise ValueError("rag_uri must not be empty")

    parsed = urlparse(trimmed)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("rag_uri scheme must be http or https")
    if parsed.hostname is None:
        raise ValueError("rag_uri must include host")
    if parsed.port is None:
        raise ValueError("rag_uri must include port")

    collection = parsed.path.lstrip("/")
    if not collection:
        raise ValueError("rag_uri must include collection path")
    _validateCollectionPath(collection=collection)

    return ParsedRagUri(
        scheme=parsed.scheme,
        host=parsed.hostname,
        port=parsed.port,
        collection=collection,
    )


def validateRagUriEndpointAllowed(
    *,
    rag_uri: str,
    allowed_endpoints: set[str],
) -> None:
    parsed = parseRagUri(rag_uri=rag_uri)
    endpoint = parsed.endpoint()
    if endpoint not in allowed_endpoints:
        raise ValueError(f"rag_uri endpoint is not allowlisted: {endpoint}")


def isPrivateHost(*, host: str) -> bool:
    lowered = host.strip().lower()
    if lowered in {"localhost", "host.minikube.internal"}:
        return True
    try:
        ip = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return bool(
        ip.is_private or
        ip.is_loopback or
        ip.is_link_local
    )


def _validateCollectionPath(*, collection: str) -> None:
    if collection.startswith("/") or collection.endswith("/"):
        raise ValueError("collection path must not start or end with slash")
    for part in collection.split("/"):
        if not part:
            raise ValueError("collection path must not include empty segments")
        if part in {".", ".."}:
            raise ValueError("collection path segments must not be dot segments")
