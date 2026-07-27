#!/usr/bin/env sh

IMAGE=${WASI_TEST_IMAGE:-urllib3:wasi-tests}
CONTAINER=${WASI_TEST_CONTAINER:-urllib3-wasi-tests}
TRAEFIK_STARTED=false

cleanup() {
    docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
    if [ "${TRAEFIK_STARTED}" = true ]; then
        docker compose stop >/dev/null 2>&1 || true
    fi
    rm -rf wasi-coverage/
}

trap cleanup EXIT INT TERM

TRAEFIK_HTTPBIN_ENABLE=true nox -s wasi_traefik || exit $?
TRAEFIK_STARTED=true

docker build -f WASI.Dockerfile -t "${IMAGE}" . || exit $?
docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true

docker run --network host --name "${CONTAINER}" "${IMAGE}"
status=$?

mkdir -p wasi-coverage
docker cp "${CONTAINER}:/app/.coverage.wasi" wasi-coverage/.coverage.wasi \
    2>/dev/null || true

if [ -f wasi-coverage/.coverage.wasi ]; then
    mv wasi-coverage/.coverage.wasi .coverage.wasi
fi

exit "${status}"
