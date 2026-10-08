# RAPTOR: Technical Documentation

Photogrammetric processing of DJI flights (RGB, thermal Zenmuse H20T/Mavic
3T, multispectral + D band Mavic 3M) to produce georeferenced
orthomosaics.

## Pipeline architecture

Orchestrated by `docker/entrypoint.sh` inside a single container
(`Dockerfile`, `FROM opendronemap/odm:gpu`). **ODM does ALL the 3D
reconstruction and orthophoto rendering for the four sensors** (RGB,
thermal, multispectral, D band). There's no custom heuristic blending; the
Python scripts prepare the input images, trim low-confidence edges, and
post-process (indices, thermal hotspot, tiles, export).

The four ODM projects (`rgb_odm`, `thermal_native_odm`,
`multispectral_odm`, `dband_odm`) are **independent**: different source
folder, different destination folder, no shared files. The pipeline takes
advantage of this on two levels:

1. **Each sensor prepares its images independently.** There's no global
   barrier waiting for all four to finish preparing before any of them can
   start reconstructing (thermal, which invokes the DJI SDK photo by
   photo, can take 20-40 min per 200 photos; RGB/MS/D band prepare in
   seconds to minutes).
2. **Each sensor's reconstruction starts as soon as ITS preparation is
   done**, competing for a memory-aware slot against the other sensors
   already ready (`odm_slots()`, see "Splitting the machine between ODM
   projects" below).

```
data/rgb_mosaico/       data/termica_mosaico/     data/multiespectral_mosaico/   data/dband_mosaico/
*_V.JPG, *_W.JPG         *_T.JPG                   *_MS_{G,R,RE,NIR}.TIF          *_D.JPG
       │                       │                          │                          │
       └──────────┬────────────┘                          └────────────┬─────────────┘
                  │  0. Organization (docker/setup-data*.sh, IN PARALLEL)
                  ▼
       1. Flight path (export_flight_path.py), before ODM: gives something
          real to look at in the geovisor from minute one
                  │
                  ▼
       2. Preparation PER SENSOR, each one independent:
            RGB: copy + exiftool (prepare-rgb)
            Thermal: DJI SDK R-JPEG→°C + denoise + re-encode Kelvin×100
            MS/D band: hardlink to images/ + geo.txt (scripts/odm_staging.py)
                  │
                  ▼
       3. Reconstruction SEMAPHORE (odm_slots(), memory-aware): each
          sensor enters as soon as ITS preparation is done, not when
          the others finish:
   ┌─────────────┬──────────────────┬────────────────────┬──────────────────┐
   │ ODM RGB     │ ODM native       │ ODM Multispectral  │ ODM D band       │
   │ (SfM+[MVS]  │ thermal (SfM+MVS │ (SfM+calibration   │ (SfM+fast ortho, │
   │ +DSM+ortho) │ +mesh+texture    │ +multiband ortho)  │ always           │
   │ processing/ │ +ortho in °C)    │ processing/        │ --fast-ortho     │
   │ rgb_odm/    │ processing/      │ multispectral_odm/ │ photo)           │
   │             │ thermal_native_  │                    │ processing/      │
   │             │ odm/             │                    │ dband_odm/       │
   └─────────────┴──────────────────┴────────────────────┴──────────────────┘
          │  4. As soon as EACH sensor finishes ITS reconstruction (it doesn't
          │     wait for the others): DSM cleanup (RGB or MS without RGB only)
          │     + low-overlap edge trim + partial tiles + COG/COPC export
          ▼                ▼                   ▼                   ▼
    dsm_clean.py   trim_low_overlap_   trim_low_overlap_   trim_low_overlap_
    (RGB/MS only)      edges.py            edges.py            edges.py
          │                │                   │                   │
          └────────────────┬───────────────────────────────────────┘
                           │  5. Cross-analysis, IN PARALLEL with each other, over what's
                           │     already trimmed (they don't read from or write to each other):
                           ▼
   ┌────────────────────┬───────────────────────────┬──────────────────────────────────┐
   │ confidence_mask.py │ compute_flight_quality.py │ compute_vegetation_indices.py +  │
   │ (RGB∩thermal)      │ (overlap, speed,          │ classify_vegetation_indices.py + │
   │                    │ % reconstructed)          │ compute_thermal_hotspot.py       │
   └────────────────────┴───────────────────────────┴──────────────────────────────────┘
              └───────────────────────┬───────────────────────────────┘
                                      ▼
             6. generate_tiles.py → geovisor/tiles/ → webapp/main.py :8080
                            ▼
             7. Final pass: export_cog.py + export_copc.py (idempotent,
                skips what each sensor already exported in step 4)
                            ▼
             8. export_products.py → EXPORT_DIR (optional)
```

## Stages in detail

### 0. Image organization (`docker/setup-data.sh`, `setup-data-multispectral.sh`)

They recursively search for each sensor's suffixes and copy to
`data/{rgb,termica,multiespectral,dband}_mosaico/`: `*_V.JPG`/`*_W.JPG`
(RGB), `*_T.JPG` (thermal) from `/input`; `*_MS_{G,R,RE,NIR}.TIF` and
`*_D.JPG` from `/input_ms`. The two scripts run **in parallel** (they read
from different source folders, write to different destinations, share
nothing). `setup-data-multispectral.sh` aborts with a clear message if any
of the 4 spectral bands is missing (ODM needs all 4 to group each
capture); D band is optional, its absence isn't an error. No metadata CSV
is extracted: the GPS/EXIF is read directly by the scripts that need it,
and ODM reads it from each photo's EXIF.

### 1. Flight path (`export_flight_path.py`)

Runs as soon as organization finishes, BEFORE touching ODM: it reads
GPS/time directly from the EXIF of `data/*_mosaico/` (already complete at
this point), and doesn't need to wait for each sensor's specific
preparation (which for thermal can take 20-40 min). It gives the geovisor
something real to show from the first minute, instead of an empty screen
for the whole reconstruction.

### 2. Image preparation (per sensor, independent)

- **RGB** (`prepare-rgb` target in the `Makefile`): copies the images to
  `processing/rgb_odm/images/` and re-tags them with `exiftool`,
  preserving full EXIF plus DJI's XMP. **`geo.txt` is deliberately not
  generated for RGB**: ODM reads the GPS from the EXIF and detects RTK
  precision on its own; see the target's extensive comment, which
  documents why `-exif:all` (and not `-gps:all`) and why `-api
  Compact=Shorthand` is needed.
- **Multispectral and D band** (`prepare_multispectral_odm.py`,
  `prepare_dband_odm.py`, both built on `scripts/odm_staging.py`):
  populate `images/` with **hardlinks** instead of copies (ODM only READS
  these files, and on large missions copying them duplicated several GB of
  writes for nothing) and generate `geo.txt` from the M3M's native GPS
  EXIF. If the hardlink can't be made (source and destination on different
  filesystems) it falls back to a normal copy without warning: same
  result, just slower.
