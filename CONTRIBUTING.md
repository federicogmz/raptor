# Contributing to RAPTOR

Thank you for your interest in contributing to **RAPTOR**! This guide outlines how to set up your development environment, run automated tests, understand the architecture, and submit contributions.

---

## Code of Conduct & Open Source Ethos

RAPTOR is developed as an open photogrammetry and remote sensing pipeline designed for field deployment, emergency response, and scientific analysis. We welcome contributions that improve reconstruction quality, processing speed, geometric accuracy, and user experience.

---

## Development Setup

The entire pipeline runs inside a single, self-contained Docker container (`raptor:latest`) based on `opendronemap/odm:gpu`. You do not need to install local GDAL, OpenSfM, or OpenMVS dependencies on your host machine.

### Requirements

* Linux (Ubuntu 22.04+ or Debian 12 recommended)
* Docker 24.0+
* NVIDIA Container Toolkit (optional, for GPU acceleration; CPU-only execution is supported)

### Building the Image

Build the container image using the CLI helper or standard Docker:

```bash
# Using the raptor launcher
./raptor build

# Or using docker directly
docker build -t raptor:latest .
```

---

## Running Tests

All tests run inside the Docker container to ensure consistent GDAL/NumPy ABI and geospatial environments.

Use the provided `./run_tests.sh` runner:

```bash
# Run the entire test suite
./run_tests.sh

# Run a specific test module
./run_tests.sh tests/test_presets.py

# Run tests with custom filter or verbosity
./run_tests.sh -k "odm_slots" -v
```

`run_tests.sh` mounts the checkout read-only and runs pytest on a copy inside
a throwaway container. Never run the suite inside the persistent `raptor-web`
container: the webapp tests that reach a successful `/start` launch the real
`docker/entrypoint.sh` and repoint `/app/{processing,outputs,...}`. To deploy
code changes to the persistent webapp, rebuild the image and redeploy
(`./raptor build && raptor restart`).

> **Note on Golden Characterization Tests:**
> `tests/test_trim_characterization.py` protects against unintended pixel shifts in orthomosaic boundary trimming against `tests/golden/trim_masks.json`. If an intentional change alters the boundary geometry, regenerate the golden mask by removing that file and re-running the test.

---

## Project Structure & Architecture

```
raptor/
├── core/                # Mission state management and hardware scan
├── docker/              # Container entrypoint, image staging, setup scripts
├── docs/                # Architecture docs (PIPELINE.md, REVIEW_PIPELINE.md)
├── geovisor/            # Lightweight offline Leaflet web map viewer
├── scripts/             # Core processing, calibration, indices, and export scripts
│   └── README.md        # Detailed catalog of all processing scripts
├── tests/               # Pytest suite exercising pipeline stages, webapp, and algorithms
├── webapp/              # FastAPI application server and modern UI
├── raptor               # Host CLI launcher (install, start, run, webapp, build)
└── run_tests.sh         # Containerized test runner
```

---

## Coding Guidelines

1. **Python (`scripts/`, `core/`, `webapp/`):**
   * Target Python 3.12 with strict exception handling for geospatial libraries (`gdal.UseExceptions()`, `ogr.UseExceptions()`).
   * Preserve NumPy 1.x ABI compatibility (`numpy==1.26.4` as specified in `requirements.txt`).
   * For concurrent raster access, prefer `scripts.gdal_open_retry.gdal_open_retry` over bare `gdal.Open` to prevent transient file locks.

2. **Shell Scripts (`docker/*.sh`, `raptor`, `run_tests.sh`):**
   * Always use `set -euo pipefail`.
   * Use defensive parameter expansion (e.g., `${VARIABLE:-default}`) to avoid unbound variable aborts under `set -u`.
   * Keep scripts idempotent and re-entrant wherever possible.

3. **Frontend (`geovisor/`, `webapp/static/`):**
   * Keep web map assets 100% offline-compatible. Do not add external CDN dependencies (Leaflet and Turf.js are vendored in `geovisor/vendor/`).
   * Follow clean, responsive layout design.

---

## Pull Request Checklist

Before submitting a Pull Request, please ensure:

1. [ ] All tests pass cleanly (`./run_tests.sh`).
2. [ ] No temporary files, debug logs, or scratch scripts are committed.
3. [ ] Any new CLI option or script is documented in `README.md`, `docs/PIPELINE.md`, or `scripts/README.md`.
4. [ ] Commits are descriptive and atomic.
