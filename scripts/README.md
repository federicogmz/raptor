# RAPTOR Scripts Catalog

The processing steps of the pipeline. `docker/entrypoint.sh` orchestrates
them, mostly through `Makefile` targets; each one can also be run by hand
inside the container (see "Advanced use" in the top-level README).

---

## 1. Ingestion & Preparation
* **`prepare_rgb_odm.py`**: Copies the RGB photos (DJI M3T / H20T) into the ODM project and cleans their EXIF/XMP.
* **`prepare_multispectral_odm.py`**: Stages the 4 multispectral bands of the DJI M3M (Green, Red, RedEdge, NIR) and writes `geo.txt`.
* **`prepare_dband_odm.py`**: Stages the M3M's own RGB camera (D band) as an independent ODM project.
* **`odm_staging.py`**: Shared staging helpers (hardlinked `images/` + `geo.txt`) for the multispectral and D band projects.
* **`convert_thermal_tiff.py`**: R-JPEG → Float32 GeoTIFF in °C through the DJI Thermal SDK (`dji_irp`).
* **`denoise_thermal_frames.py`**: Bilateral filter on the °C frames before reconstruction.
* **`prepare_thermal_native_odm.py`**: Re-encodes °C → Kelvin×100 and tags the frames so ODM's native thermal branch handles them.
* **`camera_ns.exiftool.config`**: ExifTool namespace for the XMP `Camera:BandName` tag.
* **`subsample_photos.py`**: Keeps 1 of every N photos (urgency mode, `SUB_SAMPLE`).

---

## 2. Reconstruction Scheduling
* **`hardware.py`**: Preset table (`tactico`, `cartografico`, `forense` + legacy aliases), hardware detection, per-stage thread limits (`safe_concurrency`), memory-aware reconstruction slots (`odm_slots`) and the time/resolution estimate.
* **`odm_progress_filter.py`**: Turns raw ODM console output into progress bars, structured events and per-substage timings.

---

## 3. Post-Processing & Quality
* **`dsm_clean.py`**: Removes ODM's synthetic constant fill and vertical outliers from the DSM.
* **`fast_median.py`**: Threaded median filter used by `dsm_clean.py`.
* **`trim_low_overlap_edges.py`**: Trims every product (RGB, thermal, multispectral, D band, DSM) to the convex hull of the ground footprints of the photos that entered the SfM reconstruction.
* **`confidence_mask.py`**: Binary mask (on the thermal grid) of where both RGB and thermal have valid data.
* **`compute_coverage.py`**: Mosaic coverage vs. the area actually flown (`coverage.json`), with a warning below a threshold.
* **`compute_flight_quality.py`**: Survey quality: camera overlap, flight speed and % of photos reconstructed (`flight_quality.json`).
* **`gdal_open_retry.py`**: `gdal.Open()` with retries, for transient read failures under heavy concurrent I/O.

---

## 4. Multispectral & Thermal Analysis
* **`compute_vegetation_indices.py`**: NDVI, GNDVI, NDRE and MSAVI2 from the multispectral orthomosaic.
* **`classify_vegetation_indices.py`**: Classifies those indices with fixed literature cut-offs.
* **`compute_thermal_hotspot.py`**: Classifies the thermal orthomosaic by absolute temperature (<40, 40–60, 60–88, ≥88 °C).
* **`raster_classify.py`**: Shared `classify()` / `write_class_tif()` helpers.
* **`compute_situation_summary.py`**: `situation.json`: connected ≥88 °C hotspots (≥9 px) with centroid and peak temperature, plus thermal data coverage.

---

## 5. Export, Viewer & Delivery
* **`export_flight_path.py`**: Flight path and capture points (GeoJSON) from the photos' GPS/EXIF, before ODM runs.
* **`generate_tiles.py`**: XYZ tile pyramids for the offline Leaflet geovisor.
* **`export_cog.py`** / **`export_copc.py`**: Rasters → Cloud-Optimized GeoTIFF; point clouds → COPC.
* **`export_products.py`**: Delivery to `EXPORT_DIR`, with optional reprojection and vector format choice.
* **`export_report.py`**: One-page HTML emergency report (print to PDF from the browser).
* **`notify_alert.py`**: Writes `alert.json` when there are active hotspots and optionally POSTs it to `ALERT_WEBHOOK_URL`.
* **`run_summary.py`**: Machine-readable `run_summary.json` (products, GSD, CRS, coverage, exit code).

---

## 6. Utilities & Diagnostics
* **`check_deps.py`**: Verifies the dependencies inherited from the ODM base image (GDAL, pyproj, scipy).
* **`print_timings.py`**: Per-stage timing summary from `outputs/logs/timings.json`.
* **`progress.py` / `progress.sh`**: Progress bars and the structured progress channel consumed by the webapp.
* **`debug/thermal_diag.py`**: Thermal mosaic diagnostics.
