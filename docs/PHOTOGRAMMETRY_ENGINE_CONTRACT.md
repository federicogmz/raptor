# Photogrammetry Engine I/O Contract

**Version:** 1.0  
**Date:** October 2026  
**Purpose:** Define the standard, engine-agnostic interoperability contract between **RAPTOR** (tactical wildfire analytics and emergency decision-support system) and photogrammetric reconstruction engines (**Zenith** for direct 2.5D ray-casting in Rust and **OpenDroneMap** for dense 3D Multi-View Stereo).

---

## 1. Architectural Principle

RAPTOR strictly decouples:
1. **The Photogrammetry Engine**: Responsible for camera pose calibration, triangulation, digital elevation/surface modeling (DEM/DSM), and orthomosaic generation.
2. **The RAPTOR Tactical System**: Responsible for operational wildfire analytics (thermal hotspot triage, heat anomaly clustering, spectral burn severity classification, edge reliability masking, and incident command situation reports).

Any photogrammetry engine integrated with RAPTOR must adhere to the input, output, and radiometric preservation rules specified below.

---

## 2. Operational Delivery Tiers (Latency Tiers)

| Tier | Component | Target Latency | Primary Deliverables | Typical Planimetric Accuracy |
| :--- | :--- | :--- | :--- | :--- |
| **Level 0 (Emergency Triage)** | RAPTOR | **< 2 minutes** | `triage_thermal_level0.geojson`, `situation_level0.json` | $\pm 5\text{--}15\text{ m}$ (with local DEM) / $\pm 30\text{--}50\text{ m}$ (flat plane) |
| **Level 1 (Tactical 2.5D Ortho)** | Photogrammetry Engine (`Zenith`) | **< 3 minutes** | Direct 2.5D georeferenced orthomosaics (RGB & Radiometric Thermal °C), camera footprint GeoJSON, `manifest_level1.json` | $\pm 1\text{--}3\text{ m}$ |
| **Level 2 (Full 3D MVS)** | Photogrammetry Engine (`ODM`) | **15 – 60+ minutes** | Full-resolution 3D orthomosaics, dense DSM, COPC point cloud, calibrated multispectral indices (NDVI, NDRE, MSAVI2) | Sub-meter / centimetric ($\pm 0.05\text{--}0.2\text{ m}$) |

---

## 3. Engine Input Specification

The photogrammetry engine accepts the following standardized inputs on the host filesystem or container mounts:

### 3.1. Source Imagery (`images/`)
- **Visual RGB**: JPEG or GeoTIFF frames with standard EXIF/XMP telemetry tags (`GPSLatitude`, `GPSLongitude`, `GPSAltitude`, `GimbalYawDegree`, `GimbalPitchDegree`, `GimbalRollDegree`, and optional `DewarpData` / `LRFTargetDistance`).
- **Radiometric Thermal**: Single-band Float32 GeoTIFF or binary arrays in true degrees Celsius (°C), or 16-bit unsigned integer TIFF in centi-Kelvin convention (`round((°C + 273.15) * 100)`).
- **Multispectral**: Calibrated per-band TIFF frames (Green, Red, RedEdge, NIR) with downwelling irradiance/reflectance metadata.

### 3.2. Camera Georeferencing and Poses (`geo.txt` or EXIF/XMP)
Standard tab-separated pose table headed by the Coordinate Reference System identifier (e.g., `EPSG:4326` or projected CRS):
```text
EPSG:4326
filename    longitude    latitude    altitude    yaw    pitch    roll    horiz_acc    vert_acc
IMG_0001.tif -75.58921   6.16843     2150.4      34.2   -89.5    0.1     0.05         0.08
```

### 3.3. Optional External Elevation Model (`--dem <path.tif>`)
- **Format**: GeoTIFF (Float32 or Int16) or ENVI (`.hdr`/`.raw`) with explicit CRS metadata.
- **Supported surfaces**: Digital Terrain Model (DTM) or Digital Surface Model (DSM).
- **Role in the pipeline**:
  1. Direct 2.5D ray-casting back-projection onto continuous terrain without expensive Multi-View Stereo densification.
  2. Elevation-guided feature matching constraints in 3D reconstruction.
  3. Vertical datum calibration against Laser Rangefinder (LRF) and GNSS observations.

---

## 4. Engine Output Specification

The engine writes the following standardized artifacts to the target output directory:

### 4.1. Orthomosaic Rasters (GeoTIFF / COG)
- `rgb_orthomosaic.tif`: 3-band or 4-band RGBA visual orthomosaic (`Byte`, `0–255`), DEFLATE/LZW compressed with internal overview pyramids.
- `thermal_orthomosaic.tif`: Single-band `Float32` radiometric orthomosaic in **physical degrees Celsius (°C)**.
- `dsm.tif`: Single-band `Float32` Digital Surface Model (meters above vertical datum).

### 4.2. MANDATORY INVARIANT: Radiometric Integrity
> [!IMPORTANT]
> **Destructive photometric blending is strictly prohibited on scientific thermal and multispectral rasters.**
> For radiometric thermal and multispectral bands, pixel resampling MUST preserve physical values (**Nearest-Neighbor** cell sampling or narrow Voronoi seam transitions with active hotspot protection `--max-blend-temp-diff`).
> Global exposure gain rescaling or wide feathering that dilutes peak wildfire combustion temperatures (°C) or alters calibrated spectral reflectance is strictly forbidden.

### 4.3. Level 1 Execution Manifest (`manifest_level1.json`)
A machine-readable JSON manifest certifying contract compliance, runtime metrics, CRS, GSD, elevation source, and output paths:
```json
{
  "schema_version": "1.0",
  "engine_name": "Zenith Direct 2.5D Photogrammetry Engine",
  "engine_version": "1.0.0",
  "tier": "level1_rapid_ortho",
  "elapsed_seconds": 15.27,
  "dem_used": true,
  "gsd_m": 0.0758,
  "resampling": "nearest",
  "crs": "EPSG:9377",
  "products": ["rgb_orthomosaic.tif"]
}
```

### 4.4. Structured Real-Time Telemetry Stream
- Structured progress lines emitted to `stdout` (`[ZENITH_PROGRESS] stage=<name> pct=<0..100> detail="<message>"` or `[STAGE_START]` / `[STAGE_END]`).
- Wall-clock execution times recorded per stage in seconds.
