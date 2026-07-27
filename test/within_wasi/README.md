# WASI integration tests

This directory contains the componentized sync and async integration suite.
Run it through the single-container CI wrapper:

```bash
./ci/run_wasi.sh
```

The host runner reuses the repository's Compose Traefik stack, builds Preview 2,
Preview 3, minimal-import, and combined P2/P3 components, executes every case in
an isolated Wasmtime process, and writes `.coverage.wasi` at the repository root.
