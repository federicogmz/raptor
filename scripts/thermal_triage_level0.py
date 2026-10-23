#!/usr/bin/env python3
"""
scripts/thermal_triage_level0.py
================================
Triage Térmico Nivel 0 (< 2 minutos) para RAPTOR.

Geolocaliza focos térmicos activos (temperatura aparente >= 88.0 °C) directamente
a partir de la pose de cada fotograma (GPS + actitud de gimbal) mediante
trazado de rayos (ray-casting) con ray marching acelerado en memoria e interpolación
bilineal sobre un DEM/DSM externo opcional (e.g. DSM de 2 m ortométrico del Valle de Aburrá),
con fallback a plano horizontal si no se dispone de DEM o si el foco cae fuera de cobertura.

Mejoras clave y física de sensores:
1. Intrínsecos DJI M3T:
   Sensor VOx 640x512, pitch 12 µm, distancia focal nominal 9.1 mm (DFOV 61° / HFOV 46.8°).
   Focal por defecto calibrada f = 758.3 px (no el falso HFOV de 61° que subestimaba la focal).
   Lectura prioritaria de CalibratedFocalLength / FocalLength desde EXIF/XMP.
2. Carga en memoria del ROI del DEM:
   Calcula la extensión horizontal del vuelo (+ margen) y extrae una sola vez el recorte
   a memoria NumPy. Muestreo por interpolación bilineal continua (sin escalones).
3. Corrección de Datum Vertical:
   H_orto = h_elipsoidal - N. En Antioquia / Valle de Aburrá, la ondulación geoidal
   N ≈ -24.0 m, por lo que H_orto = h_elip + 24.0 m.
   Protección contra desajuste de datum: si Z_c <= Z_dem en la vertical, se reporta.
4. Incertidumbre y Deduplicación Rigurosa:
   Propaga error de GNSS, gimbal, IFOV del sensor y proyección altimétrica.
   La deduplicación conserva un piso de error de modo común (no colapsa artificialmente con 1/sqrt(n)).
5. Integridad Operacional:
   Limpieza de salidas previas, flags de geometry_source ("external_dem" vs "plane_fallback"),
   y status explícito para centros de mando.
"""

import argparse
import glob
import json
import math
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from osgeo import gdal, osr
from scipy import ndimage

gdal.UseExceptions()

# Especificaciones del sensor térmico DJI Mavic 3 Enterprise Thermal (M3T)
# Sensor: Uncooled VOx Microbolometer 640 x 512, Pixel Pitch: 12 µm, Focal length: ~9.1 mm
DEFAULT_THERMAL_W = 640
DEFAULT_THERMAL_H = 512
DEFAULT_PIXEL_PITCH_UM = 12.0
DEFAULT_FOCAL_MM = 9.1
DEFAULT_FOCAL_PX = (DEFAULT_FOCAL_MM * 1000.0) / DEFAULT_PIXEL_PITCH_UM  # ~758.33 px
DEFAULT_DFOV_DEG = 61.0  # Campo diagonal nominal DJI

# Parámetros de detección de incendios (Wildfire mop-up)
DEFAULT_TEMP_THRESHOLD = 88.0  # °C aparentes
DEFAULT_MIN_PIXELS = 9  # Filtro de ruido (~0.09 m² a GSD ~10cm)

# Datum vertical: Ondulación geoidal en Antioquia / Valle de Aburrá (EGM96 / GEOCOL)
DEFAULT_GEOID_UNDULATION_M = -24.0  # H = h - (-24.0) = h + 24.0


def _get_utm_epsg(lon: float, lat: float) -> int:
    """Calcula el código EPSG de la zona UTM para una coordenada WGS84."""
    zone = int(math.floor((lon + 180.0) / 6.0)) + 1
    return 32600 + zone if lat >= 0 else 32700 + zone


