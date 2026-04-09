#!/bin/bash
set -euo pipefail

if [[ "$#" -lt 3 ]]; then
  echo "Usage: $0 PYTOOL_TAR LOCKFILE INSTALL_DIR [--wheel WHEEL_FILE ...] [--additional-index URL ...]"
  exit 1
fi

PYTOOL_TAR="$1"
LOCKFILE="$(realpath "$2")"
INSTALL_DIR="$(realpath "$3")"
shift 3
WHEEL_FILES=()
ADDITIONAL_INDEXES=()

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --wheel)
      if [[ "$#" -lt 2 ]]; then
        echo "ERROR: --wheel requires a value"
        exit 1
      fi
      WHEEL_FILES+=("$2")
      shift 2
      ;;
    --additional-index)
      if [[ "$#" -lt 2 ]]; then
        echo "ERROR: --additional-index requires a value"
        exit 1
      fi
      ADDITIONAL_INDEXES+=("$2")
      shift 2
      ;;
    *)
      # Backward compatibility with older positional wheel-file invocation.
      WHEEL_FILES+=("$1")
      shift
      ;;
  esac
done

# Sanity check for install dir
if [[ ! -d "${INSTALL_DIR}" ]]; then
  echo "ERROR: INSTALL_DIR '${INSTALL_DIR}' does not exist or is not a directory"
  exit 1
fi

TMPDIR="$(mktemp -d "${PWD}/tmp.XXXXXX")"
trap "rm -rf ${TMPDIR}" EXIT

# Unpack the local package bundle
tar -C "${TMPDIR}" -xf "${PYTOOL_TAR}"

# Validate expected files
[[ -f "${TMPDIR}/pytool/pytool" ]] || { echo "ERROR: pytool/pytool not found in bundle"; exit 1; }

# Collect any local wheels into a find-links directory
FIND_LINKS_ARGS=()
if [[ "${#WHEEL_FILES[@]}" -gt 0 ]]; then
  WHEELS_DIR="${TMPDIR}/wheels"
  mkdir -p "${WHEELS_DIR}"
  for whl in "${WHEEL_FILES[@]}"; do
    cp -L "${whl}" "${WHEELS_DIR}/"
  done
  FIND_LINKS_ARGS=(--find-links "${WHEELS_DIR}")
fi

INDEX_ARGS=()
if [[ "${#ADDITIONAL_INDEXES[@]}" -gt 0 ]]; then
  # Resolution order:
  # 1) local wheels (find-links)
  # 2) additional indexes in caller-specified order
  # 3) PyPI fallback
  INDEX_ARGS+=(--index-url "${ADDITIONAL_INDEXES[0]}")
  for (( i=1; i<${#ADDITIONAL_INDEXES[@]}; i++ )); do
    INDEX_ARGS+=(--extra-index-url "${ADDITIONAL_INDEXES[$i]}")
  done
  INDEX_ARGS+=(--extra-index-url "https://pypi.org/simple")
fi

PIP_ARGS=(
  --no-deps
  --no-compile
  --upgrade
  --no-cache-dir
  --target "${INSTALL_DIR}"
)
if [[ "${#FIND_LINKS_ARGS[@]}" -gt 0 ]]; then
  PIP_ARGS+=("${FIND_LINKS_ARGS[@]}")
fi
if [[ "${#INDEX_ARGS[@]}" -gt 0 ]]; then
  PIP_ARGS+=("${INDEX_ARGS[@]}")
fi
PIP_ARGS+=(-r "${LOCKFILE}")

pushd "${TMPDIR}/pytool/pytool.runfiles/_main"
"../../pytool" pip install "${PIP_ARGS[@]}"
popd
