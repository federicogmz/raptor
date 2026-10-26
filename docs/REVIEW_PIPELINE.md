# RAPTOR Backend and Pipeline Architecture Review — Efficiency and Speed

Date: 2026-08-12. Scope: `webapp/main.py`, `core/runner.py`, `core/scan.py`,
`docker/entrypoint.sh`, `docker/setup-data*.sh`, `Makefile`, `Dockerfile`, and
all post-processing modules in `scripts/*.py`.

## Executive Verdict

The pipeline is **highly optimized**: all high-impact throughput and concurrency
optimizations are implemented and verified against real operational flights.

## Implemented Core Optimizations

| Subsystem | Mechanism |
|---|---|
| Multi-Sensor Scheduler | 4 independent sensor pipelines dispatched in parallel with a FIFO semaphore (RGB, Thermal, Multispectral, D-band), asynchronous staging per sensor, and shared memory budgeting (`odm_slots` in `scripts/hardware.py` + 45 MP full-frame guard). |
| Tiered Reconstruction | **Level 0** Ray-DEM thermal triage (<2 min), **Level 1** direct 2.5D ray-casting via native Rust `Zenith` (15–120 s), and **Level 2** 3D OpenDroneMap/OpenMVS (`--use-hybrid-bundle-adjustment`, `--matcher-neighbors 8`, dynamic `--min-num-features` per preset). |
| Staging & I/O | Parallel batch copy via `xargs -P` (memory-aware), hardlinks in multispectral staging, batched `exiftool` via `-@ -`, and `cp --reflink=auto` copy-on-write on supported filesystems. |
| Radiometric Thermal | Parallel DJI Thermal SDK (`dji_irp`) decoding with cache skip, homologated SIATA environmental parameters (`core/thermal_params.py`), vectorized NumPy filtering, and multi-threaded median estimation. |
| GIS Post-Processing | Parallel Cloud Optimized GeoTIFF (COG) generation with dtype-aware predictors, strip-based 8-bit tile conversion (`to_8bit` in 2048-row strips to prevent RAM spikes on 50k×41k rasters), parallel COPC point clouds, and incremental tile pyramid caching. |
| Webapp & Geovisor | Chunked uploads offloaded via `run_in_threadpool`, zero-copy symlink local imports, `mtime`-cached HTML serving, gzip compression, immutable tile caching, pre-flight validation, and SSE live telemetry streaming. |

## Stage Runtime Model (`scripts/hardware.py`)

Measured per-photo coefficients across operational flights:

- **Feature extraction / matching / undistort**: ~13 s — parallelized across CPU cores.
- **Incremental Bundle Adjustment** (`sfm=incremental`): ~12.4 s — sequential by design in 3D SfM over steep terrain (bypassed in Level 1 2.5D mode via `Zenith`).
- **DensifyPointCloud (OpenMVS)**: ~13.7 s on reference GPU — executed in `cartografico` and `forense` 3D presets; scales with `pc_quality` and available VRAM.
- **Mesh + Texturing**: ~5.1 s — parallelized.
- **Thermal SDK preparation**: `dji_irp` conversion ~6–12 s/photo (parallelized across CPU cores).
- **Tiles + Cropping + Export**: proportional to final raster dimensions (bounded by preset `res_cm` ceiling).

## Telemetry and Benchmarking Logs

Each mission run records structured telemetry in `outputs/logs/`:
- `outputs/logs/timings.json` — wall-clock duration per pipeline stage.
- `outputs/logs/odm_<sensor>_substages.json` — sub-stage breakdown (features, bundle adjustment, MVS).
- `outputs/run_summary.json` — mission execution summary and exported deliverables.
