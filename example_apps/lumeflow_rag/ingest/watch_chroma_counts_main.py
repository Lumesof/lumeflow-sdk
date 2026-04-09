from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
import subprocess
import sys
import time
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request


_DEFAULT_CHROMA_NODE_PORT = 30090
_DEFAULT_TENANT = "default_tenant"
_DEFAULT_DATABASE = "default_database"
_DEFAULT_POLL_INTERVAL_SECONDS = 1.0
_DEFAULT_IDLE_TIMEOUT_SECONDS = 10.0
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class CollectionCountsSnapshot:
    countsByCollection: dict[str, int]
    collectionCount: int
    totalCount: int


def _resolvePublicHost(*, publicHost: str) -> str:
    cleanedHost = publicHost.strip()
    if cleanedHost:
        return cleanedHost
    result = subprocess.run(
        ["minikube", "ip"],
        capture_output=True,
        text=True,
        check=True,
    )
    host = result.stdout.strip()
    if not host:
        raise ValueError("minikube ip returned an empty host")
    return host


def _fetchJson(*, url: str, timeoutSeconds: float) -> Any:
    request = urllib_request.Request(url=url, method="GET")
    with urllib_request.urlopen(request, timeout=timeoutSeconds) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetchCollectionCountsSnapshot(
    *,
    baseUrl: str,
    tenant: str,
    database: str,
    timeoutSeconds: float,
) -> CollectionCountsSnapshot:
    collectionsUrl = (
        f"{baseUrl}/api/v2/tenants/{tenant}/databases/{database}/collections"
    )
    collections = _fetchJson(url=collectionsUrl, timeoutSeconds=timeoutSeconds)
    if not isinstance(collections, list):
        raise RuntimeError(f"Unexpected collections payload type: {type(collections)!r}")

    countsByCollection: dict[str, int] = {}
    totalCount = 0
    for collection in collections:
        if not isinstance(collection, dict):
            continue
        collectionId = str(collection.get("id", "")).strip()
        if not collectionId:
            continue
        collectionName = str(collection.get("name", collectionId)).strip() or collectionId
        countUrl = (
            f"{baseUrl}/api/v2/tenants/{tenant}/databases/{database}/collections/"
            f"{collectionId}/count"
        )
        rawCount = urllib_request.urlopen(countUrl, timeout=timeoutSeconds).read().decode("utf-8").strip()
        countValue = int(rawCount or "0")
        countsByCollection[collectionName] = countValue
        totalCount += countValue

    return CollectionCountsSnapshot(
        countsByCollection=countsByCollection,
        collectionCount=len(countsByCollection),
        totalCount=totalCount,
    )


def _formatUtcTimestamp() -> str:
    now = datetime.now(tz=timezone.utc)
    return now.strftime("%Y-%m-%d %H:%M:%S UTC")


def _renderFrame(*, lines: list[str], useAnsiInPlace: bool) -> None:
    if useAnsiInPlace:
        sys.stdout.write("\x1b[H\x1b[2J")
        sys.stdout.write("\n".join(lines))
        sys.stdout.write("\n")
        sys.stdout.flush()
        return
    print("\n".join(lines), flush=True)


def _buildParser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Poll Chroma collection counts and refresh them in-place in the terminal."
        )
    )
    parser.add_argument(
        "--public-host",
        default="",
        help="Cluster public host (defaults to `minikube ip`).",
    )
    parser.add_argument(
        "--chroma-node-port",
        type=int,
        default=_DEFAULT_CHROMA_NODE_PORT,
        help="Chroma NodePort.",
    )
    parser.add_argument(
        "--tenant",
        default=_DEFAULT_TENANT,
        help="Chroma tenant.",
    )
    parser.add_argument(
        "--database",
        default=_DEFAULT_DATABASE,
        help="Chroma database.",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=_DEFAULT_POLL_INTERVAL_SECONDS,
        help="Polling interval in seconds.",
    )
    parser.add_argument(
        "--idle-timeout-seconds",
        type=float,
        default=_DEFAULT_IDLE_TIMEOUT_SECONDS,
        help="Auto-exit after this many seconds with no count changes.",
    )
    parser.add_argument(
        "--request-timeout-seconds",
        type=float,
        default=_DEFAULT_REQUEST_TIMEOUT_SECONDS,
        help="Per-request timeout in seconds.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _buildParser().parse_args(argv)
    if args.chroma_node_port <= 0:
        raise SystemExit("--chroma-node-port must be > 0")
    if args.poll_interval_seconds <= 0:
        raise SystemExit("--poll-interval-seconds must be > 0")
    if args.idle_timeout_seconds <= 0:
        raise SystemExit("--idle-timeout-seconds must be > 0")
    if args.request_timeout_seconds <= 0:
        raise SystemExit("--request-timeout-seconds must be > 0")

    publicHost = _resolvePublicHost(publicHost=args.public_host)
    baseUrl = f"http://{publicHost}:{args.chroma_node_port}"
    useAnsiInPlace = sys.stdout.isatty()

    previousSnapshot: CollectionCountsSnapshot | None = None
    lastChangeMonotonic = time.monotonic()

    try:
        while True:
            nowMonotonic = time.monotonic()
            elapsedWithoutChange = nowMonotonic - lastChangeMonotonic
            status = "ok"
            snapshot = previousSnapshot
            errorMessage = ""

            try:
                snapshot = _fetchCollectionCountsSnapshot(
                    baseUrl=baseUrl,
                    tenant=args.tenant,
                    database=args.database,
                    timeoutSeconds=args.request_timeout_seconds,
                )
                if previousSnapshot is None or snapshot != previousSnapshot:
                    previousSnapshot = snapshot
                    lastChangeMonotonic = nowMonotonic
                    elapsedWithoutChange = 0.0
                    status = "updated"
                else:
                    status = "unchanged"
            except (urllib_error.URLError, urllib_error.HTTPError, ValueError, RuntimeError) as exc:
                status = "error"
                errorMessage = str(exc)

            lines: list[str] = [
                "Chroma Collection Counts (live)",
                f"timestamp={_formatUtcTimestamp()}",
                (
                    f"endpoint={baseUrl} tenant={args.tenant} database={args.database} "
                    f"poll_s={args.poll_interval_seconds:.1f} idle_timeout_s={args.idle_timeout_seconds:.1f}"
                ),
                f"status={status} no_change_for_s={elapsedWithoutChange:.1f}",
                "",
            ]

            if snapshot is None:
                lines.append("No snapshot yet.")
            else:
                lines.append(
                    f"collections={snapshot.collectionCount} total={snapshot.totalCount}"
                )
                lines.append("")
                for collectionName in sorted(snapshot.countsByCollection):
                    count = snapshot.countsByCollection[collectionName]
                    lines.append(f"{collectionName}: {count}")

            if errorMessage:
                lines.extend(["", f"last_error={errorMessage}"])

            if elapsedWithoutChange >= args.idle_timeout_seconds:
                lines.extend(
                    [
                        "",
                        (
                            "Exiting: counts did not change for "
                            f"{args.idle_timeout_seconds:.1f}s."
                        ),
                    ]
                )
                _renderFrame(lines=lines, useAnsiInPlace=useAnsiInPlace)
                return 0

            _renderFrame(lines=lines, useAnsiInPlace=useAnsiInPlace)
            time.sleep(args.poll_interval_seconds)
    except KeyboardInterrupt:
        print("Interrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
