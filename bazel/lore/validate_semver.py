from __future__ import annotations

import argparse
import logging
import re

LOG = logging.getLogger(__name__)
_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    args = parser.parse_args()

    if not _SEMVER_RE.fullmatch(args.version):
        LOG.error("invalid semver: %s", args.version)
        raise SystemExit(1)

    LOG.info("validated semver: %s", args.version)


if __name__ == "__main__":
    main()
