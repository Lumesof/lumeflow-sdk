from __future__ import annotations

import os
import subprocess
import unittest
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


class ValidateSemverTests(unittest.TestCase):
    def _validatorPath(self) -> str:
        runfiles = _createRunfiles()
        return _resolveRunfilePath(
            runfiles=runfiles,
            candidateRunfilesPaths=(
                "_main/bazel/lore/validate_semver",
                "bazel/lore/validate_semver",
            ),
        )

    def _runValidator(self, *, version: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self._validatorPath(), version],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_validVersionsExitZero(self):
        for version in ("0.0.0", "1.0.0", "10.20.300"):
            with self.subTest(version=version):
                result = self._runValidator(version=version)
                self.assertEqual(result.returncode, 0, msg=result.stderr)

    def test_invalidVersionsExitNonZero(self):
        for version in ("1.0", "1.0.0-alpha", "v1.0.0", "not-semver", ""):
            with self.subTest(version=version):
                result = self._runValidator(version=version)
                self.assertEqual(result.returncode, 1)
                self.assertIn("semver", result.stderr)


if __name__ == "__main__":
    unittest.main()
