FROM debian:bookworm-slim

WORKDIR /app

# Install curl for downloading uv
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# Install a standalone Python via uv (statically-linked _ssl)
RUN uv python install 3.12

# Verify _ssl is indeed statically linked (no __file__)
RUN uv run --python 3.12 -- python -c "\
import ssl; \
assert not hasattr(ssl._ssl, '__file__'), \
    f'_ssl has __file__={ssl._ssl.__file__!r}; not statically linked'; \
print('OK: _ssl is statically linked (no __file__)')"

# Create a virtual environment with the uv-managed Python
RUN uv venv --python 3.12 /app/.venv
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml LICENSE.txt README.md hatch_build.py dev-requirements.txt urllib3_future.pth ./
COPY ./src/urllib3 src/urllib3/
COPY ./test test/
COPY ./dummyserver dummyserver/

# Install the package and test dependencies
RUN uv pip install -r dev-requirements.txt
RUN uv pip install ".[socks,brotli,zstd]"

ENV TRAEFIK_HTTPBIN_ENABLE=false
ENV CI=true
ENV TERM=xterm-256color

CMD ["python", "-m", "pytest", "-v", "--cov=urllib3", "--cov-report=", \
    "test/with_dummyserver/test_https.py::TestHTTPS_TLSv1_3::test_in_memory_client_intermediate", \
    "test/with_dummyserver/test_https.py::TestHTTPS_TLSv1_3::test_in_memory_client_key_password", \
    "test/with_dummyserver/test_https.py::TestHTTPS_TLSv1_3::test_in_memory_client_key_password_ctypes_only", \
    "test/with_dummyserver/test_https.py::TestHTTPS_TLSv1_3::test_in_memory_client_key_password_shm_only", \
    "test/with_dummyserver/test_https.py::TestHTTPS_TLSv1_3::test_in_memory_client_key_password_fifo_only"]
