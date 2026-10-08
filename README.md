# RAPTOR: DJI M3T / H20T / M3M

**R**econstruction of **A**erial **P**roducts, **T**hermal, **O**ptical (and
multispectral), for **R**esponse. A general photogrammetric processing
pipeline, not just for emergencies: three quality presets range from a quick
look in minutes to the maximum detail the flight can deliver (see "Quality,
terrain, and hardware" below). It generates georeferenced RGB and thermal
orthomosaics, plus vegetation indices (NDVI/GNDVI/NDRE/MSAVI2), from DJI
photogrammetric flights (Mavic 3T, Matrice 300 RTK + Zenmuse H20T, and
optionally Mavic 3 Multispectral), with an interactive Leaflet geovisor.

**The entire pipeline runs in ONE SINGLE Docker container** (ODM plus the
pipeline's own post-processing, no nested containers). The DJI Thermal SDK
is already included in the repository. You only need the flight's source
images.

The sensors (RGB+thermal from the M3T/H20T, multispectral from the M3M, D
band from the M3M) are **independent**: each one prepares its images,
reconstructs, and publishes its products on its own, without waiting for
the others unless available RAM forces them to take turns (see "How many
sensors reconstruct at once" below).

---

## Installation: persistent webapp (default)

The way to install RAPTOR on a machine (your own or someone else's) is one
command:

```bash
git clone <repo-url> raptor && cd raptor && ./raptor install
```

`install` builds the image, installs a global `raptor` command on your
`PATH` (so every later command is just `raptor ...`, from any directory —
no `./` and no `cd`ing back into the checkout), asks two questions, and
deploys the webapp. The two questions:

1. **Deliveries folder** — where exported products permanently live on
   your disk. Suggested default `~/raptor-deliveries`, created if it
   doesn't exist. This becomes the mounted export root for every mission
   from then on (see "Delivering the products" below for why it has to be
   decided once, at deploy time, rather than per export).
2. **Port** — default `8080`.

Both are saved to `~/.raptor/config`; `raptor init` reruns just this
onboarding later if you want to change them (follow it with `raptor
restart` to apply).

If `install` can't write to `/usr/local/bin` (no `~/.local/bin` either), it
tells you so and prints the `export PATH=...` line to add to your shell
profile — the checkout keeps working with `./raptor` in the meantime.

Once installed, open **http://localhost:8080** (or whatever port you
chose): name the mission, add the flight folder(s), pick flat or steep
terrain, confirm which products to generate (derived from what you added —
only RGB is offered if there's no thermal, and vice versa), pick a quality
preset, and start. Progress streams live (SSE), and the geovisor with the
tiles comes up as soon as products exist. Export is not part of this setup
— see "Delivering the products" below for how it works once a product is
ready.

Everyday commands, from anywhere:

```bash
raptor start      # deploy (or redeploy) with the saved config
raptor restart     # same thing, explicit name
raptor stop        # stop it
raptor status      # check if it's running
raptor logs        # watch progress live (Ctrl+C doesn't stop it, it only exits the log)
```

`raptor start` stays **in the background** (`-d`) with `--restart
unless-stopped`: if the container goes down or the machine reboots, Docker
brings it back up on its own (as long as Docker's own service is enabled:
`sudo systemctl enable --now docker` on a standard, root, non-rootless
install). Running it again replaces **only** that container (`raptor-web`
by name), and never touches other containers on the same machine.

For a one-off debug session, without touching the saved deploy (runs
attached to the terminal, no automatic restart, removes itself on exit):
`raptor webapp --fg`. `raptor webapp [flags]` is also the manual path for
anyone who wants to skip the saved config and pass `--export`/`--runs`/
`--port` explicitly on each call instead — `raptor start`/`install` are a
thin wrapper around it, not a replacement.

Uploaded photos and results end up in `~/.raptor/runs/<mission>/` by
default (`RAPTOR_RUNS_DIR` in the saved config).

---

## Automation and CI

`./raptor run` processes a mission without interaction, with exit codes and
a machine-readable summary, suitable for chaining into a pipeline:

```bash
./raptor run \
  --input ./vuelos/la_clara \
  --input-ms ./vuelos/la_clara_ms \
  --preset cartografico --terreno escarpado \
  --export ./entregas/la_clara \
  --export-products rgb,thermal,dsm,classes \
  --export-epsg 9377 \
  --json
```

It exits with a code other than 0 if the pipeline fails. `--json` prints
`run_summary.json`: which products came out, at what GSD, CRS, and real
coverage, how many features each vector has, and where the delivery ended
up **on the host disk**. That summary is always written, even when the run
fails, with the exit code inside it. In CI, which stage it died at matters
as much as the code itself. `./raptor run --help` lists all the options.

Unlike the interactive mode, `run` **does not** leave the geovisor serving
when it finishes (that would hang CI); add `--serve` if you want it.

> **Note:** the image's default `CMD` is `webapp`. For the batch pipeline
> you need to pass `run` as an explicit argument. `./raptor run` already
> does this; see "Manual use with `docker run`" below if you invoke Docker
> by hand.

## Quality, terrain, and hardware

Quality is requested via **preset**, chosen by use case and time:

| Preset | What it's for | Resolution ceiling | Features & SfM |
|---|---|---|---|
| `tactico` | Immediate field response: orthomosaic from the sparse SfM cloud (`--fast-orthophoto`), no dense cloud | 10 cm/px | 3k feats (`lowest`), 4 neighbors, hybrid BA |
| `cartografico` *(default)* | The standard delivery for a mission: orthomosaic, DSM, and 3D point cloud | 4 cm/px | 8k feats (`high`), 8 neighbors, full MVS, hybrid BA |
| `forense` | Forensic inspection and archival: maximum detail the flight can deliver | 1 cm/px | 16k feats (`ultra`), 24 neighbors, full MVS, global BA |

*Backwards compatibility aliases:* `vistazo` and `rapido` → `tactico`;
`estandar` and `alta` → `cartografico`; `maxima` → `forense`. A numeric
`--quality 0-100` is also accepted and maps to the equivalent preset.

Each preset fixes several things at once: the surface model's detail level
(the point cloud and mesh that the orthomosaic and DSM come from), the
resolution ceiling requested from ODM, how many features are extracted per
photo, how many neighboring photos are compared when matching, and the
bundle adjustment type. All three reconstruct with `sfm_algorithm:
incremental`. The table lives in one place, `scripts/hardware.py::PRESETS`.

### Terrain: flat vs rugged

`sfm_algorithm: planar` aligns photos through homographies between pairs,
valid only if the scene is actually flat. **On terrain with strong relief
OpenSfM silently discards the photos it can't fit.** Confirmed live: in a
rocky-terrain mission, a `planar` reconstruction incorporated only 19-24% of
the photos, with no visible error. That is why every preset now uses
`incremental` (photo-by-photo bundle adjustment, no plane assumed).

`--terreno escarpado` (and the matching selector in the webapp) is still
accepted: it forces `incremental` on any preset that would use `planar`.
With the current preset table that is none of them, so `plano` and
`escarpado` reconstruct (and estimate) the same way; the flag is kept so
existing scripts and CI invocations keep working.

### `--fast-orthophoto` on RGB (`tactico`)

RGB is the only sensor whose DSM is the source of the mission's surface
model, so in `cartografico` and `forense` it always goes through
`DensifyPointCloud` (the dense cloud/MVS). In `tactico` RGB skips that stage
(`--fast-orthophoto`, the same mechanism D band always uses): the DSM and
orthomosaic come from the sparse SfM cloud instead of the dense one, poorer
and with more gaps, but the difference is measured in hours. Confirmed
live: an RGB reconstruction of ~1200 photos with a laptop GPU took ~11h in
`DensifyPointCloud`. With `--fast-orthophoto` that stage doesn't run.

With `lowest` feature quality, SfM on small or low-texture flights can split
into several disconnected partial reconstructions that ODM then places by
GPS alone. Check `% reconstructed` in `flight_quality.json` (and the
`Reconstruction N: … images` lines in `outputs/logs/odm_rgb.log`) before
relying on a `tactico` mosaic for measurement.

### Estimate before starting

Before starting, `./raptor run` (and the webapp form) show what resolution
the products will come out at and how long it's expected to take, based on
your hardware:

```bash
./raptor run --input ./vuelos/la_clara --preset forense
```
```
🖥️  Hardware detected: 20 cores, 27 GB RAM free, no GPU (runs on CPU)
🎚️  Preset "Forensic": Archival and forensic detail: maximum physical resolution…
📐 Up to 1 cm/px in the orthomosaic and DSM — the real ceiling is set by your flight's GSD…
⏱️  Estimated time with your hardware: 6.6 h – 26.3 h for 1652 photos. Approximate…
```

To see all three presets with their estimated time without starting
anything:

```bash
python3 scripts/hardware.py estimate --photos 1652
```

`--quality N` (0-100) is still accepted and maps to the equivalent preset,
so as not to break runs and scripts that already use it (always on flat
terrain: the legacy number doesn't distinguish terrain).

Two things worth being clear about regarding the estimate:

- **The estimate is deliberately approximate.** The model is linear in the
  number of photos, and the real scene (overlap, vegetation, terrain) is
  not. It's always shown as a range, not as a number that promises a
  precision that doesn't exist.
- **Output resolution isn't decided by quality, it's decided by the
  flight.** The GSD (meters per pixel on the ground) is set by flight
  altitude and the sensor; ODM measures it from the reconstruction and
  **can never go finer** than that, no matter how high the requested
  quality is. Asking for 1 cm on a flight whose real GSD is 9 cm doesn't
  add detail, it just produces smaller pixels. What quality DOES decide is
  whether you can deliberately request a **coarser** ceiling (to finish
  sooner), and whether the 3D model resolves objects like treetops and
  roof edges or not.

### How many sensors reconstruct at once

The ODM projects (`rgb_odm`, `thermal_native_odm`, `multispectral_odm`,
`dband_odm`) are independent from each other: different source folder,
different destination folder, no shared files. Each one **prepares its
images independently** (it doesn't wait for the others to finish
preparing) and only competes for a reconstruction slot once ITS OWN
preparation is done, through a memory-aware semaphore (`odm_slots()` in
`scripts/hardware.py`). On a machine with plenty of RAM, two or more
reconstructions run at the same time; on a smaller one, they take turns.
The old behavior (one at a time) stays as a floor, never a ceiling. RGB in
particular, with its heavier photo profile, usually needs the whole
machine to itself unless it uses `--fast-orthophoto` (`tactico`),
which reduces its real memory need and lets it share a slot with another
sensor.

`RAPTOR_ODM_PARALELO=1` forces the old sequential mode (one at a time);
`MAX_CONCURRENCY N` manually sets the threads per project (a "watch my
memory" knob, not how many projects run together).

---

## Manual use with `docker run` (for scripts / fine control)

If you'd rather invoke Docker yourself (automation, CI, a single one-off
mission) instead of the webapp, follow this guide:

### Quick guide: from the SD card to the geovisor

### Requirements

- Docker
- NVIDIA GPU + `nvidia-container-toolkit` (recommended; see "GPU" below for
  when it's actually indispensable)

```bash
# Ubuntu 24.04:
sudo apt install docker.io nvidia-container-toolkit
sudo systemctl restart docker
```

### Step 1: Build the image

```bash
docker build -t raptor .
```

### Step 2: Run it (all in one command!)

```bash
docker run --gpus all -v /path/to/photos:/input -p 8080:8080 raptor run
```

> The `run` argument at the end is mandatory: the image's default `CMD` is
> `webapp` (see above), so for the classic batch/CLI mode you have to
> request it explicitly.

### GPU

The base image's dense reconstruction binary (`DensifyPointCloud`) is
linked against `libcuda.so.1`, and without the NVIDIA runtime mounted it
doesn't load:

```
DensifyPointCloud: error while loading shared libraries: libcuda.so.1
opendm.system.SubprocessException: Child returned 127
```

That means the run dies at the `openmvs` stage, **after** having paid for
the preparation and the entire SfM, if `--gpus all` was missing and that
mission was actually going to touch the dense stage. That ODM "detects
`nvidia-smi` and falls back to CPU" is true for choosing the *algorithm*,
but it doesn't avoid that dynamic link.

The entrypoint checks this in the first second, **only for sensors that
will actually run `DensifyPointCloud`**: RGB in `tactico` (which
use `--fast-orthophoto`) and D band (which always uses it) are excluded
from the check, same as any run with `SKIP_ODM=1`. If any active sensor
really is going to touch it (thermal, multispectral, or RGB outside
`tactico`) and the runtime is missing, it aborts with instructions
instead of blowing up hours later. `./raptor run` passes `--gpus all` on
its own unless given `--no-gpu`.

**Practical recommendation: pass `--gpus all` whenever the machine allows
it.** It's the only way for the dense stage (when it runs) to be fast; the
automatic check is a safety net for when it isn't needed, not a reason to
skip it out of habit.

The CUDA version mismatch (a driver somewhat older than what the ODM image
asks for) is already resolved by default (`NVIDIA_DISABLE_REQUIRE=1` baked
into the image), no need to pass it by hand.

> **Mount the volumes in this mode.** The image **does not declare
> `VOLUME`** for `processing/`, `preprocessing/`, `outputs/`, or
> `geovisor/tiles/` (the webapp needs to be able to replace those paths
> with symlinks at runtime to handle several missions in one session; see
> the comment in the `Dockerfile`). Without a declared `VOLUME` there's no
> anonymous-volume safety net: **if you don't mount anything in `run`
> mode, the products are lost when the container is removed.** `./raptor
> run` mounts what's needed for you.

Mount them to keep the results, look at the files directly (e.g. the °C
temperature TIFFs in `preprocessing/thermal_dji_sdk/` to use in another
program), and not lose ODM's work if the pipeline fails partway through (so
you can resume with `SKIP_ODM=1`):

```bash
docker run --gpus all \
  -v /path/to/photos:/input \
  -v $PWD/processing:/app/processing \
  -v $PWD/preprocessing:/app/preprocessing \
  -v $PWD/outputs:/app/outputs \
  -v $PWD/geovisor/tiles:/app/geovisor/tiles \
  -p 8080:8080 \
  raptor run
```

If you already ran without mounting `preprocessing/` (like in a previous
`docker run` without `--rm`), get the files out with `docker cp
<container>:/app/preprocessing/thermal_dji_sdk ./preprocessing`.

**One folder per mission (recommended):** `processing/`, `outputs/`, and
`geovisor/tiles/` hold the state of THE mission processed there. If you run
a new mission mounting the same paths as a previous one, ODM reconstructs
on data mixed from two different flights.
Use one `runs/<mission-name>/` folder per run:

```bash
mkdir -p runs/la_clara/{processing,outputs,tiles}
docker run --gpus all --rm \
  -v /path/to/la_clara:/input \
  -v $PWD/runs/la_clara/processing:/app/processing \
  -v $PWD/runs/la_clara/outputs:/app/outputs \
  -v $PWD/runs/la_clara/tiles:/app/geovisor/tiles \
  -p 8080:8080 \
  raptor run
```

That way no run overwrites or mixes another one's work. To view the
geovisor of an already-processed mission later: `docker run --rm -p
8080:8080 -v $PWD/runs/la_clara/tiles:/app/geovisor/tiles raptor serve`.

> Note: if you'd rather not deal with host volumes/paths by hand, the
> [persistent webapp](#installation-persistent-webapp-default) solves
> exactly this same problem (one folder per mission, no mixed runs) by
> uploading photos through the browser instead of mounting them.

**Multispectral (DJI M3M, optional):** if you also have a multispectral
flight of the same area, mount a SECOND volume separate from `/input` (it's
another flight/sensor, it doesn't mix with RGB+thermal):

```bash
docker run --gpus all \
  -v /path/to/photos:/input \
  -v /path/to/m3m-flight:/input_ms \
  -v $PWD/outputs:/app/outputs \
  -v $PWD/geovisor/tiles:/app/geovisor/tiles \
  -p 8080:8080 \
  raptor run
```

You can also run an M3M flight **alone** (without `/input`), mounting only
`/input_ms` and passing `-e MODE=none`: in that case the DSM comes from the
multispectral project itself (which also runs with `--dsm`).

If `/input_ms` isn't mounted, this module isn't touched at all: zero impact
on existing RGB+thermal missions. The M3M flight is processed with ODM
(which supports multispectral natively via the XMP tag `Camera:BandName`)
using `--radiometric-calibration camera+sun` (calibrates to reflectance
using the sun sensor embedded in each band, no physical calibration panel
needed).

**M3M D band (fast visible mosaic, optional):** the M3M also carries its
own RGB camera (a separate sensor from the 4 multispectral lenses, files
`*_D.JPG`). It's its own independent ODM project (`dband_odm`), always with
`--fast-orthophoto` (without this, D band would run the most expensive
stage of the whole mission, `DensifyPointCloud`, without needing it: it
never requests `--dsm`, RGB or multispectral already has one). Explicit
opt-in, not automatic just because `*_D.JPG` files exist: it's extra
processing not every mission wants to pay for:

```bash
docker run --gpus all \
  -v /path/to/m3m-flight:/input_ms \
  -e DBAND=1 \
  -v $PWD/outputs:/app/outputs \
  -p 8080:8080 \
  raptor run
```

In the webapp it's a checkbox next to the multispectral block. Its products
(`outputs/dband_orthomosaic.tif`, `outputs/point_cloud_dband.copc.laz`) and
its layer in the geovisor are independent of the 4 spectral bands.

Useful environment variables (`-e VAR=value`):

| Variable | Default | Use |
|---|---|---|
| `MODE` | `rgb+thermal` | `rgb` to skip all thermal processing; `none` if the mission does NOT have an RGB/thermal flight (multispectral only, requires `/input_ms`) |
| `SKIP_ODM` | `0` | `1` to reuse `processing/*_odm/` from a previous run |
| `MS_SOURCE_DIR` | `/input_ms` | Source folder for the multispectral flight (the module runs ONLY if it exists) |
| `DBAND` | `0` | `1` to also reconstruct the M3M's D band (visible mosaic, opt-in, requires `MS_SOURCE_DIR` with `*_D.JPG` files) |
| `PORT` | `8080` | Geovisor port |
| `SERVE` | `1` | `0` to not bring up the geovisor when finished |
| `PRESET` | `cartografico` | `tactico` \| `cartografico` \| `forense` (legacy `vistazo`/`rapido`/`estandar`/`alta`/`maxima` still accepted). Surface model detail, resolution ceiling, features per photo, and bundle adjustment. See "Quality" above |
| `TERRENO` | `plano` | `plano` \| `escarpado`. Kept for compatibility; all current presets already reconstruct incrementally, see "Terrain" above |
| `QUALITY` | - | *(legacy)* 0-100; maps to the equivalent preset |
| `RAPTOR_ODM_PARALELO` | automatic | `1` forces one ODM reconstruction at a time; by default it's calculated from available RAM (`odm_slots()` in `scripts/hardware.py`) |
| `MAX_CONCURRENCY` | automatic | ODM threads per project. Calculated from available RAM by default; lower it if the process dies without a message (see "Memory" in Notes) |
| `EXPORT_DIR` | - | Delivery folder. Without this nothing gets exported |
| `EXPORT_PRODUCTS` | `all` | What to export, comma-separated (see below) |
| `EXPORT_RASTER_FORMAT` | `cog` | `cog` \| `gtiff` |
| `EXPORT_VECTOR_FORMAT` | `geojson` | `geojson` \| `gpkg` \| `shp` \| `kml` |
| `EXPORT_EPSG` | `source` | Output EPSG, or `source` to skip reprojecting |

### Delivering the products

Exporting is not something you configure before a mission runs — it's an
action you take afterward, once a product actually exists, so you can
export one product today and another tomorrow without having had to decide
everything up front. In the webapp, once a mission has tiles or generated
products, its card in the mission list gets an **Export** button: it opens
a modal offering only the products that mission actually generated
(checking a multispectral box on an RGB/thermal-only mission isn't
possible — it's simply not offered), plus destination folder, format, and
CRS. On the CLI it's the `EXPORT_*` variables, applied at the end of that
same `run` invocation:

```bash
docker run --gpus all \
  -v /path/to/photos:/input \
  -v /path/to/deliveries:/deliveries \
  -e EXPORT_DIR=/deliveries/la_clara \
  -e EXPORT_PRODUCTS=rgb,thermal,dsm,classes \
  -e EXPORT_EPSG=9377 \
  -e EXPORT_VECTOR_FORMAT=gpkg \
  -p 8080:8080 raptor run
```

Available products: `rgb`, `thermal`, `dsm`, `multispectral`, `indices`,
`classes`, `confidence`, `flight_path`, `situation`, `pointclouds`
(includes D band if it ran) (or `all`). Whichever the mission didn't
generate are skipped without failing.

The webapp's export modal needs a deliveries folder mounted into the
container to have anywhere to write to — this is exactly what `raptor
install`/`raptor init` ask for and mount permanently (Docker can't add a
bind mount to an already-running container, so it has to be decided once,
at deploy time; everything *under* that mounted root — which products,
format, CRS, destination subfolder — is still chosen freely on each
export). Without it, the Export button still opens but shows why it can't
write anywhere yet instead of a destination field.

**EPSG:9377 (MAGNA-SIRGAS / Origen-Nacional)** is the recommended value:
it's Colombia's single national system adopted by the IGAC, the one
government entities expect for official cartography. ODM produces its
outputs in the WGS84 UTM zone matching the flight's GPS, so without this
step you have to reproject by hand in QGIS after every mission. Class
rasters (hotspot, classified indices) are resampled by nearest neighbor,
never by averaging: averaging classes invents intermediate categories that
don't exist.

The destination folder is a path **inside the container**: to write to the
host disk you have to mount it (`-v /path/on/the/host:/deliveries`). The webapp
checks this as you write it and warns right there if the path doesn't
exist or can't be written to, instead of failing at the end of the run.
Alongside the files it writes an `export_manifest.json` with what was
exported, in which CRS, and in which formats.

The entrypoint does **everything automatically**:
1. **Organizes** the images from `/input` (searches recursively for
   `*_V.JPG`, `*_W.JPG`, `*_T.JPG`) and, in parallel, if `/input_ms`
   exists, the MS bands and D band from there (`*_MS_G/R/RE/NIR.TIF`,
   `*_D.JPG`)
2. **Extracts metadata** GPS/EXIF; **converts** R-JPEG to °C (DJI SDK)
   plus denoise plus re-encoding for ODM. Each sensor prepares its images
   independently
3. **Reconstructs each sensor** as soon as ITS preparation is done,
   without waiting for the others: RGB, native thermal, multispectral, and
   D band compete for a memory-aware reconstruction slot (see "How many
   sensors reconstruct at once")
4. **Trims edges** with low overlap as soon as ITS sensor finishes
   reconstructing (same criterion for all four products, with a confidence
   mask based on RGB∩thermal camera overlap), cleans the DSM (discards
   synthetic fill), and **exports that sensor to COG/COPC** without
   waiting for the others
5. In parallel with each other: **computes NDVI/GNDVI/NDRE/MSAVI2** and
   classifies them (if there's multispectral), **classifies the thermal
   hotspot** (absolute temperature, with or without multispectral), and
   **computes survey quality** (camera overlap, flight speed, %
   reconstructed)
6. **Generates tiles** (incrementally, as soon as each product is ready,
   not only at the end) plus a final COG/COPC export pass that skips what
   each sensor already exported
7. **Deploys** the geovisor at `http://localhost:8080`

### Step 3: Open the geovisor

**http://localhost:8080**

Includes:
- RGB, thermal, and D band layers with adjustable opacity
- **Band combination selector** (dropdown): for the RGB layer, reorder its
  R/G/B channels; for multispectral, choose between predefined
  combinations (CIR, RedEdge) or build a custom one combining any of the 4
  spectral bands (Red/Green/RedEdge/NIR)
- Color palettes (inferno, viridis, jet, ironbow, hot/cold)
- RGB vs thermal comparison slider
- Hotspot detection
- Distance measurement tool
- DSM hillshade
- View export
- Vegetation index layers (NDVI/GNDVI/NDRE/MSAVI2, RdYlGn palette) if the
  mission included a multispectral flight
- Works offline once loaded: Leaflet is vendored (`geovisor/vendor/`), no
  dependency on a CDN

---

## Pipeline

```
data/rgb_mosaico/ + data/termica_mosaico/  [+ data/multiespectral_mosaico/ + data/dband_mosaico/]
        │  (parallel organization)
        ▼
  1. Metadata (GPS, EXIF) + flight path (geovisor shows something from minute one)
  2. Preparation PER SENSOR, each one independent: geo.txt/images RGB+MS+D;
     thermal → DJI SDK °C + denoise + re-encode Kelvin×100
  3. Reconstruction PER SENSOR, each one starts as soon as ITS preparation is done,
     competing for a memory-aware slot (odm_slots()):
       ODM RGB (SfM+[MVS]+DSM+ortho, MVS except tactico, there --fast-orthophoto)
       ODM native thermal (SfM+MVS+mesh+texture+ortho in °C)
       ODM Multispectral (SfM+calibration+multiband ortho, if applicable)
       ODM D band (SfM+fast ortho, --fast-orthophoto always, if applicable)
  4. Per sensor, as soon as ITS reconstruction is done: DSM cleanup (RGB/MS only)
     + low-overlap edge trim + COG/COPC export of THAT sensor
  5. In parallel with each other, over what's already trimmed: confidence mask
     (RGB∩thermal) + survey quality + vegetation indices
     (if MS) + thermal hotspot (if thermal)
  6. XYZ tiles (incremental) + geovisor (with band combination selector)
  7. Final cloud-optimized export pass: whatever's missing → COG/COPC
        │
        ▼
  outputs/: dsm.tif, rgb_orthomosaic.tif, thermal_orthomosaic.tif (all COG)
            [multispectral_orthomosaic.tif, dband_orthomosaic.tif,
             indices/{ndvi,gndvi,ndre,msavi2}.tif (COG),
             termico_hotspot_class.tif]
            point_cloud_{rgb,thermal,multispectral,dband}.copc.laz
  geovisor/: http://localhost:8080
```

**Timings**: they depend too much on the preset, terrain, and hardware for
a fixed table. Use `python3 scripts/hardware.py estimate --photos N
[--preset P] [--terreno T]` for the real estimate for your case. The
reference that motivated dropping `planar`, measured live with the former
fastest preset (20-core laptop, RTX A1000 6GB, ~1200 photos per sensor,
rugged terrain):

| SfM algorithm | RGB | Thermal |
|---|---|---|
| `planar` | minutes, 23.6% coverage | minutes, 18.7% coverage |
| `incremental` | ~10h, 99.2% coverage | ~6h, 98.7% coverage |

Incremental reconstruction is sequential by design (one photo at a time,
not parallelizable across photos). It's the real cost of not losing
coverage on terrain with strong relief, not a suboptimal config.

---

## Project structure

```
raptor/
├── data/                          ← Source images (organized by the entrypoint from /input)
├── preprocessing/thermal_dji_sdk/ ← °C TIFFs (regenerable)
├── processing/                    ← ODM working directories (rgb_odm, thermal_native_odm,
│                                     multispectral_odm, dband_odm)
├── outputs/                       ← Final products
├── scripts/                       ← Pipeline steps (Python); catalog in scripts/README.md
├── geovisor/                      ← Leaflet dashboard (band selector, "Current situation" panel,
│                                     Leaflet vendored, works offline)
├── webapp/                        ← Interactive webapp (FastAPI): main.py, static/index.html
├── core/                          ← Pipeline orchestrator (activate_mission, PipelineRun,
│                                     mission scanning), used by webapp/main.py
├── docker/                        ← entrypoint.sh (orchestrates everything) + setup-data.sh +
│                                     setup-data-multispectral.sh + patch_odm_multispectral.py
├── dji_thermal_sdk/               ← DJI Thermal SDK (included)
├── docs/                          ← PIPELINE.md (technical documentation), REVIEW_PIPELINE.md
├── tests/                         ← pytest suite (run with ./run_tests.sh)
├── paper/                         ← Manuscript drafts and research notes (not part of the image)
├── raptor                         ← Host CLI launcher (install, start, run, webapp, build)
├── run_tests.sh                   ← Containerized test runner
├── Dockerfile
├── Makefile
├── CONTRIBUTING.md
├── LICENSE
└── README.md
```

---

## Advanced use

### Individual steps (inside the container)

For manual debugging, get a shell inside the container (mount the same
volumes as a normal run) and run individual `make` targets:

```bash
docker run --rm -it \
  -v $PWD/processing:/app/processing -v $PWD/outputs:/app/outputs \
  --entrypoint bash raptor

# inside the container:
make clean-dsm trim-edges-dsm trim-edges-rgb
make sdk-convert denoise-thermal prepare-thermal-native  # before invoking thermal ODM
make trim-edges-thermal confidence-mask flight-quality
make prepare-multispectral trim-edges-multispectral compute-indices  # Only if /input_ms exists
make prepare-dband trim-edges-dband                         # Only if DBAND=1
make compute-thermal-hotspot situation-summary              # Thermal without multispectral
make tiles serve                                            # Tiles + viewer
make export-cog export-copc                                 # Rasters → COG, clouds → COPC
make info                                                   # Status
make clean-all                                              # Clean results
```

(ODM's SfM/MVS/mesh/texture/ortho, for all four sensors, doesn't have a
`make` target. It's invoked directly as `python3 /code/run.py ...`; see
`docker/entrypoint.sh`, function `run_odm()` and `_odm_args()`.)

---

## Expected structure in `/input`

The pipeline is designed to **not version data**: you just mount the
mission folder as a volume:

```
<source-directory>/
├── DJI_20240615100000_0001_V.JPG   ← RGB
├── DJI_20240615100003_0002_V.JPG
├── ...
└── THERMAL/                        ← or in the same folder
    ├── DJI_20240615100000_0001_T.JPG   ← Thermal
    ├── DJI_20240615100003_0002_T.JPG
    └── ...
```

The entrypoint recursively searches for `*_V.JPG`, `*_W.JPG` (RGB) and
`*_T.JPG` (thermal) inside `/input` (SD card, disk, or local folder mounted
with `-v`).

### Expected structure in `/input_ms` (multispectral, optional)

DJI M3M flight, mounted as a SEPARATE volume from `/input` (it's another
drone/sensor over the same area, it doesn't mix):

```
<m3m-flight>/
├── DJI_20240615100000_0001_MS_G.TIF    ← Green
├── DJI_20240615100000_0001_MS_R.TIF    ← Red
├── DJI_20240615100000_0001_MS_RE.TIF   ← RedEdge
├── DJI_20240615100000_0001_MS_NIR.TIF  ← NIR
├── DJI_20240615100000_0001_D.JPG       ← RGB "display" (D band, optional, see DBAND=1)
└── ...
```

The entrypoint recursively searches `/input_ms` for `*_MS_G.TIF`,
`*_MS_R.TIF`, `*_MS_RE.TIF`, `*_MS_NIR.TIF` for the 4 spectral bands, and
`*_D.JPG` for D band (opt-in, see `DBAND=1` above).

### Just the viewer (if you already have tiles generated)

```bash
docker run --rm -p 8080:8080 -v $PWD/geovisor/tiles:/app/geovisor/tiles \
  raptor serve
```

---

## Notes

- **Thermal sensor**: DJI's SDK calibrates atmospheric correction up to
  25m. At ~500m AGL this is a hardware limitation.
- **Tests**: `make test` (inside the container) runs `scripts/check_deps.py`
  and the `tests/` suite. No flight data needed: the `trim_*` tests run
  against a synthetic mission (`tests/synthetic.py`), and the webapp is
  tested with `TestClient` without starting the server. Many tests extract
  REAL blocks from `docker/entrypoint.sh`/`Makefile` and run them with
  stubs, so they can't drift out of sync with the source. It covers the
  adaptive thresholds and trimming, the per-sensor async dispatch and its
  memory semaphore, the presets and `terreno`, export (formats, CRS and
  georeferencing, COG/COPC idempotency), the CI JSON summary, the form's
  pre-validation, and the launcher's argument validation.
  The characterization test compares the trim against
  `tests/golden/trim_masks.json`: if a change moves even a single pixel, it
  fails. If the change is intentional, delete that file and regenerate it
  by running the suite twice.
- **Dependencies**: the ones RAPTOR installs are pinned in
  `requirements.txt`. GDAL, pyproj, and scipy are inherited from
  `opendronemap/odm:gpu` (a moving tag) and aren't pinned with pip so as
  not to fight its SuperBuild; instead `scripts/check_deps.py` checks the
  supported ranges and **fails the build** if the base image moved.
- **Memory**: ODM documents a peak of ~1 GB per thread for every 2 MP of
  image, and by default uses **all** cores. The pipeline caps each
  sensor's threads to what available RAM can handle (`safe_concurrency()`)
  and ALSO decides how many sensors can reconstruct at once without adding
  more threads than RAM can afford (`odm_slots()`, see "How many sensors
  reconstruct at once" above). This used to protect only the multispectral
  band alignment; now it protects any combination of sensors running at
  the same time. When the kernel kills the process for lack of memory
  there's no exception to log: the log just stops dead mid-stage. To force
  it by hand: `-e MAX_CONCURRENCY=4` (threads per project) or `-e
  RAPTOR_ODM_PARALELO=1` (one at a time).
- **Timings for each run**: `outputs/logs/timings.json` (and the summary
  the entrypoint prints at the end) break down preparation, each ODM
  reconstruction, and post-processing, via `scripts/print_timings.py`.
  Useful for calibrating how long your own machine takes with your own
  missions.
- **EPSG**: taken from each mission's own raster projection (the UTM zone
  ODM chose based on its GPS), nothing needs adjusting to fly in a
  different zone. `UTM_EPSG` in `trim_low_overlap_edges.py` is only the
  fallback if a raster arrived without a readable projection.
- **Reprocessing**: delete `processing/` and `outputs/` (or `make
  clean-all` inside the container) and run `docker run ... raptor run`
  again (or re-upload the mission from the webapp).
- **DSM and edge trim**: `dsm_clean.py` discards the synthetic hole-fill
  ODM applies (a constant-height platform, not real terrain) without
  refilling it with any invented value; `trim_low_overlap_edges.py`
  applies the same camera-overlap geometric criterion to all four products
  (RGB, thermal, multispectral, D band) so their trimmed edges line up
  with each other.
- **Native thermal (ODM)**: the thermal orthomosaic is generated with
  ODM's 3D mesh renderer (same as RGB/multispectral), not a custom
  heuristic blend. `scripts/prepare_thermal_native_odm.py` re-encodes the
  Float32 °C TIFFs (`convert_thermal_tiff.py`) to uint16 Kelvin×100 and
  tags them as `Make=DJI`/`Model=ZH20T`/XMP `Camera:BandName=LWIR`, the
  exact format ODM's `opendm/thermal.py` recognizes to apply its own
  Kelvin→°C calibration during texture rendering. Against an Agisoft
  reference delivery, the native render gives 68.6% coverage versus 16.9%
  for a custom heuristic blend, with no fragmentation artifacts and at
  higher resolution. See `docs/PIPELINE.md`.
- **Geolocated thermal TIFF**: every temperature TIFF in
  `preprocessing/thermal_dji_sdk/*.tif` carries the GPS/gimbal EXIF of its
  source R-JPEG embedded in it. `prepare_thermal_native_odm.py` uses it to
  build the thermal ODM project's `geo.txt`, and it also lets you
  export/deliver these Float32 °C TIFFs to external software (Agisoft,
  Pix4D) that builds its own alignment from each photo's GPS.
- **Cloud-optimized products (COG + COPC)**: all final rasters in
  `outputs/*.tif` (RGB, thermal, DSM, confidence mask, multispectral, D
  band, indices) are converted to **COG** (Cloud Optimized GeoTIFF:
  embedded overviews, readable in HTTP ranges without downloading the
  whole file; QGIS/ArcGIS open them just like a normal GeoTIFF) as soon as
  post-processing for EACH sensor finishes, not at the end of the whole
  mission. ODM's georeferenced dense point clouds
  (`odm_georeferencing/odm_georeferenced_model.laz`, from all four
  projects) are also exported to **COPC**
  (`outputs/point_cloud_{rgb,thermal,multispectral,dband}.copc.laz`) for
  streaming in Potree/QGIS/CloudCompare, with the same incremental
  criterion. A final pass with no arguments skips whatever's already up to
  date (COG: checks the real layout; COPC: compares mtimes) and only
  converts what's missing. Manual targets: `make export-cog` / `make
  export-copc`.
- **Multispectral**: the `camera+sun` calibration uses the sun sensor
  embedded in each M3M band, no physical calibration panel needed. PPK
  correction isn't supported yet (it requires a base station file, not
  included); the RTK GPS already embedded in the EXIF is used instead,
  same as RGB/thermal.

---

## Technical documentation

See `docs/PIPELINE.md` for:
- Details of each stage
- Adjustable ODM parameters (RGB, native thermal, multispectral, D band)
- Per-sensor async dispatch and memory allocation between projects
- Edge trim criteria (convex hull of the reconstructed photos' ground footprints)
- Artifact diagnostics (arcs, combing, sweeps)

---

## Contributing & License

* **Contributing:** Please read [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, containerized test runner instructions, and coding standards.
* **Scripts Catalog:** See [scripts/README.md](scripts/README.md) for a breakdown of every pipeline step.
* **License:** RAPTOR's own code is released under the [MIT License](LICENSE). The bundled DJI Thermal SDK in `dji_thermal_sdk/` keeps its own terms (see `dji_thermal_sdk/License.txt`).