def build_camera_basis_enu(
    yaw_deg: float, pitch_deg: float, roll_deg: float = 0.0
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Construye la base ortonormal de la cámara en coordenadas ENU (East, North, Up).
    Convención DJI Gimbal:
      - yaw: grados en sentido horario desde el Norte (0° = North, 90° = East).
      - pitch: grados desde horizontal (0° = horizontal, -90° = nadir hacia abajo).
      - roll: rotación alrededor del eje óptico de la cámara.
    """
    psi = math.radians(yaw_deg)
    theta = math.radians(pitch_deg)
    phi = math.radians(roll_deg)

    sin_psi, cos_psi = math.sin(psi), math.cos(psi)
    sin_theta, cos_theta = math.sin(theta), math.cos(theta)

    # 1. Eje óptico de la cámara (adelante)
    z_c = np.array([cos_theta * sin_psi, cos_theta * cos_psi, sin_theta], dtype=np.float64)
    z_c /= np.linalg.norm(z_c)

    # 2. Vector base derecha de la imagen (sin roll)
    x_c = np.array([cos_psi, -sin_psi, 0.0], dtype=np.float64)

    # 3. Vector base arriba de la imagen (sin roll)
    u_top = np.array([-sin_theta * sin_psi, -sin_theta * cos_psi, cos_theta], dtype=np.float64)

    # 4. Aplicar rotación de Roll alrededor de z_c
    cos_phi, sin_phi = math.cos(phi), math.sin(phi)
    x_c_prime = cos_phi * x_c + sin_phi * u_top
    u_top_prime = -sin_phi * x_c + cos_phi * u_top

    x_c_prime /= np.linalg.norm(x_c_prime)
    u_top_prime /= np.linalg.norm(u_top_prime)

    return x_c_prime, u_top_prime, z_c


def pixel_to_ray_vector(
    u: float,
    v: float,
    w: int,
    h: int,
    fx: float,
    fy: float,
    yaw_deg: float,
    pitch_deg: float,
    roll_deg: float = 0.0,
    cx: Optional[float] = None,
    cy: Optional[float] = None,
) -> np.ndarray:
    """
    Convierte las coordenadas de píxel (u, v) en un vector unitario de dirección
    del rayo en sistema de coordenadas ENU.
    """
    if cx is None:
        cx = w / 2.0
    if cy is None:
        cy = h / 2.0

    x_c_prime, u_top_prime, z_c = build_camera_basis_enu(yaw_deg, pitch_deg, roll_deg)

    dx = (u - cx) / fx
    dy = (v - cy) / fy

    ray = z_c + dx * x_c_prime - dy * u_top_prime
    norm = np.linalg.norm(ray)
    if norm < 1e-12:
        return z_c
    return ray / norm


class DEMReaderInMemory:
    """
    Lector de DEM de alto rendimiento: extrae el ROI de la misión a memoria NumPy
    y realiza muestreo continuo por interpolación bilineal.
    """

    def __init__(
        self,
        dem_path: str,
        target_crs_wkt: str,
        bbox_utm: Tuple[float, float, float, float],
        buffer_m: float = 1500.0,
        geoid_undulation_m: float = 0.0,
        vertical_datum: str = "orthometric",
    ):
        self.dem_path = dem_path
        self.geoid_undulation_m = geoid_undulation_m
        self.vertical_datum = vertical_datum

        ds = gdal.Open(dem_path)
        if ds is None:
            raise FileNotFoundError(f"No se pudo abrir el DEM en {dem_path}")

        dem_srs_wkt = ds.GetProjection()
        dem_srs = osr.SpatialReference()
        dem_srs.ImportFromWkt(dem_srs_wkt)
        dem_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

        target_srs = osr.SpatialReference()
        target_srs.ImportFromWkt(target_crs_wkt)
        target_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

        self.needs_reproject = not dem_srs.IsSame(target_srs)
        if self.needs_reproject:
            self.target_to_dem = osr.CoordinateTransformation(target_srs, dem_srs)
            self.dem_to_target = osr.CoordinateTransformation(dem_srs, target_srs)
        else:
            self.target_to_dem = None
            self.dem_to_target = None

        gt = ds.GetGeoTransform()
        inv_gt = gdal.InvGeoTransform(gt)

        # Transformar el bbox a coordenadas del DEM
        min_x, min_y, max_x, max_y = bbox_utm
        min_x -= buffer_m
        min_y -= buffer_m
        max_x += buffer_m
        max_y += buffer_m

        if self.needs_reproject:
            corners = [
                self.target_to_dem.TransformPoint(min_x, min_y)[:2],
                self.target_to_dem.TransformPoint(min_x, max_y)[:2],
                self.target_to_dem.TransformPoint(max_x, min_y)[:2],
                self.target_to_dem.TransformPoint(max_x, max_y)[:2],
            ]
            dem_min_x = min(c[0] for c in corners)
            dem_max_x = max(c[0] for c in corners)
            dem_min_y = min(c[1] for c in corners)
            dem_max_y = max(c[1] for c in corners)
        else:
            dem_min_x, dem_max_x = min_x, max_x
            dem_min_y, dem_max_y = min_y, max_y

        # Convertir a píxeles en el raster original
        p0_x = inv_gt[0] + dem_min_x * inv_gt[1] + dem_max_y * inv_gt[2]
        p0_y = inv_gt[3] + dem_min_x * inv_gt[4] + dem_max_y * inv_gt[5]
        p1_x = inv_gt[0] + dem_max_x * inv_gt[1] + dem_min_y * inv_gt[2]
        p1_y = inv_gt[3] + dem_max_x * inv_gt[4] + dem_min_y * inv_gt[5]

        col_min = max(0, int(math.floor(min(p0_x, p1_x))))
        col_max = min(ds.RasterXSize - 1, int(math.ceil(max(p0_x, p1_x))))
        row_min = max(0, int(math.floor(min(p0_y, p1_y))))
        row_max = min(ds.RasterYSize - 1, int(math.ceil(max(p0_y, p1_y))))

        win_w = max(1, col_max - col_min + 1)
        win_h = max(1, row_max - row_min + 1)

        band = ds.GetRasterBand(1)
        self.nodata = band.GetNoDataValue()
        self.data = band.ReadAsArray(col_min, row_min, win_w, win_h).astype(np.float32)

        # Geotransform de la ventana recortada
        win_x0 = gt[0] + col_min * gt[1] + row_min * gt[2]
        win_y0 = gt[3] + col_min * gt[4] + row_min * gt[5]
        self.win_gt = (win_x0, gt[1], gt[2], win_y0, gt[4], gt[5])
        self.inv_win_gt = gdal.InvGeoTransform(self.win_gt)
        self.win_w = win_w
        self.win_h = win_h
        self.dem_res = min(abs(gt[1]), abs(gt[5]))
        ds = None

    def sample_elevation_bilinear(self, x: float, y: float) -> Optional[float]:
        """Interpolación bilineal continua sobre el recorte en memoria."""
        if self.needs_reproject:
            try:
                dem_x, dem_y, _ = self.target_to_dem.TransformPoint(x, y)
            except Exception:
                return None
        else:
            dem_x, dem_y = x, y

        px = self.inv_win_gt[0] + dem_x * self.inv_win_gt[1] + dem_y * self.inv_win_gt[2]
        py = self.inv_win_gt[3] + dem_x * self.inv_win_gt[4] + dem_y * self.inv_win_gt[5]

        # Validar límites
        if px < 0 or px >= self.win_w - 1 or py < 0 or py >= self.win_h - 1:
            return None

        c0, r0 = int(math.floor(px)), int(math.floor(py))
        c1, r1 = c0 + 1, r0 + 1
        fx, fy = px - c0, py - r0

        v00 = self.data[r0, c0]
        v10 = self.data[r0, c1]
        v01 = self.data[r1, c0]
        v11 = self.data[r1, c1]

        if not (np.isfinite(v00) and np.isfinite(v10) and np.isfinite(v01) and np.isfinite(v11)):
            return None
        if self.nodata is not None:
            if any(np.isclose(v, self.nodata) for v in (v00, v10, v01, v11)):
                return None

        # Bilineal
        top = (1.0 - fx) * v00 + fx * v10
        bottom = (1.0 - fx) * v01 + fx * v11
        elev_raw = float((1.0 - fy) * top + fy * bottom)

        # Ajuste de datum: si el DEM es ortométrico y el dron es elipsoidal (WGS84)
        # H_orto = h_elip - N => h_elip = H_orto + N
        # Para comparar en el sistema del DEM (ortométrico): elev_dem permanece ortométrica
        return elev_raw


def intersect_ray_dem(
    c_xyz: np.ndarray,
    ray_dir: np.ndarray,
    dem: DEMReaderInMemory,
    max_range_m: float = 4000.0,
    bisection_steps: int = 16,
) -> Tuple[Optional[np.ndarray], str, float]:
    """
    Intersecta un rayo óptico con el DEM usando ray-marching adaptativo y bisección.
    Retorna (punto_impacto_xyz, metodo, distancia_s).
    """
    dz = ray_dir[2]
    if dz >= -1e-4:
        return None, "invalid_ray_direction", 0.0

    # Comprobación de integridad: la cámara debe estar sobre el DEM
    initial_dem_z = dem.sample_elevation_bilinear(c_xyz[0], c_xyz[1])
    if initial_dem_z is not None and c_xyz[2] <= initial_dem_z:
        # La cámara empieza debajo del DEM (desajuste severo de datum o colisión)
        return None, "camera_below_dem_datum_mismatch", 0.0

    off_nadir_rad = math.acos(np.clip(-dz, 0.0, 1.0))
    sin_off_nadir = max(0.15, math.sin(off_nadir_rad))

    # Paso de avance acotado para evitar divergencia a nadir y no saltar crestas
    step_s = min(15.0, max(1.0, (dem.dem_res * 0.5) / sin_off_nadir))

    s_prev = 0.0
    s = step_s
    crossed = False

    while s <= max_range_m:
        p = c_xyz + s * ray_dir
        dem_z = dem.sample_elevation_bilinear(p[0], p[1])

        if dem_z is None:
            # Rayo salió de la cobertura de la ventana
            break

        ray_z = p[2]
        if ray_z <= dem_z:
            crossed = True
            break

        s_prev = s
        s += step_s

    if not crossed:
        return None, "out_of_dem_bounds", s

    # Bisección para refinamiento continuo y submétrico en [s_prev, s]
    s_low = s_prev
    s_high = s
    for _ in range(bisection_steps):
        s_mid = 0.5 * (s_low + s_high)
        p_mid = c_xyz + s_mid * ray_dir
        z_dem_mid = dem.sample_elevation_bilinear(p_mid[0], p_mid[1])
        if z_dem_mid is None:
            break
        if p_mid[2] <= z_dem_mid:
            s_high = s_mid
        else:
            s_low = s_mid

    s_star = 0.5 * (s_low + s_high)
    impact_pt = c_xyz + s_star * ray_dir
    return impact_pt, "external_dem", s_star


def intersect_ray_plane(
    c_xyz: np.ndarray, ray_dir: np.ndarray, ground_z: float
) -> Tuple[Optional[np.ndarray], str, float]:
    """Intersecta el rayo con un plano horizontal de elevación ground_z."""
    dz = ray_dir[2]
    if dz >= -1e-4:
        return None, "invalid_ray_direction", 0.0

    s = (ground_z - c_xyz[2]) / dz
    if s <= 0:
        return None, "negative_range", s

    impact_pt = c_xyz + s * ray_dir
    return impact_pt, "plane_fallback", s


def compute_uncertainty_radius(
    slant_range_m: float,
    off_nadir_deg: float,
    gnss_acc_m: float,
    dem_used: bool,
    dem_vertical_acc_m: float = 2.0,
    slope_uncertainty_m: float = 35.0,
    gimbal_angular_err_rad: float = 0.035,  # ~2.0°
    ifov_px_rad: float = 0.0013,  # IFOV del píxel térmico (~1.3 mrad)
) -> Tuple[float, Dict[str, float]]:
    """
    Propagación rigurosa de incertidumbre posicional:
    sigma_pos = sqrt(sigma_GNSS^2 + (R * sigma_gimbal)^2 + (R * ifov)^2 + (sigma_Z * tan(off_nadir))^2)
    """
    rad = math.radians(off_nadir_deg)
    tan_off_nadir = math.tan(rad)

    e_gnss = max(0.5, gnss_acc_m)
    e_angular = slant_range_m * gimbal_angular_err_rad
    e_ifov = slant_range_m * ifov_px_rad

    sigma_z = dem_vertical_acc_m if dem_used else slope_uncertainty_m
    e_vertical_proj = sigma_z * tan_off_nadir

    total_sigma = math.sqrt(e_gnss**2 + e_angular**2 + e_ifov**2 + e_vertical_proj**2)

    components = {
        "e_gnss_m": round(e_gnss, 2),
        "e_angular_m": round(e_angular, 2),
        "e_vertical_proj_m": round(e_vertical_proj, 2),
    }
    return round(total_sigma, 1), components


def extract_metadata_from_geotiff(tif_path: str) -> Dict[str, Any]:
    """
    Extrae la pose de la cámara e intrínsecos de los metadatos GDAL / EXIF / XMP.
    """
    ds = gdal.Open(tif_path)
    if ds is None:
        return {}

    w = ds.RasterXSize
    h = ds.RasterYSize
    meta = ds.GetMetadata()
    exif_meta = ds.GetMetadata("EXIF") or {}
    all_meta = {**meta, **exif_meta}
    ds = None

    def get_float(keys: List[str], default: Optional[float] = None) -> Optional[float]:
        for k in keys:
            for m in (all_meta, meta, exif_meta):
                if k in m:
                    try:
                        return float(m[k])
                    except (ValueError, TypeError):
                        pass
        return default

    lat = get_float(["GPSLatitude", "EXIF_GPSLatitude", "Latitude"])
    lon = get_float(["GPSLongitude", "EXIF_GPSLongitude", "Longitude"])
    alt = get_float(["GPSAltitude", "EXIF_GPSAltitude", "AbsoluteAltitude", "Altitude"], default=0.0)
    rel_alt = get_float(["RelativeAltitude", "EXIF_RelativeAltitude"], default=None)

    yaw = get_float(["GimbalYawDegree", "FlightYawDegree"], default=0.0)
    pitch = get_float(["GimbalPitchDegree", "FlightPitchDegree"], default=-90.0)
    roll = get_float(["GimbalRollDegree", "FlightRollDegree"], default=0.0)

    rtk_lon = get_float(["RtkStdLon"], default=None)
    rtk_lat = get_float(["RtkStdLat"], default=None)
    if rtk_lon is not None and rtk_lat is not None:
        h_acc = max(rtk_lon, rtk_lat) * 2.0
    else:
        h_acc = 3.0  # GNSS convencional sin RTK

    # Distancia focal desde tags
    focal_mm = get_float(["FocalLength", "EXIF_FocalLength"], default=DEFAULT_FOCAL_MM)
    calib_focal = get_float(["CalibratedFocalLength", "XMP:CalibratedFocalLength"], default=None)

    if calib_focal is not None and calib_focal > 100.0:
        focal_px = calib_focal
    else:
        focal_px = (focal_mm * 1000.0) / DEFAULT_PIXEL_PITCH_UM

    # Si GDAL no guardó los tags EXIF directamente, intentar exiftool si está en el sistema
    if (lat is None or lon is None) and shutil.which("exiftool"):
        try:
            res = subprocess.run(
                ["exiftool", "-j", "-n", tif_path],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0:
                d = json.loads(res.stdout)[0]
                lat = float(d.get("GPSLatitude", lat or 0.0))
                lon = float(d.get("GPSLongitude", lon or 0.0))
                alt = float(d.get("GPSAltitude", alt or 0.0))
                rel_alt = float(d.get("RelativeAltitude", rel_alt or 0.0))
                yaw = float(d.get("GimbalYawDegree", yaw or 0.0))
                pitch = float(d.get("GimbalPitchDegree", pitch or -90.0))
                roll = float(d.get("GimbalRollDegree", roll or 0.0))
                if "RtkStdLon" in d and "RtkStdLat" in d:
                    h_acc = max(float(d["RtkStdLon"]), float(d["RtkStdLat"])) * 2.0
                if "CalibratedFocalLength" in d:
                    focal_px = float(d["CalibratedFocalLength"])
                elif "FocalLength" in d:
                    focal_px = (float(d["FocalLength"]) * 1000.0) / DEFAULT_PIXEL_PITCH_UM
        except Exception:
            pass

    return {
        "width": w,
        "height": h,
        "lat": lat,
        "lon": lon,
        "altitude": alt,
        "relative_altitude": rel_alt,
        "yaw": yaw,
        "pitch": pitch,
        "roll": roll,
        "h_acc": h_acc,
        "focal_px": focal_px,
    }


def deduplicate_hotspots(
    hotspots: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Agrupa detecciones del mismo foco observadas desde diferentes fotogramas
    (overlap) basadas en distancia espacial normalizada.

    Política de Incertidumbre:
    El sesgo GNSS, de yaw y del DEM son comunes al vuelo.
    Por tanto, NO se reduce sigma como 1/sqrt(n) por debajo del piso sistémico.
    """
    if not hotspots:
        return []

    clusters: List[List[Dict[str, Any]]] = []

    for h in hotspots:
        matched = False
        hx, hy = h["utm_x"], h["utm_y"]
        h_unc = h["uncertainty_radius_m"]

        for cl in clusters:
            cl_xs = [c["utm_x"] for c in cl]
            cl_ys = [c["utm_y"] for c in cl]
            cl_unc = max(c["uncertainty_radius_m"] for c in cl)
            cx, cy = float(np.mean(cl_xs)), float(np.mean(cl_ys))

            dist = math.hypot(hx - cx, hy - cy)
            thresh = max(h_unc, cl_unc, 8.0)

            if dist <= thresh:
                cl.append(h)
                matched = True
                break

        if not matched:
            clusters.append([h])

    consolidated = []
    for idx, cl in enumerate(clusters, 1):
        peak_temp = max(c["temp_max_c"] for c in cl)
        total_px = sum(c["pixel_count"] for c in cl)

        # Promedio ponderado espacial
        weights = [1.0 / (c["uncertainty_radius_m"] ** 2) for c in cl]
        w_sum = sum(weights)
        w_norm = [w / w_sum for w in weights]

        avg_lat = sum(c["lat"] * w for c, w in zip(cl, w_norm))
        avg_lon = sum(c["lon"] * w for c, w in zip(cl, w_norm))
        avg_elev = sum(c["elevation_m"] * w for c, w in zip(cl, w_norm))

        # Cota de incertidumbre sistémica: preserva el mejor error individual
        # con una leve ganancia estadística acotada al 85%, nunca colapsando a cero
        min_individual_unc = min(c["uncertainty_radius_m"] for c in cl)
        cl_sigma = max(
            min_individual_unc * 0.85, 3.5 if any(c["geometry_source"] == "external_dem" for c in cl) else 20.0
        )

        sources = [c["image_file"] for c in cl]
        sources_unique = sorted(list(set(sources)))
        methods = sorted(list({c["geometry_source"] for c in cl}))

        consolidated.append(
            {
                "id": idx,
                "lat": round(avg_lat, 6),
                "lon": round(avg_lon, 6),
                "elevation_m": round(avg_elev, 1),
                "temp_max_c": round(peak_temp, 1),
                "pixel_count_total": int(total_px),
                "n_observations": len(cl),
                "uncertainty_radius_m": round(cl_sigma, 1),
                "geometry_sources": methods,
                "images": sources_unique,
            }
        )

    consolidated.sort(key=lambda x: -x["temp_max_c"])
    return consolidated


def run_thermal_triage(
    input_dir: str,
    output_geojson: str,
    output_json: str,
    external_dem: Optional[str] = None,
    temp_threshold: float = DEFAULT_TEMP_THRESHOLD,
    min_pixels: int = DEFAULT_MIN_PIXELS,
    geoid_undulation_m: float = DEFAULT_GEOID_UNDULATION_M,
    dem_vertical_datum: str = "orthometric",
    focal_length_px_override: Optional[float] = None,
    ground_elevation_fallback: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Ejecuta el pipeline completo de Triage Nivel 0.
    """
    t0 = time.time()
    os.makedirs(os.path.dirname(os.path.abspath(output_geojson)), exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(output_json)), exist_ok=True)

    # Limpieza rigurosa de salidas previas para evitar confusión con datos viejos
    for p in (output_geojson, output_json):
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass

    # Si se especificó DEM externo pero el archivo no existe, reportar error explícito
    if external_dem:
        if not os.path.isfile(external_dem):
            err_msg = f"❌ ERROR: El DEM externo especificado no existe: {external_dem}"
            print(err_msg, file=sys.stderr)
            fail_doc = {
                "tier": "level0_tactical",
                "status": "failed",
                "error": err_msg,
                "hotspots_activos": 0,
                "hotspots": [],
            }
            with open(output_json, "w", encoding="utf-8") as f:
                json.dump(fail_doc, f, indent=2)
            sys.exit(1)

    # Buscar archivos TIFF térmicos Float32
    tif_files = sorted(glob.glob(os.path.join(input_dir, "*.tif")))
    if not tif_files:
        print(f"⚠ No se encontraron archivos .tif en {input_dir}")
        empty_doc = {
            "tier": "level0_tactical",
            "status": "empty",
            "hotspots_activos": 0,
            "hotspots": [],
        }
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(empty_doc, f, indent=2)
        return empty_doc

    print(f"🔥 Iniciando Triage Térmico Nivel 0 sobre {len(tif_files)} imágenes...")

    # Extraer metadatos de todas las imágenes para calcular la extensión de vuelo
    poses = []
    lons, lats = [], []
    for f in tif_files:
        m = extract_metadata_from_geotiff(f)
        if m.get("lat") is not None and m.get("lon") is not None:
            poses.append((f, m))
            lons.append(m["lon"])
            lats.append(m["lat"])

    if not poses:
        print("❌ Ninguna imagen térmica contiene metadatos de GPS.")
        empty_doc = {
            "tier": "level0_tactical",
            "status": "no_gps",
            "hotspots_activos": 0,
            "hotspots": [],
        }
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(empty_doc, f, indent=2)
        return empty_doc

    ref_lon, ref_lat = float(np.median(lons)), float(np.median(lats))
    epsg_utm = _get_utm_epsg(ref_lon, ref_lat)

    utm_srs = osr.SpatialReference()
    utm_srs.ImportFromEPSG(epsg_utm)
    utm_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    utm_proj_wkt = utm_srs.ExportToWkt()

    wgs84_srs = osr.SpatialReference()
    wgs84_srs.ImportFromEPSG(4326)
    wgs84_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    to_utm = osr.CoordinateTransformation(wgs84_srs, utm_srs)
    to_wgs84 = osr.CoordinateTransformation(utm_srs, wgs84_srs)

    # Calcular bounding box en UTM
    utm_coords = [to_utm.TransformPoint(lon, lat)[:2] for lon, lat in zip(lons, lats)]
    min_x = min(c[0] for c in utm_coords)
    max_x = max(c[0] for c in utm_coords)
    min_y = min(c[1] for c in utm_coords)
    max_y = max(c[1] for c in utm_coords)
    bbox_utm = (min_x, min_y, max_x, max_y)

    dem_reader: Optional[DEMReaderInMemory] = None
    if external_dem and os.path.isfile(external_dem):
        print(f"🏔 Cargando DEM/DSM en memoria ({external_dem})...")
        try:
            dem_reader = DEMReaderInMemory(
                dem_path=external_dem,
                target_crs_wkt=utm_proj_wkt,
                bbox_utm=bbox_utm,
                buffer_m=1500.0,
                geoid_undulation_m=geoid_undulation_m,
                vertical_datum=dem_vertical_datum,
            )
            print(
                f"  ✓ Ventana DEM cargada en memoria: {dem_reader.win_w}x{dem_reader.win_h} px "
                f"(res ~{dem_reader.dem_res:.1f} m, interpolación bilineal activa)"
            )
        except Exception as exc:
            print(f"  ❌ Error cargando ventana del DEM ({exc}) — pasando a fallback plano")
            dem_reader = None
    else:
        print("🗺 Sin DEM externo provisto — usando fallback a plano horizontal")

    raw_hotspots = []

    for tif_path, meta in poses:
        cx_utm, cy_utm, _ = to_utm.TransformPoint(meta["lon"], meta["lat"])
        alt_gnss = meta["altitude"]
        rel_alt = meta.get("relative_altitude")

        # Ajuste de datum vertical para la cámara si el DEM es ortométrico:
        # H = h - N. Si N = -24m, H = h + 24m.
        if dem_reader is not None and dem_vertical_datum == "orthometric":
            c_alt = alt_gnss - geoid_undulation_m
        else:
            c_alt = alt_gnss

        if ground_elevation_fallback is not None:
            ground_z = ground_elevation_fallback
        elif rel_alt is not None and rel_alt > 5.0:
            ground_z = c_alt - rel_alt
        else:
            ground_z = c_alt - 100.0

        c_xyz = np.array([cx_utm, cy_utm, c_alt], dtype=np.float64)

        # Leer raster térmico
        ds = gdal.Open(tif_path)
        arr = ds.GetRasterBand(1).ReadAsArray()
        ds = None

        valid_mask = np.isfinite(arr) & (arr >= temp_threshold)
        if not valid_mask.any():
            continue

        lbl, n_components = ndimage.label(valid_mask)
        w, h = meta["width"], meta["height"]

        # Focal en píxeles: override explícito, del EXIF, o default DJI M3T
        if focal_length_px_override is not None:
            fx = fy = focal_length_px_override
        else:
            fx = fy = meta.get("focal_px", DEFAULT_FOCAL_PX)

        for comp_id in range(1, n_components + 1):
            ys, xs = np.where(lbl == comp_id)
            if len(xs) < min_pixels:
                continue

            u_center = float(xs.mean())
            v_center = float(ys.mean())
            peak_temp = float(arr[ys, xs].max())

            ray_dir = pixel_to_ray_vector(
                u_center,
                v_center,
                w,
                h,
                fx,
                fy,
                meta["yaw"],
                meta["pitch"],
                meta["roll"],
            )

            if ray_dir[2] >= -1e-4:
                continue

            off_nadir_deg = math.degrees(math.acos(np.clip(-ray_dir[2], 0.0, 1.0)))

            impact_xyz = None
            method = "none"
            slant_range = 0.0

            if dem_reader is not None:
                impact_xyz, method, slant_range = intersect_ray_dem(c_xyz, ray_dir, dem_reader)

            if impact_xyz is None:
                impact_xyz, method, slant_range = intersect_ray_plane(c_xyz, ray_dir, ground_z)

            if impact_xyz is None:
                continue

            imp_lon, imp_lat, _ = to_wgs84.TransformPoint(impact_xyz[0], impact_xyz[1])

            unc_m, unc_components = compute_uncertainty_radius(
                slant_range_m=slant_range,
                off_nadir_deg=off_nadir_deg,
                gnss_acc_m=meta["h_acc"],
                dem_used=(method == "external_dem"),
            )

            raw_hotspots.append(
                {
                    "lat": imp_lat,
                    "lon": imp_lon,
                    "utm_x": impact_xyz[0],
                    "utm_y": impact_xyz[1],
                    "elevation_m": impact_xyz[2],
                    "temp_max_c": peak_temp,
                    "pixel_count": int(len(xs)),
                    "uncertainty_radius_m": unc_m,
                    "uncertainty_components": unc_components,
                    "off_nadir_deg": round(off_nadir_deg, 1),
                    "slant_range_m": round(slant_range, 1),
                    "geometry_source": method,
                    "image_file": os.path.basename(tif_path),
                }
            )

    print(f"  ✓ Detectadas {len(raw_hotspots)} observaciones brutas de focos")

    consolidated = deduplicate_hotspots(raw_hotspots)
    print(f"  ✓ Focos consolidados tras deduplicación: {len(consolidated)}")

    features = []
    for h in consolidated:
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [h["lon"], h["lat"], h["elevation_m"]],
                },
                "properties": {
                    "id": h["id"],
                    "temp_max_apparent_c": h["temp_max_c"],
                    "pixel_count_total": h["pixel_count_total"],
                    "n_observations": h["n_observations"],
                    "uncertainty_radius_m": h["uncertainty_radius_m"],
                    "geometry_sources": h["geometry_sources"],
                    "images": h["images"],
                    "tier": "Level 0 Tactical Triage (<2 min)",
                    "status": "preliminar_tactico",
                },
            }
        )

    geojson_doc = {
        "type": "FeatureCollection",
        "name": "triage_thermal_level0",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features,
    }

    with open(output_geojson, "w", encoding="utf-8") as f:
        json.dump(geojson_doc, f, indent=2, ensure_ascii=False)

    elapsed_sec = round(time.time() - t0, 1)
    peak_overall = max([h["temp_max_c"] for h in consolidated], default=None)

    situation_doc = {
        "tier": "level0_tactical",
        "status": "ok",
        "hotspots_activos": len(consolidated),
        "temp_max_c": peak_overall,
        "elapsed_seconds": elapsed_sec,
        "dem_utilizado": bool(dem_reader is not None),
        "external_dem_path": external_dem if dem_reader else None,
        "vertical_datum": dem_vertical_datum if dem_reader else "plane_relative",
        "hotspots": consolidated[:25],
    }

    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(situation_doc, f, indent=2, ensure_ascii=False)

    print(f"✅ Triage completado con éxito en {elapsed_sec}s")
    print(f"   GeoJSON: {output_geojson}")
    print(f"   Resumen: {output_json}")
    return situation_doc


def main():
    parser = argparse.ArgumentParser(description="Triage Térmico Nivel 0 para RAPTOR (<2 min)")
    parser.add_argument(
        "--input-dir",
        default="preprocessing/thermal_dji_sdk",
        help="Directorio con GeoTIFFs térmicos Float32",
    )
    parser.add_argument(
        "--external-dem",
        default=os.environ.get("EXTERNAL_DEM", None),
        help="Ruta opcional al GeoTIFF del DEM externo (e.g. 2m del Valle)",
    )
    parser.add_argument(
        "--output-geojson",
        default="outputs/triage_thermal_level0.geojson",
        help="Ruta del GeoJSON de salida",
    )
    parser.add_argument(
        "--output-json",
        default="outputs/situation_level0.json",
        help="Ruta del resumen JSON táctico",
    )
    parser.add_argument(
        "--temp-threshold",
        type=float,
        default=DEFAULT_TEMP_THRESHOLD,
        help="Umbral de temperatura activa en °C (default: 88.0)",
    )
    parser.add_argument(
        "--min-pixels",
        type=int,
        default=DEFAULT_MIN_PIXELS,
        help="Mínimo de píxeles para validar un foco (default: 9)",
    )
    parser.add_argument(
        "--dem-vertical-datum",
        choices=["orthometric", "ellipsoidal"],
        default="orthometric",
        help="Datum vertical del DEM (default: orthometric)",
    )
    parser.add_argument(
        "--geoid-undulation-m",
        type=float,
        default=DEFAULT_GEOID_UNDULATION_M,
        help=f"Ondulación geoidal N en metros (default: {DEFAULT_GEOID_UNDULATION_M} para Valle de Aburrá)",
    )
    parser.add_argument(
        "--focal-length-px",
        type=float,
        default=None,
        help="Override explícito de distancia focal en píxeles (default: 758.3 para DJI M3T)",
    )
    parser.add_argument(
        "--ground-elevation",
        type=float,
        default=None,
        help="Cota base estimada de terreno para fallback si no hay DEM",
    )

    args = parser.parse_args()

    if not os.path.isdir(args.input_dir) and os.path.isdir("outputs/thermal_converted"):
        args.input_dir = "outputs/thermal_converted"

    run_thermal_triage(
        input_dir=args.input_dir,
        output_geojson=args.output_geojson,
        output_json=args.output_json,
        external_dem=args.external_dem,
        temp_threshold=args.temp_threshold,
        min_pixels=args.min_pixels,
        geoid_undulation_m=args.geoid_undulation_m,
        dem_vertical_datum=args.dem_vertical_datum,
        focal_length_px_override=args.focal_length_px,
        ground_elevation_fallback=args.ground_elevation,
    )


if __name__ == "__main__":
    main()
