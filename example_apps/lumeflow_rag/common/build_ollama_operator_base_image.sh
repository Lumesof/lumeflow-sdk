#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

IMAGE_REPO="us-central1-docker.pkg.dev/lumesof-infra-dev/dev-docker-registry/lumesof-ollama-operator-base"
IMAGE_TAG=""
PLATFORM="linux/amd64"
MODELS="qwen2.5:3b"

if [[ -n "${DOCKER_CONFIG:-}" ]]; then
    if [[ ! -d "${DOCKER_CONFIG}" || ! -w "${DOCKER_CONFIG}" ]]; then
        FALLBACK_DOCKER_CONFIG="${HOME}/.docker"
        mkdir -p "${FALLBACK_DOCKER_CONFIG}"
        if [[ -f "${DOCKER_CONFIG}/config.json" && ! -f "${FALLBACK_DOCKER_CONFIG}/config.json" ]]; then
            cp "${DOCKER_CONFIG}/config.json" "${FALLBACK_DOCKER_CONFIG}/config.json"
        fi
        export DOCKER_CONFIG="${FALLBACK_DOCKER_CONFIG}"
        echo "==> DOCKER_CONFIG was not writable; using ${DOCKER_CONFIG}" >&2
    fi
fi

for arg in "$@"; do
    case "${arg}" in
        --image-repo=*)
            IMAGE_REPO="${arg#*=}"
            ;;
        --image-tag=*)
            IMAGE_TAG="${arg#*=}"
            ;;
        --platform=*)
            PLATFORM="${arg#*=}"
            ;;
        --model=*)
            MODELS="${arg#*=}"
            ;;
        --models=*)
            MODELS="${arg#*=}"
            ;;
        *)
            echo "Unknown argument: ${arg}" >&2
            echo "Usage: $0 [--image-repo=...] [--image-tag=...] [--platform=...] [--model=<model>] [--models='model1 model2']" >&2
            exit 2
            ;;
    esac
done

NORMALIZED_MODELS="$(echo "${MODELS}" | tr ',' ' ' | xargs)"
if [[ -z "${NORMALIZED_MODELS}" ]]; then
    echo "ERROR: at least one model must be provided." >&2
    exit 2
fi

read -r -a MODEL_ARRAY <<< "${NORMALIZED_MODELS}"
MODEL_TAG=""
for model_name in "${MODEL_ARRAY[@]}"; do
    current_tag="$(
        echo "${model_name}" \
            | tr '[:upper:]' '[:lower:]' \
            | sed -E 's/[^a-z0-9._-]+/-/g; s/^-+//; s/-+$//'
    )"
    if [[ -z "${current_tag}" ]]; then
        echo "ERROR: unable to derive model tag from model '${model_name}'" >&2
        exit 2
    fi
    if [[ -z "${MODEL_TAG}" ]]; then
        MODEL_TAG="${current_tag}"
    else
        MODEL_TAG="${MODEL_TAG}-${current_tag}"
    fi
done

if [[ -z "${MODEL_TAG}" ]]; then
    echo "ERROR: unable to derive model tag from models '${NORMALIZED_MODELS}'" >&2
    exit 2
fi

MODEL_REPO_SUFFIX="-${MODEL_TAG}"
if [[ "${IMAGE_REPO}" == *"${MODEL_REPO_SUFFIX}" ]]; then
    IMAGE_REPO_WITH_MODEL="${IMAGE_REPO}"
else
    IMAGE_REPO_WITH_MODEL="${IMAGE_REPO}${MODEL_REPO_SUFFIX}"
fi

if [[ -z "${IMAGE_TAG}" ]]; then
    IMAGE_TAG="latest"
fi

IMAGE_REF="${IMAGE_REPO_WITH_MODEL}:${IMAGE_TAG}"

echo "==> Building ${IMAGE_REF}"
docker build \
    --platform "${PLATFORM}" \
    -f "${SCRIPT_DIR}/Dockerfile.ollama_operator_base" \
    --build-arg "OLLAMA_PRELOAD_MODELS=${NORMALIZED_MODELS}" \
    -t "${IMAGE_REF}" \
    "${SCRIPT_DIR}"

echo "==> Pushing ${IMAGE_REF}"
docker push "${IMAGE_REF}"

DIGEST="$(docker inspect --format='{{index .RepoDigests 0}}' "${IMAGE_REF}")"
echo "==> Published ${DIGEST}"
