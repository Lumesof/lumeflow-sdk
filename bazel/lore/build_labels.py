"""Build LORe OCI labels before image construction."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)


def _loadPortsPayload(*, path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        LOG.error("missing ports manifest: %s", path)
        raise SystemExit(1) from exc
    except json.JSONDecodeError as exc:
        LOG.error("failed to parse ports manifest %s: %s", path, exc)
        raise SystemExit(1) from exc

    if not isinstance(payload, dict):
        LOG.error("ports manifest must be a JSON object")
        raise SystemExit(1)
    schemaVersion = payload.get("schema_version")
    if schemaVersion is not None and schemaVersion != 1:
        LOG.error("unsupported ports manifest schema_version '%s'", schemaVersion)
        raise SystemExit(1)
    return payload


def _normalizePortEntries(*, payload: dict[str, Any], key: str) -> list[dict[str, str]]:
    value = payload.get(key)
    if not isinstance(value, list):
        LOG.error("ports manifest key '%s' must be a list", key)
        raise SystemExit(1)

    entries: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            LOG.error("ports manifest key '%s' must contain objects", key)
            raise SystemExit(1)
        portName = item.get("name")
        serializationFormat = item.get("serialization_format")
        typeUrl = item.get("type_url")
        if (
            not isinstance(portName, str)
            or not isinstance(serializationFormat, str)
            or not isinstance(typeUrl, str)
        ):
            LOG.error(
                "ports manifest key '%s' entries must have string name/serialization_format/type_url",
                key,
            )
            raise SystemExit(1)
        entries.append({
            "name": portName,
            "serialization_format": serializationFormat,
            "type_url": typeUrl,
        })

    return sorted(entries, key=lambda entry: entry["name"])


def _buildPortLabel(*, payload: dict[str, Any], key: str) -> str:
    entries = _normalizePortEntries(payload=payload, key=key)
    return ",".join(
        f"{entry['name']}:{entry['serialization_format']}:{entry['type_url']}"
        for entry in entries
    )


def _buildLoreLabels(*, args: argparse.Namespace, portsPayload: dict[str, Any]) -> dict[str, str]:
    return {
        "lore.category": args.category,
        "lore.changelog": args.changelog,
        "lore.description": args.description,
        "lore.lumeflow.min_version": args.lumeflow_min_version,
        "lore.ports.egress": _buildPortLabel(payload=portsPayload, key="egress"),
        "lore.ports.ingress": _buildPortLabel(payload=portsPayload, key="ingress"),
        "lore.publisher": args.publisher,
        "lore.slug": args.slug,
        "lore.version": args.version,
        "lore.visibility": args.visibility,
    }


def _parseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ports-json", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--publisher", required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--category", required=True)
    parser.add_argument("--visibility", required=True)
    parser.add_argument("--lumeflow-min-version", required=True)
    parser.add_argument("--changelog", required=True)
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = _parseArgs()

    portsPayload = _loadPortsPayload(path=Path(args.ports_json))
    labels = _buildLoreLabels(args=args, portsPayload=portsPayload)

    outputPath = Path(args.output)
    outputPath.write_text(
        "\n".join(f"{key}={value}" for key, value in sorted(labels.items())) + "\n",
        encoding="utf-8",
    )
    LOG.info("wrote LORe labels to %s", outputPath)


if __name__ == "__main__":
    main()