- **Native thermal** (`prepare_thermal_native_odm.py`): re-encodes the
  Float32 °C TIFFs (from `convert_thermal_tiff.py`) to uint16 Kelvin×100
  and adds EXIF `Make=DJI`/`Model=ZH20T` plus XMP `Camera:BandName=LWIR`,
  the exact format and tags that ODM's `opendm/thermal.py` recognizes to
  apply its own native radiometric calibration (Kelvin→Celsius) during
  reconstruction. See `scripts/camera_ns.exiftool.config` (the XMP
  namespace needed to *write* `Camera:BandName`, not just read it).

**Shared preparation budget**: multispectral, thermal, and D band EACH
parallelize internally (copy+exiftool / `dji_irp` per image). If each one
requested its own memory-aware concurrency without knowing about the
others, running them all at once could add up to more threads than
available RAM can afford. `entrypoint.sh` computes a single budget
(`PREP_BUDGET`, calibrated against the heaviest profile, 5 MP) and splits
it between the streams that will actually run and parallelize internally
(`PREP_SPLIT`). Thermal is left out of this split because `dji_irp`
measures ~9 MB RSS per process, a completely different memory profile.

### 3. ODM reconstruction (memory-aware semaphore)

All quality parameters (`--pc-quality`, `--feature-quality`,
`--orthophoto-resolution`/`--dem-resolution`, `--min-num-features`,
`--matcher-neighbors`, `--sfm-algorithm`, bundle adjustment,
`--fast-orthophoto`) come from the chosen **preset** (`tactico` /
`cartografico` / `forense`; the legacy names `vistazo`, `rapido`,
`estandar`, `alta` and `maxima` are aliases) and the **terrain**
(`plano`/`escarpado`) via `scripts/hardware.py::preset()`, the single
source that the webapp and `./raptor run` also read to show the same
resolution/time message before starting. See "Quality, terrain, and
hardware" in the README for why each lever exists.

