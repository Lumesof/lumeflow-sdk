from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Sequence

from python.runfiles import Runfiles

LOG = logging.getLogger(__name__)


def graph_type(_value: str):  # type: ignore[no-untyped-def]
    def _decorate(cls):  # type: ignore[no-untyped-def]
        return cls

    return _decorate


def materialize(method):  # type: ignore[no-untyped-def]
    return method


@graph_type("sync")
class SyncFixtureGraph:
    @materialize
    def buildDag(self):  # type: ignore[no-untyped-def]
        return None


@graph_type("async")
class AsyncFixtureGraph:
    @materialize
    def makeDag(self):  # type: ignore[no-untyped-def]
        return None


@graph_type("sync")
class MissingMaterializeFixtureGraph:
    def buildDag(self):  # type: ignore[no-untyped-def]
        return None


@graph_type("sync")
class MultipleMaterializeFixtureGraph:
    @materialize
    def buildDag(self):  # type: ignore[no-untyped-def]
        return None

    @materialize
    def buildSecondDag(self):  # type: ignore[no-untyped-def]
        return None


@graph_type("invalid")
class InvalidGraphTypeFixtureGraph:
    @materialize
    def buildDag(self):  # type: ignore[no-untyped-def]
        return None


class MissingGraphTypeFixtureGraph:
    @materialize
    def buildDag(self):  # type: ignore[no-untyped-def]
        return None


def _createRunfiles():
    return Runfiles.Create()


def _resolveRunfilePath(*, runfiles, candidateRunfilesPaths: Sequence[str]) -> str:
    candidates: list[str] = []
    runfilesRoot = os.environ.get("RUNFILES_DIR") or os.environ.get("RUNFILES_ROOT", "")
    for runfilePath in candidateRunfilesPaths:
        if runfiles is not None:
            resolved = runfiles.Rlocation(runfilePath) or ""
            if resolved:
                candidates.append(resolved)
        if runfilesRoot:
            candidates.append(os.path.join(runfilesRoot, runfilePath))
        candidates.append(runfilePath)
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    raise RuntimeError(f"runfile not found; checked: {', '.join(candidates)}")


class ExtractGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    def _extractorPath(self) -> str:
        runfiles = _createRunfiles()
        return _resolveRunfilePath(
            runfiles=runfiles,
            candidateRunfilesPaths=(
                "_main/bazel/lore/extract_graph",
                "bazel/lore/extract_graph",
            ),
        )

    def _sourcePath(self) -> str:
        return str(Path(__file__).resolve())

    def _runExtractor(
        self,
        *,
        className: str,
        outputPath: str,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            self._extractorPath(),
            "--source",
            self._sourcePath(),
            "--class",
            className,
            "--output",
            outputPath,
        ]
        LOG.info("running graph extractor for fixture %s", className)
        LOG.info("command: %s", " ".join(command))
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )

    def _extractManifest(self, *, className: str) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as tempDir:
            outputPath = os.path.join(tempDir, "graph.json")
            result = self._runExtractor(className=className, outputPath=outputPath)
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            with open(outputPath, encoding="utf-8") as outputFile:
                return json.load(outputFile)

    def test_extractsSyncGraphMetadata(self) -> None:
        manifest = self._extractManifest(className="SyncFixtureGraph")
        self.assertEqual(
            manifest,
            {
                "schema_version": 1,
                "graph_class": "SyncFixtureGraph",
                "graph_type": "sync",
                "materialize_method": "buildDag",
            },
        )

    def test_extractsAsyncGraphMetadata(self) -> None:
        manifest = self._extractManifest(className="AsyncFixtureGraph")
        self.assertEqual(manifest["graph_type"], "async")
        self.assertEqual(manifest["materialize_method"], "makeDag")

    def test_missingGraphTypeExitsNonZero(self) -> None:
        with tempfile.TemporaryDirectory() as tempDir:
            result = self._runExtractor(
                className="MissingGraphTypeFixtureGraph",
                outputPath=os.path.join(tempDir, "graph.json"),
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("@graph_type", result.stderr)

    def test_invalidGraphTypeExitsNonZero(self) -> None:
        with tempfile.TemporaryDirectory() as tempDir:
            result = self._runExtractor(
                className="InvalidGraphTypeFixtureGraph",
                outputPath=os.path.join(tempDir, "graph.json"),
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("unsupported graph type", result.stderr)

    def test_missingMaterializeExitsNonZero(self) -> None:
        with tempfile.TemporaryDirectory() as tempDir:
            result = self._runExtractor(
                className="MissingMaterializeFixtureGraph",
                outputPath=os.path.join(tempDir, "graph.json"),
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("@materialize", result.stderr)

    def test_multipleMaterializeExitsNonZero(self) -> None:
        with tempfile.TemporaryDirectory() as tempDir:
            result = self._runExtractor(
                className="MultipleMaterializeFixtureGraph",
                outputPath=os.path.join(tempDir, "graph.json"),
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("multiple @materialize", result.stderr)


if __name__ == "__main__":
    unittest.main()
