# syntax=docker/dockerfile:1.7

FROM python:3.14-slim-bookworm

ARG TARGETARCH
ARG WASMTIME_VERSION=47.0.1

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    COMPONENTIZE_WIT=/opt/componentize-py/wit \
    PYTHONPATH=/app/src:/app/test:/app

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl tini xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN set -eu; \
    case "${TARGETARCH}" in \
        amd64) archive_arch=x86_64; checksum=955059f7f2ff399338a01558c49a3df830eedfb76b837490dfaa479b4ff5d7ee ;; \
        arm64) archive_arch=aarch64; checksum=0b81ce9a54b808d3f9efd5485ca2ed216d0f26802442c17136395915db503fa5 ;; \
        *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    archive="wasmtime-v${WASMTIME_VERSION}-${archive_arch}-linux.tar.xz"; \
    curl --fail --location --retry 5 \
        --output "/tmp/${archive}" \
        "https://github.com/bytecodealliance/wasmtime/releases/download/v${WASMTIME_VERSION}/${archive}"; \
    printf '%s  %s\n' "${checksum}" "/tmp/${archive}" | sha256sum --check; \
    tar -xJf "/tmp/${archive}" -C /tmp; \
    mv "/tmp/wasmtime-v${WASMTIME_VERSION}-${archive_arch}-linux/wasmtime" /usr/local/bin/wasmtime; \
    rm -rf "/tmp/${archive}" "/tmp/wasmtime-v${WASMTIME_VERSION}-${archive_arch}-linux"

COPY wasi-requirements.txt /app/wasi-requirements.txt

RUN python -m pip install --no-cache-dir --require-hashes \
    --requirement /app/wasi-requirements.txt

RUN mkdir -p /opt/componentize-py \
    && curl --fail --location --retry 5 \
        "https://github.com/bytecodealliance/componentize-py/archive/refs/tags/v0.25.0.tar.gz" \
        | tar -xz --strip-components=1 -C /opt/componentize-py \
            componentize-py-0.25.0/wit

COPY . /app

RUN componentize-py --version \
    && wasmtime --version \
    && python -c "import rtls; print(f'rtls {rtls.__version__}')"

ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "within_wasi.run"]
