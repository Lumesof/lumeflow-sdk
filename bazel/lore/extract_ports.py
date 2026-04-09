"""Port extractor for LORe operator publish pipeline."""

from __future__ import annotations

import argparse
import ast
import json
import logging
from typing import Any

LOG = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_CANONICAL_ANY_TYPE_PREFIX = "type.googleapis.com/"
_PORTS_REQUIRED_KEYS = frozenset({"ingress", "egress"})
_PORT_SPEC_REQUIRED_KEYS = frozenset({"name", "serialization_format", "type_url"})
_SUPPORTED_SERIALIZATION_FORMATS = frozenset({"PROTO"})
_SERIALIZATION_FORMAT_BY_NUMERIC_VALUE = {
    1: "PROTO",
    2: "JSON",
    100: "CUSTOM",
}


def _loadSourceAst(*, sourcePath: str) -> ast.Module:
    try:
        with open(sourcePath, encoding="utf-8") as sourceFile:
            source = sourceFile.read()
    except OSError as exc:
        LOG.error("could not read source '%s': %s", sourcePath, exc)
        raise SystemExit(1) from exc

    try:
        return ast.parse(source, filename=sourcePath)
    except SyntaxError as exc:
        LOG.error("could not parse source '%s': %s", sourcePath, exc)
        raise SystemExit(1)


def _findClassDef(*, moduleAst: ast.Module, className: str) -> ast.ClassDef:
    for node in moduleAst.body:
        if isinstance(node, ast.ClassDef) and node.name == className:
            return node
    LOG.error("class '%s' not found in source", className)
    raise SystemExit(1)


def _collectModuleBindings(*, moduleAst: ast.Module) -> dict[str, ast.AST]:
    bindings: dict[str, ast.AST] = {}
    for node in moduleAst.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.value is not None:
                bindings[node.target.id] = node.value
    return bindings


