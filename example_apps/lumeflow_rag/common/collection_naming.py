from __future__ import annotations

import re


_VALID_PART = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def buildCollectionName(
    *,
    tenant_or_project: str,
    kb_or_index: str,
    version: str,
) -> str:
    tenant = _normalizePart(raw=tenant_or_project, field_name="tenant_or_project")
    kb = _normalizePart(raw=kb_or_index, field_name="kb_or_index")
    normalized_version = _normalizeVersion(raw=version)
    return f"{tenant}_{kb}_{normalized_version}"


def parseCollectionName(*, collection_name: str) -> tuple[str, str, str]:
    trimmed = collection_name.strip()
    pieces = trimmed.split("_")
    if len(pieces) != 3:
        raise ValueError("collection name must have exactly 3 underscore-separated segments")
    tenant_or_project, kb_or_index, version = pieces
    _validatePart(raw=tenant_or_project, field_name="tenant_or_project")
    _validatePart(raw=kb_or_index, field_name="kb_or_index")
    _validateVersion(raw=version)
    return tenant_or_project, kb_or_index, version


def _normalizePart(*, raw: str, field_name: str) -> str:
    normalized = raw.strip().lower().replace("_", "-")
    normalized = re.sub(r"[^a-z0-9-]+", "-", normalized)
    normalized = re.sub(r"-{2,}", "-", normalized)
    normalized = normalized.strip("-")
    _validatePart(raw=normalized, field_name=field_name)
    return normalized


def _validatePart(*, raw: str, field_name: str) -> None:
    if not raw:
        raise ValueError(f"{field_name} must not be empty")
    if not _VALID_PART.match(raw):
        raise ValueError(
            f"{field_name} must match lowercase kebab-case pattern: [a-z0-9]+(?:-[a-z0-9]+)*"
        )


def _normalizeVersion(*, raw: str) -> str:
    trimmed = raw.strip().lower().replace(".", "-")
    if trimmed.startswith("v"):
        trimmed = trimmed[1:]
    candidate = f"v{trimmed}"
    _validateVersion(raw=candidate)
    return candidate


def _validateVersion(*, raw: str) -> None:
    if not re.match(r"^v[0-9]+(?:-[0-9]+)*$", raw):
        raise ValueError("version must normalize to v<digits> with optional -<digits> segments")