- **`sfm_algorithm`**: `incremental` in every preset. `planar` (which the
  former fast presets used by default) aligns photos through homographies between pairs, much faster than
  incremental reconstruction, but **only valid if the scene is actually
  flat** (nadir flight, fixed altitude). On terrain with strong relief,
  `planar` can silently discard most of the photos: in a rocky-terrain
  mission, `vistazo` with `planar` incorporated only 283 of 1199 RGB
  photos (23.6%) and 224 of 1199 thermal photos (18.7%) into the final
  reconstruction, with no error at all: the pipeline "finished fine" with
  a heavily trimmed orthomosaic. `incremental` reconstructs photo by photo
  through bundle adjustment, without assuming a plane. Confirmed on the same
  mission: 99.2% and 98.7% coverage respectively, at the cost of being
  considerably slower (same mission: thermal ~6h, RGB ~10h instead of
  minutes; incremental is sequential by design, not parallelizable across
  photos). `TERRENO=escarpado` still forces `incremental` on any preset
  that would use `planar`; with the current table that is none, so the
  flag has no effect and only survives for compatibility.

- **`--fast-orthophoto` on RGB** (`tactico`): skips
  `DensifyPointCloud` (MVS), the dense reconstruction stage, which on a
  ~1200-photo flight with a laptop GPU measured ~11h of the ~15h total for
  that reconstruction. The DSM then comes from the SPARSE SfM cloud
  instead of the dense one (poorer, more gaps). `cartografico` and `forense` keep
  the full dense cloud because RGB is the source of the mission's DSM. D
  band uses this flag ALWAYS (it never requests `--dsm`, there's no point
  paying the most expensive stage of the whole mission for a product that
  doesn't need it).

- **`run_odm()`** (in `entrypoint.sh`) splits each reconstruction into two
  ODM invocations with different concurrency levels: `--end-with opensfm`
  with LIGHT concurrency (all of SfM: detecting/matching features,
  reconstructing, undistorting; ODM downsizes photos to
  `feature_process_size` before touching them, so the per-thread footprint
  doesn't depend on native resolution) and `--rerun-from openmvs` with
  HEAVY concurrency (dense reconstruction, which if it runs, does depend
  on the sensor's native resolution). When the project uses
  `--fast-orthophoto` there's no dense stage to protect, so `run_odm()`
  detects this (it checks the arguments, not the sensor's name) and does
  **a single invocation** with light concurrency for the whole run.

- **ODM RGB**: SfM + [MVS except with `--fast-orthophoto`] + DSM +
  orthophoto.
- **ODM native thermal**: SfM + MVS + mesh + texture + orthophoto, the
  FULL pipeline just like RGB, not just SfM. `--radiometric-calibration
  camera` triggers ODM's native Kelvin×100→°C calibration (see
  `opendm/thermal.py::dn_to_temperature`, `DJI ZH20T` branch) during
  texture rendering, so the final orthophoto already comes out in real
  °C. The native render is used instead of a custom heuristic blend:
  against an Agisoft reference delivery on the same mission, a custom
  winner-take-all backward projection gives 16.9% coverage with
  fragmentation artifacts at the edges; ODM's native render gives 68.6%
  with no artifacts, at higher resolution.
- **ODM Multispectral**: SfM + calibration to reflectance (sun sensor, no
  physical panel) + multiband orthophoto. Only runs if `/input_ms` was
  mounted. If there's no RGB flight, its own DSM (also run with `--dsm`)
  becomes the source of the mission's surface model.
- **ODM D band**: SfM + fast orthophoto (`--fast-orthophoto` always, no
  `--dsm`). Only runs if `DBAND=1` and there are `*_D.JPG` files.

### Splitting the machine between ODM projects (`odm_slots()`)

The four projects are independent, so running them strictly in series
wastes the machine in a measurable way: one sensor's incremental
reconstruction can use ~1 core for hours while the others do nothing, and
the next reconstruction waits its turn with no real need to.

`scripts/hardware.py::odm_slots(mp_profiles)` decides, against REAL
available RAM (not total) and the cores, how many pending projects can
reconstruct at once: it tries the largest allowed N and lowers it until
EVERY project in the batch gets at least `MIN_HILOS_POR_PROYECTO` in both
the light and heavy phases. If not even two fit, it runs one at a time
(the old behavior, now a floor rather than a ceiling). Each sensor's
megapixel profile (`_odm_mp()` in `entrypoint.sh`) is the proxy for how
much RAM it needs at its heaviest stage: 12.3 MP for RGB (native photos,
what makes `DensifyPointCloud` heavy), unless that RGB uses
`--fast-orthophoto`. In that case its real heaviest stage is texturing
over the "undistorted" images (close to native resolution, but far from
MVS cost), so its profile drops by half. Without this adjustment,
`odm_slots()` kept reserving the whole machine for an RGB that no longer
paid the cost that profile assumed.

`RAPTOR_ODM_PARALELO=1` forces the old sequential mode. `MAX_CONCURRENCY`
is still the manual knob for threads per project (not for how many
projects run together; honoring it by launching several projects with
that value each would be the opposite of what it's asking for).

### The CUDA check

The dense reconstruction binary (`DensifyPointCloud`) is linked against
`libcuda.so.1` and doesn't even load without the NVIDIA runtime mounted.
The run would die at the `openmvs` stage, after having paid for the
preparation and the entire SfM. `entrypoint.sh` checks this in the first
second, but **only for sensors that will actually touch that stage**:
with `SKIP_ODM=1` there's no reconstruction at all, and neither D band
(`--fast-orthophoto` always) nor RGB in `tactico`
(`--fast-orthophoto` too) touches it. If those are the only active
sensors, the check doesn't block even if CUDA is missing.

### 4. Per-sensor post-processing (as soon as ITS reconstruction is done)

- **DSM cleanup** (`dsm_clean.py`): detects and removes ODM's synthetic
  fill (flat plateaus with ~0 local variance) and outliers. Fills real
  gaps (water, shadow) using the trend of the surrounding terrain. Runs on
  the RGB DSM, or the multispectral one if there's no RGB flight. Output:
  `outputs/dsm.tif`.
- **Edge trim** (`trim_low_overlap_edges.py`): `trim_rgb`/
  `trim_thermal_native`/`trim_multispectral`/`trim_dband` share the same
  generic logic. They project the ground footprint of every photo that
  entered the SfM reconstruction (EACH project's `reconstruction.json`)
  and keep ODM's own alpha intersected with the convex hull of those
  footprints (`_footprint_hull_mask`): a single photo covering a point is
  enough to keep it. Native thermal also does a point despike and trims
  physically implausible extremes.
- **Cloud-optimized export of THAT sensor** (`export_cog.py`,
  `export_copc.py`, called with explicit arguments): as soon as one
  sensor's post-processing finishes, its products already come out as
  COG/COPC, no need to wait for the other sensors to finish. It's
  deliberately idempotent: a final pass with no arguments (see stage 7)
  walks through all of `outputs/` again but skips whatever's already up
  to date.
- **`publish_partial()`**: geovisor tiles regenerated as soon as there's
  something new to show. Not a new stage in the progress bar (the user
  keeps seeing whatever phase is actually running), just an update to
  what's already there.

### 5. Cross-analysis (in parallel with each other)

They run in parallel because **they don't read from or write to each
other**: each one only reads already-trimmed orthomosaics/indices:

- **Confidence mask** (`confidence_mask.py`): reads
  `outputs/{rgb,thermal}_orthomosaic.tif`, generates
  `outputs/confidence_mask.tif` with the pixels reliable in both
  products. Requires RGB AND thermal (if either is missing, it doesn't
  run; `confidence_mask.py` would try to open a nonexistent file).
- **Survey quality** (`compute_flight_quality.py`): camera overlap,
  flight speed, % of images reconstructed. Independent of whether there's
  multispectral, it runs for any combination of sensors that includes
  thermal.
- **Vegetation indices** (`compute_vegetation_indices.py` +
  `classify_vegetation_indices.py`): NDVI/GNDVI/NDRE/MSAVI2 from the
  multispectral multiband orthophoto, if the mission carries one, plus
  its classification into discrete classes using standard remote-sensing
  breakpoints. Bands are identified by NAME (`GetDescription()`, which
  ODM writes from the XMP `Camera:BandName`), not by position.
- **Thermal hotspot** (`compute_thermal_hotspot.py`): ABSOLUTE
  temperature (not relative anomaly), for any mission with thermal. It
  doesn't depend on NDVI or multispectral. It's the pipeline's only
  hotspot source.
- **`compute_situation_summary.py`**: summarizes active hotspots, their
  real location, and data confidence in `outputs/situation.json`, for the
  geovisor's executive summary.

### 6. Tiles and geovisor

- **XYZ tiles** (`generate_tiles.py`): generates PNG tiles from zoom 14
  up to whatever zoom matches each raster's REAL resolution
  (`zoom_range()`, derives the max zoom from the geotransform). RGB
  (8-bit color), thermal, multispectral, and D band (grayscale with a
  color range computed from each mission's real percentile), and indices
  (RdYlGn divergent palette). It's invoked incrementally
  (`publish_partial()`) throughout the whole pipeline, not just at the
  end, skipping whatever hasn't changed (`up_to_date()`, mtime).
- **Server** (`webapp/main.py`): serves the Leaflet dashboard (vendored,
  works offline) and the tiles under `/geovisor/`, plus the live progress
  HUD (SSE) and point sampling. It's the same process in `webapp` and
  `serve` modes.

### 7. Final cloud-optimized export pass

- **COG** (`export_cog.py`, no arguments): walks through `PRODUCTS` (all
  possible final rasters, including `dband_orthomosaic.tif`) and converts
  only what exists and **isn't COG yet** (`_ya_es_cog()` checks GDAL's
  real `LAYOUT=COG` metadata). Whatever each sensor already exported in
  stage 4 is skipped at no cost.
- **COPC** (`export_copc.py`, no arguments): converts the 4 georeferenced
  dense point clouds (`odm_georeferencing/odm_georeferenced_model.laz`
  from each project) to Cloud Optimized Point Cloud via `pdal`, skipping
  whichever are already up to date (`_al_dia()`, compares mtime against
  the source).

### 8. Delivery to the user (optional)

- **`export_products.py`**: copies the chosen products to `EXPORT_DIR`,
  in the requested format (`cog`/`gtiff`, `geojson`/`gpkg`/`shp`/`kml`)
  and EPSG. Without `EXPORT_DIR` it's a no-op. Class rasters are
  resampled with nearest neighbor (same criterion as `generate_tiles.py`);
  continuous ones with cubic. It writes an `export_manifest.json` with the
  delivery's details.

  Reprojection happens in **two steps** (warp to a temporary GTiff, then
  `Translate` to the final format) because `gdal.Warp` can't write
  directly to COG: that driver only implements `CreateCopy()`, and the
  Python binding, unlike the `gdalwarp` executable, returns a GeoTIFF with
  a destroyed geotransform without raising any error. See the comment in
  `_export_raster()`.

## Final products

| File | Description | Approx. resolution |
|---|---|---|
| `outputs/dsm.tif` | Clean digital surface model (COG) | depends on preset |
| `outputs/rgb_orthomosaic.tif` | RGB orthophoto (COG) | depends on preset |
| `outputs/thermal_orthomosaic.tif` | Thermal orthomosaic in °C (COG) | depends on preset |
| `outputs/multispectral_orthomosaic.tif` | Multiband orthophoto (COG) | depends on preset |
| `outputs/dband_orthomosaic.tif` | Fast orthophoto from the M3M's RGB camera (COG) | depends on preset |
| `outputs/indices/{ndvi,gndvi,ndre,msavi2}.tif` | Vegetation indices (COG) | same as MS |
| `outputs/confidence_mask.tif` | Reliable RGB∩thermal mask | thermal grid |
| `outputs/termico_hotspot_class.tif`, `outputs/indices/{ndvi,gndvi,ndre,msavi2}_class.tif` | Classifications (COG) | thermal grid / same as MS |
| `outputs/situation.json` | Executive summary for the geovisor | - |
| `outputs/point_cloud_{rgb,thermal,multispectral,dband}.copc.laz` | Georeferenced dense clouds (COPC) | - |
| `outputs/logs/timings.json` | Per-stage time breakdown | - |

## Reference system

Each mission's own: ODM picks the UTM zone based on the flight's GPS and
writes it into all its products. Scripts that need to project (the camera
overlap count in `trim_low_overlap_edges.py`) **derive the CRS from the
raster they're processing itself**, so there's nothing to adjust to fly
in a different zone.

`UTM_EPSG = 32618` stays in that script only as a fallback for a raster
without a readable projection. It's not used as a fixed value: outside
zone 18N that would put camera footprints hundreds of kilometers from the
raster and silently degrade the edge trim.

Vector products (`flight_path.geojson`) go in EPSG:4326 per RFC 7946.
Leaflet ignores any `crs` member and always assumes WGS84.

## Dependencies

- **Docker** + NVIDIA Container Toolkit (recommended, see "The CUDA
  check" above for when it's actually indispensable)
- **DJI Thermal SDK** (included in `dji_thermal_sdk/`)
- **Python 3** with: `gdal`, `numpy`, `scipy`, `pyproj` (in the container)
- **exiftool** (in the container, with `scripts/camera_ns.exiftool.config`
  to be able to write the XMP tag `Camera:BandName`)
- **pdal** (included in ODM's base image, at
  `/code/SuperBuild/install/bin/pdal`)
- **ODM** (inherited from the base image `opendronemap/odm:gpu`)
