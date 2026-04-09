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


def operator_ports(_ports):  # type: ignore[no-untyped-def]
    def _decorator(cls):  # type: ignore[no-untyped-def]
        return cls

    return _decorator


class _PayloadType:
    PROTO = 1


REGULAR_PORTS = {
    "ingress": [
        {
            "name": "chunk",
            "serialization_format": _PayloadType.PROTO,
            "type_url": "pkg.ChunkRequest",
        },
    ],
    "egress": [
        {
            "name": "store",
            "serialization_format": _PayloadType.PROTO,
            "type_url": "pkg.StoreRequest",
        },
    ],
}


@operator_ports(REGULAR_PORTS)
class RegularPortsFixture:
    pass


@operator_ports(
    {
        "ingress": [],
        "egress": [],
    }
)
class LeafFixture:
    pass


@operator_ports(
    {
        "ingress": [],
        "egress": [
            {
                "name": "out",
                "serialization_format": _PayloadType.PROTO,
                "type_url": "some.Type",
            },
        ],
    }
)
class SourceFixture:
    pass


class MissingOperatorPortsFixture:
    pass


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


class ExtractPortsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    def _extractorPath(self) -> str:
        runfiles = _createRunfiles()
        return _resolveRunfilePath(
            runfiles=runfiles,
            candidateRunfilesPaths=(
                "_main/bazel/lore/extract_ports",
                "bazel/lore/extract_ports",
            ),
        )

    def _sourcePath(self) -> str:
        return str(Path(__file__).resolve())

    def _extractorEnv(self) -> dict[str, str]:
        env = dict(os.environ)
        repoRoot = str(Path(__file__).resolve().parents[3])
        pythonPath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{repoRoot}:{pythonPath}" if pythonPath else repoRoot
        return env

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
        LOG.info("running extractor for fixture %s", className)
        LOG.info("command: %s", " ".join(command))
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=self._extractorEnv(),
            check=False,
        )
        LOG.info("return code for %s: %s", className, result.returncode)
        if result.stdout:
            LOG.info("stdout for %s:\n%s", className, result.stdout.rstrip())
        if result.stderr:
            LOG.info("stderr for %s:\n%s", className, result.stderr.rstrip())
        return result

    def _resultMessage(
        self,
        *,
        className: str,
        result: subprocess.CompletedProcess[str],
    ) -> str:
        return (
            f"class={className}\n"
            f"returncode={result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

    def _extractManifest(self, *, className: str) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as tempDir:
            outputPath = os.path.join(tempDir, "ports.json")
            result = self._runExtractor(
                className=className,
                outputPath=outputPath,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=self._resultMessage(className=className, result=result),
            )
            with open(outputPath, encoding="utf-8") as outputFile:
                manifest = json.load(outputFile)
            LOG.info("manifest for %s:\n%s", className, json.dumps(manifest, indent=2))
            return manifest

    def test_regularPortsProduceExpectedManifest(self):
        manifest = self._extractManifest(className="RegularPortsFixture")

        self.assertEqual(
            manifest["ingress"],
            [
                {
                    "name": "chunk",
                    "serialization_format": "PROTO",
                    "type_url": "type.googleapis.com/pkg.ChunkRequest",
                },
            ],
        )
        self.assertEqual(
            manifest["egress"],
            [
                {
                    "name": "store",
                    "serialization_format": "PROTO",
                    "type_url": "type.googleapis.com/pkg.StoreRequest",
                },
            ],
        )
        self.assertEqual(manifest["schema_version"], 1)

    def test_leafOperatorProducesEmptyIngressAndEgress(self):
        manifest = self._extractManifest(className="LeafFixture")

        self.assertEqual(manifest["ingress"], [])
        self.assertEqual(manifest["egress"], [])

    def test_sourceOperatorProducesEmptyIngress(self):
        manifest = self._extractManifest(className="SourceFixture")

        self.assertEqual(manifest["ingress"], [])
        self.assertEqual(
            manifest["egress"],
            [
                {
                    "name": "out",
                    "serialization_format": "PROTO",
                    "type_url": "type.googleapis.com/some.Type",
                },
            ],
        )

    def test_missingClassExitsNonZero(self):
        with tempfile.TemporaryDirectory() as tempDir:
            result = self._runExtractor(
                className="NonExistentClass",
                outputPath=os.path.join(tempDir, "ports.json"),
            )

        self.assertEqual(
            result.returncode,
            1,
            msg=self._resultMessage(className="NonExistentClass", result=result),
        )
        self.assertIn(
            "not found",
            result.stderr,
            msg=self._resultMessage(className="NonExistentClass", result=result),
        )

    def test_missingOperatorPortsExitsNonZero(self):
        with tempfile.TemporaryDirectory() as tempDir:
            result = self._runExtractor(
                className="MissingOperatorPortsFixture",
                outputPath=os.path.join(tempDir, "ports.json"),
            )

        self.assertEqual(
            result.returncode,
            1,
            msg=self._resultMessage(className="MissingOperatorPortsFixture", result=result),
        )
        self.assertIn(
            "@operator_ports",
            result.stderr,
            msg=self._resultMessage(className="MissingOperatorPortsFixture", result=result),
        )


if __name__ == "__main__":
    unittest.main()
