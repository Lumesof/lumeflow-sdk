from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Sequence

from python.runfiles import Runfiles


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


class BuildLabelsTests(unittest.TestCase):
    def _toolPath(self) -> str:
        runfiles = _createRunfiles()
        return _resolveRunfilePath(
            runfiles=runfiles,
            candidateRunfilesPaths=(
                "_main/bazel/lore/build_labels",
                "bazel/lore/build_labels",
            ),
        )

    def _runTool(self, *, portsJson: Path, output: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                self._toolPath(),
                "--ports-json",
                str(portsJson),
                "--output",
                str(output),
                "--publisher",
                "lumesof",
                "--slug",
                "fixture-operator",
                "--description",
                "Fixture operator",
                "--version",
                "1.2.3",
                "--category",
                "testing",
                "--visibility",
                "private",
                "--lumeflow-min-version",
                "",
                "--changelog",
                "",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_writesExpectedLabels(self):
        with tempfile.TemporaryDirectory() as tempDir:
            tempPath = Path(tempDir)
            portsJson = tempPath / "ports.json"
            portsJson.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "ingress": [
                            {
                                "name": "chunk",
                                "serialization_format": "PROTO",
                                "type_url": "type.googleapis.com/pkg.ChunkRequest",
                            },
                        ],
                        "egress": [
                            {
                                "name": "store",
                                "serialization_format": "PROTO",
                                "type_url": "type.googleapis.com/pkg.StoreRequest",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = tempPath / "labels.txt"

            result = self._runTool(portsJson=portsJson, output=output)

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "\n".join([
                    "lore.category=testing",
                    "lore.changelog=",
                    "lore.description=Fixture operator",
                    "lore.lumeflow.min_version=",
                    "lore.ports.egress=store:PROTO:type.googleapis.com/pkg.StoreRequest",
                    "lore.ports.ingress=chunk:PROTO:type.googleapis.com/pkg.ChunkRequest",
                    "lore.publisher=lumesof",
                    "lore.slug=fixture-operator",
                    "lore.version=1.2.3",
                    "lore.visibility=private",
                    "",
                ]),
            )

    def test_invalidPortsPayloadExitsNonZero(self):
        with tempfile.TemporaryDirectory() as tempDir:
            tempPath = Path(tempDir)
            portsJson = tempPath / "ports.json"
            portsJson.write_text(json.dumps({"ingress": "bad"}), encoding="utf-8")
            output = tempPath / "labels.txt"

            result = self._runTool(portsJson=portsJson, output=output)

            self.assertEqual(result.returncode, 1)
            self.assertIn("must be a list", result.stderr)


if __name__ == "__main__":
    unittest.main()
