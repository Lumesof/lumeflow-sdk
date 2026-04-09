#!/usr/bin/env bash
set -euo pipefail

# Installs the Lumeflow local runtime smoketest and dev env packages from the
# public SDK Debian repo.
# Intended to run directly on a fresh VM.

SDK_PROJECT="${SDK_PROJECT:-lumesof-sdk-infra}"
SDK_LOCATION="${SDK_LOCATION:-us-central1}"
SDK_DEBIAN_REPO="${SDK_DEBIAN_REPO:-sdk-debian-registry}"
PACKAGE_NAME="${PACKAGE_NAME:-lumeflow-local-smoketest}"
DEV_ENV_PACKAGE_NAME="${DEV_ENV_PACKAGE_NAME:-lumesof-dev-env}"
ASSUME_YES=0

printUsage() {
  cat <<USAGE
Usage: $(basename "$0") [options]

Install ${PACKAGE_NAME}, ${DEV_ENV_PACKAGE_NAME}, and their dependencies on this machine.

Options:
  --yes                 Non-interactive apt mode.
  --sdk-project <id>    SDK project (default: ${SDK_PROJECT})
  --location <region>   Artifact Registry location (default: ${SDK_LOCATION})
  --repo <name>         Debian repo name (default: ${SDK_DEBIAN_REPO})
  --package <name>      Package to install (default: ${PACKAGE_NAME})
  --dev-env-package <name>
                        Dev env package to install (default: ${DEV_ENV_PACKAGE_NAME})
  -h, --help            Show this help

Environment overrides:
  SDK_PROJECT, SDK_LOCATION, SDK_DEBIAN_REPO, PACKAGE_NAME, DEV_ENV_PACKAGE_NAME
USAGE
}

requireCmd() {
  local cmd="$1"
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "ERROR: Required command not found: ${cmd}" >&2
    exit 1
  fi
}

aptInstall() {
  local flags=()
  if [[ "${ASSUME_YES}" -eq 1 ]]; then
    flags=(-y)
  fi
  sudo apt-get install "${flags[@]}" "$@"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes)
      ASSUME_YES=1
      shift
      ;;
    --sdk-project)
      SDK_PROJECT="$2"
      shift 2
      ;;
    --location)
      SDK_LOCATION="$2"
      shift 2
      ;;
    --repo)
      SDK_DEBIAN_REPO="$2"
      shift 2
      ;;
    --package)
      PACKAGE_NAME="$2"
      shift 2
      ;;
    --dev-env-package)
      DEV_ENV_PACKAGE_NAME="$2"
      shift 2
      ;;
    -h|--help)
      printUsage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown argument: $1" >&2
      printUsage
      exit 1
      ;;
  esac
done

requireCmd apt-get
requireCmd curl
requireCmd gpg
requireCmd tee

if [[ ! -f /etc/os-release ]]; then
  echo "ERROR: /etc/os-release not found; unsupported OS." >&2
  exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release

if [[ "${ID:-}" != "ubuntu" ]]; then
  echo "ERROR: This installer supports Ubuntu only. Found ID='${ID:-unknown}'." >&2
  exit 1
fi

if [[ "${VERSION_CODENAME:-}" != "noble" && "${VERSION_CODENAME:-}" != "jammy" ]]; then
  echo "ERROR: This package set currently targets Ubuntu noble (24.04) and jammy (22.04)." >&2
  echo "Found VERSION_CODENAME='${VERSION_CODENAME:-unknown}'." >&2
  exit 1
fi

if [[ "$(dpkg --print-architecture)" != "amd64" ]]; then
  echo "ERROR: This package set currently supports amd64 only." >&2
  exit 1
fi

echo "Configuring apt repositories..."

# Docker CE apt repo (required by lumeflow-local-runtime dependency on docker-ce)
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

# Public Artifact Registry apt repo.
curl -fsSL "https://${SDK_LOCATION}-apt.pkg.dev/doc/repo-signing-key.gpg" \
  | sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/artifact-registry.gpg
echo "deb [signed-by=/etc/apt/trusted.gpg.d/artifact-registry.gpg] https://${SDK_LOCATION}-apt.pkg.dev/projects/${SDK_PROJECT} ${SDK_DEBIAN_REPO} main" \
  | sudo tee /etc/apt/sources.list.d/lumesof-sdk.list >/dev/null

echo "Refreshing apt metadata..."
sudo apt-get update

echo "Installing ${PACKAGE_NAME} and ${DEV_ENV_PACKAGE_NAME}..."
if ! aptInstall "${PACKAGE_NAME}" "${DEV_ENV_PACKAGE_NAME}"; then
  echo "First install attempt failed; trying to repair and retry..."
  aptInstall -f
  aptInstall "${PACKAGE_NAME}" "${DEV_ENV_PACKAGE_NAME}"
fi

if id -nG "${USER}" | tr ' ' '\n' | grep -qx docker; then
  echo "User ${USER} is already in docker group."
else
  echo "Adding ${USER} to docker group..."
  sudo usermod -aG docker "${USER}"
  echo "IMPORTANT: log out and log back in (or restart your shell session) before running smoketest."
fi

echo
echo "Install complete."
echo "Run smoketest in order:"
echo "  lumeflow-smoketest --start-cluster"
echo "  lumeflow-smoketest --run-even-odd-dag"
echo "  lumeflow-smoketest --teardown-cluster"
