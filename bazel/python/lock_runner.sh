#!/bin/bash

# Helper function that copies files according to the manifest and generates the
# root requirements.txt file
process_manifest() {
  local tempdir="$1"
  local manifest="$2"

  local reqfile="$(mktemp "${tempdir}/requirements.XXXXXX.txt")"

  jq -c '.[]' "${manifest}" | while read -r entry; do
    local shortpath=$(echo "${entry}" | jq -r '.short_path')
    local fullpath=$(echo "${entry}" | jq -r '.path')
    local target="${tempdir}/${shortpath}"

    mkdir -p "$(dirname "${target}")"
    cp -L "${fullpath}" "${target}"

    echo "-r ${shortpath}" >> "${reqfile}"
  done
  echo ${reqfile}
}


set -euo pipefail

if [[ "$#" -lt 3 ]]; then
  echo "Usage: $0 LOCAL_PKG LOCKFILE MANIFEST [--wheel WHEEL_FILE ...] [--additional-index URL ...]"
  exit 1
fi

PYTOOL_TAR="$1"
LOCKFILE="$(realpath "$2")"
MANIFEST="$3"
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

TMPDIR="$(mktemp -d "${PWD}/tmp.XXXXXX")"
trap 'rm -rf "${TMPDIR}"' EXIT

# Unpack pytool bundle
tar -C "${TMPDIR}" -xf "${PYTOOL_TAR}"
[ -f "${TMPDIR}/pytool/pytool" ] || { echo "Missing pytool/pytool in the provided tarball"; exit 1; }

# Copy all the files into TMPDIR per the manifest
TMPDIR2="$(mktemp -d "${TMPDIR}/tmp.XXXXXX")"
REQFILE="$(process_manifest "${TMPDIR2}" "${MANIFEST}")"

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

COMPILE_ARGS=(
  --no-config
  --no-annotate
  --no-header
  --allow-unsafe
  --generate-hashes
  --output-file "${LOCKFILE}"
)
if [[ "${#FIND_LINKS_ARGS[@]}" -gt 0 ]]; then
  COMPILE_ARGS+=("${FIND_LINKS_ARGS[@]}")
fi
if [[ "${#INDEX_ARGS[@]}" -gt 0 ]]; then
  COMPILE_ARGS+=("${INDEX_ARGS[@]}")
fi
COMPILE_ARGS+=("${REQFILE}")

pushd "${TMPDIR}/pytool/pytool.runfiles/_main"
"../../pytool" piptools compile "${COMPILE_ARGS[@]}"
popd

# Strip --find-links lines that pip-compile injects into the output; they
# contain absolute sandbox paths that are not portable across machines.
# We re-supply --find-links at install time from the Bazel action arguments.
sed -i '/^--find-links/d' "${LOCKFILE}"
sed -i '/^--index-url/d' "${LOCKFILE}"
sed -i '/^--extra-index-url/d' "${LOCKFILE}"
# Remove blank lines that may be left after stripping
sed -i '/^[[:space:]]*$/d' "${LOCKFILE}"
