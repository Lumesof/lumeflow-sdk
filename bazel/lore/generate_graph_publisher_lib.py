"""Generate a Python module that exposes a graph Publisher implementation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _parseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--module-path", required=True)
    parser.add_argument("--workspace-name", required=True)
    parser.add_argument("--publisher-path", action="append", default=[])
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def _buildModuleSource(*, workspaceName: str, publisherPaths: list[str]) -> str:
    uniquePaths = sorted({path.strip() for path in publisherPaths if path.strip()})
    if len(uniquePaths) == 0:
        raise ValueError("publisher-path must contain at least one executable path")

    publisherPathsJson = json.dumps(uniquePaths, indent=4)
    return (
        "from __future__ import annotations\n"
        "\n"
        "import asyncio\n"
        "import os\n"
        "import subprocess\n"
        "\n"
        "from python.runfiles import Runfiles\n"
        "\n"
        "from bazel.lore.base_publisher import BasePublisher\n"
        "\n"
        f"_MAIN_WORKSPACE_NAME = {json.dumps(workspaceName)}\n"
        f"_PUBLISHER_PATHS = {publisherPathsJson}\n"
        "\n"
        "\n"
        "class Publisher(BasePublisher):\n"
        "    async def publish(self) -> None:\n"
        "        await asyncio.to_thread(self._runPublishers)\n"
        "\n"
        "    def _runPublishers(self) -> None:\n"
        "        runfiles = Runfiles.Create()\n"
        "        if runfiles is None:\n"
        "            raise RuntimeError('Unable to initialize Bazel runfiles helper.')\n"
        "\n"
        "        for relativePath in _PUBLISHER_PATHS:\n"
        "            executablePath = self._resolveExecutable(runfiles=runfiles, relativePath=relativePath)\n"
        "            env = os.environ.copy()\n"
        "            process = subprocess.run(\n"
        "                [executablePath],\n"
        "                check=False,\n"
        "                capture_output=True,\n"
        "                text=True,\n"
        "                env=env,\n"
        "            )\n"
        "            if process.returncode != 0:\n"
        "                raise RuntimeError(\n"
        "                    f'Publisher failed for {relativePath} '\n"
        "                    f'(exit={process.returncode}) '\n"
        "                    f'stdout={process.stdout.strip()} stderr={process.stderr.strip()}'\n"
        "                )\n"
        "\n"
        "    @staticmethod\n"
        "    def _resolveExecutable(*, runfiles: Runfiles, relativePath: str) -> str:\n"
        "        candidates = (\n"
        "            relativePath,\n"
        "            f'{_MAIN_WORKSPACE_NAME}/{relativePath}',\n"
        "            f'_main/{relativePath}',\n"
        "        )\n"
        "        for candidate in candidates:\n"
        "            resolved = runfiles.Rlocation(candidate) or ''\n"
        "            if not resolved:\n"
        "                continue\n"
        "            if not os.path.isfile(resolved):\n"
        "                continue\n"
        "            if os.access(resolved, os.X_OK):\n"
        "                return resolved\n"
        "        raise RuntimeError(f'Unable to resolve publisher executable: {relativePath}')\n"
        "\n"
        "\n"
        "__all__ = ['Publisher']\n"
    )


def main() -> None:
    args = _parseArgs()
    source = _buildModuleSource(
        workspaceName=args.workspace_name,
        publisherPaths=args.publisher_path,
    )
    outputPath = Path(args.output)
    outputPath.parent.mkdir(parents=True, exist_ok=True)
    outputPath.write_text(source, encoding="utf-8")


if __name__ == "__main__":
    main()
