"""Graph extractor for LORe graph publish pipeline."""

from __future__ import annotations

import argparse
import ast
import json
import logging

LOG = logging.getLogger(__name__)
_SCHEMA_VERSION = 1
_VALID_GRAPH_TYPES = frozenset({"sync", "async"})


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


def _dottedName(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dottedName(node.value)
        if not prefix:
            return ""
        return f"{prefix}.{node.attr}"
    return ""


def _findClassDef(*, moduleAst: ast.Module, className: str) -> ast.ClassDef:
    for node in moduleAst.body:
        if isinstance(node, ast.ClassDef) and node.name == className:
            return node
    LOG.error("class '%s' not found in source", className)
    raise SystemExit(1)


def _extractGraphType(*, classDef: ast.ClassDef, className: str) -> str:
    graphTypeValues: list[str] = []
    for decorator in classDef.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        decoratorName = _dottedName(decorator.func)
        if decoratorName.split(".")[-1] != "graph_type":
            continue
        if len(decorator.args) != 1 or decorator.keywords:
            LOG.error("'%s' @graph_type must have one positional argument", className)
            raise SystemExit(1)
        arg = decorator.args[0]
        if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
            LOG.error("'%s' @graph_type argument must be a string literal", className)
            raise SystemExit(1)
        graphTypeValues.append(arg.value.strip().lower())

    if len(graphTypeValues) == 0:
        LOG.error("'%s' is missing @graph_type('sync'|'async')", className)
        raise SystemExit(1)
    if len(graphTypeValues) > 1:
        LOG.error("'%s' has multiple @graph_type decorators", className)
        raise SystemExit(1)
    graphType = graphTypeValues[0]
    if graphType not in _VALID_GRAPH_TYPES:
        LOG.error("'%s' has unsupported graph type '%s'", className, graphType)
        raise SystemExit(1)
    return graphType


def _extractMaterializeMethod(*, classDef: ast.ClassDef, className: str) -> str:
    methodNames: list[str] = []
    for node in classDef.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        hasMaterialize = False
        for decorator in node.decorator_list:
            decoratorName = _dottedName(decorator)
            if decoratorName.split(".")[-1] == "materialize":
                hasMaterialize = True
                break
        if hasMaterialize:
            methodNames.append(node.name)

    if len(methodNames) == 0:
        LOG.error("'%s' must define exactly one @materialize method", className)
        raise SystemExit(1)
    if len(methodNames) > 1:
        LOG.error("'%s' has multiple @materialize methods: %s", className, sorted(methodNames))
        raise SystemExit(1)
    return methodNames[0]


def _buildManifest(*, moduleAst: ast.Module, className: str) -> dict[str, object]:
    classDef = _findClassDef(moduleAst=moduleAst, className=className)
    graphType = _extractGraphType(classDef=classDef, className=className)
    materializeMethod = _extractMaterializeMethod(classDef=classDef, className=className)
    return {
        "schema_version": _SCHEMA_VERSION,
        "graph_class": className,
        "graph_type": graphType,
        "materialize_method": materializeMethod,
    }


def _parseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--class", dest="className", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = _parseArgs()
    manifest = _buildManifest(
        moduleAst=_loadSourceAst(sourcePath=args.source),
        className=args.className,
    )
    with open(args.output, "w", encoding="utf-8") as outputFile:
        json.dump(manifest, outputFile, indent=2, sort_keys=True)
        outputFile.write("\n")
    LOG.info("wrote graph manifest to %s", args.output)


if __name__ == "__main__":
    main()
