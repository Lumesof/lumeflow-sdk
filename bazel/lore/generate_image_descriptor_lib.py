"""Generate a Python module that exposes an OperatorImageDescriptor."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _parseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ports-json", required=True)
    parser.add_argument("--digest-file", required=True)
    parser.add_argument("--image-repository", required=True)
    parser.add_argument("--module-path", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def _loadPorts(*, portsJsonPath: Path) -> dict[str, Any]:
    payload = json.loads(portsJsonPath.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("ports manifest must be a JSON object")
    return payload


def _readDigest(*, digestPath: Path) -> str:
    digest = digestPath.read_text(encoding="utf-8").strip()
    if not digest.startswith("sha256:"):
        raise ValueError(f"digest must start with 'sha256:', got '{digest}'")
    return digest


def _normalizePortEntries(*, portsPayload: dict[str, Any], key: str) -> list[dict[str, str]]:
    value = portsPayload.get(key)
    if not isinstance(value, list):
        raise ValueError(f"ports manifest key '{key}' must be a list")

    entries: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"ports manifest key '{key}' must contain objects")
        name = item.get("name")
        serializationFormat = item.get("serialization_format")
        typeUrl = item.get("type_url")
        if (
            not isinstance(name, str)
            or not isinstance(serializationFormat, str)
            or not isinstance(typeUrl, str)
        ):
            raise ValueError(
                f"ports manifest key '{key}' entries must have string name/serialization_format/type_url"
            )
        entries.append(
            {
                "name": name,
                "direction": key,
                "serializationFormat": serializationFormat,
                "typeUrl": typeUrl,
            }
        )
    return sorted(entries, key=lambda entry: entry["name"])


def _buildModuleSource(
    *,
    imageRepository: str,
    digest: str,
    portsPayload: dict[str, Any],
) -> str:
    imageUrl = f"{imageRepository}@{digest}"
    portEntries = _normalizePortEntries(portsPayload=portsPayload, key="ingress") + _normalizePortEntries(
        portsPayload=portsPayload,
        key="egress",
    )
    portsJson = json.dumps(portEntries, indent=4, sort_keys=True)
    return (
        "from __future__ import annotations\n"
        "\n"
        "from lumesof.lumeflow import OperatorImageDescriptor, OperatorPortDescriptor\n"
        "\n"
        "_PORT_ENTRIES = "
        f"{portsJson}\n"
        "\n"
        "_IMAGE_DESCRIPTOR = OperatorImageDescriptor(\n"
        f"    imageUrl={json.dumps(imageUrl)},\n"
        "    ports=tuple(\n"
        "        OperatorPortDescriptor(\n"
        "            name=entry[\"name\"],\n"
        "            direction=entry[\"direction\"],\n"
        "            serializationFormat=entry[\"serializationFormat\"],\n"
        "            typeUrl=entry[\"typeUrl\"],\n"
        "        )\n"
        "        for entry in _PORT_ENTRIES\n"
        "    ),\n"
        ")\n"
        "\n"
        "__all__ = [\"_IMAGE_DESCRIPTOR\"]\n"
    )


def main() -> None:
    args = _parseArgs()
    portsPayload = _loadPorts(portsJsonPath=Path(args.ports_json))
    digest = _readDigest(digestPath=Path(args.digest_file))
    source = _buildModuleSource(
        imageRepository=args.image_repository,
        digest=digest,
        portsPayload=portsPayload,
    )
    outputPath = Path(args.output)
    outputPath.parent.mkdir(parents=True, exist_ok=True)
    outputPath.write_text(source, encoding="utf-8")


if __name__ == "__main__":
    main()

