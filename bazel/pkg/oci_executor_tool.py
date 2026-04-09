import argparse
import os
import stat
import sys
from pathlib import Path


def _writeExecutableScript(out_path: Path, image_name: str, digest: str) -> None:
    """Write a self-contained bash script that runs the image by tag (digest sans 'sha256:')."""
    tag = digest.split(":", 1)[-1]  # strip 'sha256:' if present

    script = f"""#!/usr/bin/env bash
set -euo pipefail

# Optional override: export DOCKER_BIN=podman
DOCKER_BIN="${{DOCKER_BIN:-docker}}"

IMAGE_NAME="{image_name}"
TAG="{tag}"

exec "$DOCKER_BIN" run --pull=never "${{IMAGE_NAME}}:${{TAG}}" "$@"
"""
    out_path.write_text(script, encoding="utf-8")
    mode = out_path.stat().st_mode
    out_path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate an executable script that runs an OCI image by digest."
    )
    parser.add_argument("--image_name", required=True, help='e.g. "localhost/app:dev"')
    parser.add_argument("--digest_file", required=True, help='Path to file with "sha256:..."')
    parser.add_argument("--out", required=True, help="Path to output executable script")

    args = parser.parse_args()
    digest = Path(args.digest_file).read_text(encoding="utf-8").strip()
    if not digest.startswith("sha256:"):
        print(f"ERROR: digest did not look like sha256:... (got: {digest[:32]}...)", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    _writeExecutableScript(out, args.image_name, digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