def _dottedName(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dottedName(node.value)
        if not prefix:
            return ""
        return f"{prefix}.{node.attr}"
    return ""


def _resolveNode(
    *,
    node: ast.AST,
    moduleBindings: dict[str, ast.AST],
    className: str,
    resolutionPath: tuple[str, ...] = (),
) -> ast.AST:
    if isinstance(node, ast.Name):
        symbolName = node.id
        if symbolName in resolutionPath:
            LOG.error(
                "'%s' @operator_ports has cyclic symbol reference: %s -> %s",
                className,
                " -> ".join(resolutionPath),
                symbolName,
            )
            raise SystemExit(1)
        binding = moduleBindings.get(symbolName)
        if binding is None:
            LOG.error(
                "'%s' @operator_ports references unresolved symbol '%s'",
                className,
                symbolName,
            )
            raise SystemExit(1)
        return _resolveNode(
            node=binding,
            moduleBindings=moduleBindings,
            className=className,
            resolutionPath=resolutionPath + (symbolName,),
        )
    return node


def _extractDecoratorArgNode(
    *,
    classDef: ast.ClassDef,
    moduleBindings: dict[str, ast.AST],
    className: str,
) -> ast.AST:
    for decorator in classDef.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        decoratorName = _dottedName(decorator.func)
        if decoratorName.split(".")[-1] != "operator_ports":
            continue
        if decorator.keywords:
            LOG.error("'%s' @operator_ports must not use keyword arguments", className)
            raise SystemExit(1)
        if len(decorator.args) != 1:
            LOG.error("'%s' @operator_ports must provide exactly one positional argument", className)
            raise SystemExit(1)
        return _resolveNode(
            node=decorator.args[0],
            moduleBindings=moduleBindings,
            className=className,
        )

    LOG.error("'%s' is missing @operator_ports(...)", className)
    raise SystemExit(1)


def _dictItems(*, node: ast.Dict, className: str, context: str) -> list[tuple[str, ast.AST]]:
    items: list[tuple[str, ast.AST]] = []
    for keyNode, valueNode in zip(node.keys, node.values):
        if keyNode is None:
            LOG.error("'%s' %s cannot use dictionary unpacking", className, context)
            raise SystemExit(1)
        keyValue = _stringLiteral(node=keyNode, className=className, context=f"{context}.<key>")
        items.append((keyValue, valueNode))
    return items


def _stringLiteral(*, node: ast.AST, className: str, context: str) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    LOG.error("'%s' %s must be a string literal", className, context)
    raise SystemExit(1)


def _canonicalizeTypeUrl(*, identifier: str) -> str:
    if not identifier:
        return ""
    normalized = identifier.rsplit("/", 1)[-1]
    if not normalized:
        return ""
    return f"{_CANONICAL_ANY_TYPE_PREFIX}{normalized}"


def _parseSerializationFormat(
    *,
    node: ast.AST,
    className: str,
    context: str,
) -> str:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            candidate = node.value.strip()
            if "." in candidate:
                candidate = candidate.rsplit(".", 1)[-1]
            if candidate in _SUPPORTED_SERIALIZATION_FORMATS:
                return candidate
            LOG.error("'%s' %s has unsupported serialization_format '%s'", className, context, node.value)
            raise SystemExit(1)
        if isinstance(node.value, int) and not isinstance(node.value, bool):
            candidate = _SERIALIZATION_FORMAT_BY_NUMERIC_VALUE.get(node.value, str(node.value))
            if candidate in _SUPPORTED_SERIALIZATION_FORMATS:
                return candidate
            LOG.error("'%s' %s has unsupported serialization_format '%s'", className, context, candidate)
            raise SystemExit(1)
    dotted = _dottedName(node)
    if dotted:
        candidate = dotted.rsplit(".", 1)[-1]
        if candidate in _SUPPORTED_SERIALIZATION_FORMATS:
            return candidate
        LOG.error("'%s' %s has unsupported serialization_format '%s'", className, context, dotted)
        raise SystemExit(1)
    LOG.error("'%s' %s must be a supported serialization_format expression", className, context)
    raise SystemExit(1)


def _parsePortSpec(
    *,
    node: ast.AST,
    className: str,
    sectionName: str,
    index: int,
    moduleBindings: dict[str, ast.AST],
) -> dict[str, str]:
    resolvedNode = _resolveNode(node=node, moduleBindings=moduleBindings, className=className)
    if not isinstance(resolvedNode, ast.Dict):
        LOG.error(
            "'%s' @operator_ports.%s[%d] must be an object literal",
            className,
            sectionName,
            index,
        )
        raise SystemExit(1)

    itemMap = dict(
        _dictItems(
            node=resolvedNode,
            className=className,
            context=f"@operator_ports.{sectionName}[{index}]",
        )
    )
    keys = set(itemMap.keys())
    missingKeys = sorted(_PORT_SPEC_REQUIRED_KEYS - keys)
    extraKeys = sorted(keys - _PORT_SPEC_REQUIRED_KEYS)
    if missingKeys or extraKeys:
        LOG.error(
            "'%s' @operator_ports.%s[%d] must use keys %s; missing=%s extra=%s",
            className,
            sectionName,
            index,
            sorted(_PORT_SPEC_REQUIRED_KEYS),
            missingKeys,
            extraKeys,
        )
        raise SystemExit(1)

    name = _stringLiteral(
        node=_resolveNode(
            node=itemMap["name"],
            moduleBindings=moduleBindings,
            className=className,
        ),
        className=className,
        context=f"@operator_ports.{sectionName}[{index}].name",
    )
    if not name:
        LOG.error("'%s' @operator_ports.%s[%d].name must be non-empty", className, sectionName, index)
        raise SystemExit(1)

    serializationFormat = _parseSerializationFormat(
        node=_resolveNode(
            node=itemMap["serialization_format"],
            moduleBindings=moduleBindings,
            className=className,
        ),
        className=className,
        context=f"@operator_ports.{sectionName}[{index}].serialization_format",
    )

    typeUrlRaw = _stringLiteral(
        node=_resolveNode(
            node=itemMap["type_url"],
            moduleBindings=moduleBindings,
            className=className,
        ),
        className=className,
        context=f"@operator_ports.{sectionName}[{index}].type_url",
    )
    canonicalTypeUrl = _canonicalizeTypeUrl(identifier=typeUrlRaw)
    if not canonicalTypeUrl:
        LOG.error(
            "'%s' @operator_ports.%s[%d].type_url must resolve to a canonical type URL",
            className,
            sectionName,
            index,
        )
        raise SystemExit(1)

    return {
        "name": name,
        "serialization_format": serializationFormat,
        "type_url": canonicalTypeUrl,
    }


def _parsePortSection(
    *,
    node: ast.AST,
    className: str,
    sectionName: str,
    moduleBindings: dict[str, ast.AST],
) -> list[dict[str, str]]:
    resolvedNode = _resolveNode(node=node, moduleBindings=moduleBindings, className=className)
    if not isinstance(resolvedNode, ast.List):
        LOG.error("'%s' @operator_ports.%s must be a list literal", className, sectionName)
        raise SystemExit(1)

    normalized: list[dict[str, str]] = []
    seenNames: set[str] = set()
    for index, rawPortNode in enumerate(resolvedNode.elts):
        normalizedSpec = _parsePortSpec(
            node=rawPortNode,
            className=className,
            sectionName=sectionName,
            index=index,
            moduleBindings=moduleBindings,
        )
        portName = normalizedSpec["name"]
        if portName in seenNames:
            LOG.error(
                "'%s' @operator_ports.%s has duplicate port '%s'",
                className,
                sectionName,
                portName,
            )
            raise SystemExit(1)
        seenNames.add(portName)
        normalized.append(normalizedSpec)

    return sorted(normalized, key=lambda entry: entry["name"])


def _buildManifestFromDecorator(
    *,
    decoratorArgNode: ast.AST,
    moduleBindings: dict[str, ast.AST],
    className: str,
) -> dict[str, Any]:
    if not isinstance(decoratorArgNode, ast.Dict):
        LOG.error("'%s' @operator_ports(...) argument must resolve to an object literal", className)
        raise SystemExit(1)

    sectionMap = dict(
        _dictItems(
            node=decoratorArgNode,
            className=className,
            context="@operator_ports(...)",
        )
    )
    keys = set(sectionMap.keys())
    missingKeys = sorted(_PORTS_REQUIRED_KEYS - keys)
    extraKeys = sorted(keys - _PORTS_REQUIRED_KEYS)
    if missingKeys or extraKeys:
        LOG.error(
            "'%s' @operator_ports(...) must use keys %s; missing=%s extra=%s",
            className,
            sorted(_PORTS_REQUIRED_KEYS),
            missingKeys,
            extraKeys,
        )
        raise SystemExit(1)

    ingressPorts = _parsePortSection(
        node=sectionMap["ingress"],
        className=className,
        sectionName="ingress",
        moduleBindings=moduleBindings,
    )
    egressPorts = _parsePortSection(
        node=sectionMap["egress"],
        className=className,
        sectionName="egress",
        moduleBindings=moduleBindings,
    )
    return {
        "schema_version": _SCHEMA_VERSION,
        "ingress": ingressPorts,
        "egress": egressPorts,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--class", dest="className", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    moduleAst = _loadSourceAst(sourcePath=args.source)
    moduleBindings = _collectModuleBindings(moduleAst=moduleAst)
    classDef = _findClassDef(moduleAst=moduleAst, className=args.className)
    decoratorArgNode = _extractDecoratorArgNode(
        classDef=classDef,
        moduleBindings=moduleBindings,
        className=args.className,
    )
    manifest = _buildManifestFromDecorator(
        decoratorArgNode=decoratorArgNode,
        moduleBindings=moduleBindings,
        className=args.className,
    )

    with open(args.output, "w", encoding="utf-8") as outputFile:
        json.dump(manifest, outputFile, indent=2)

    LOG.info("wrote port manifest to %s", args.output)


if __name__ == "__main__":
    main()
