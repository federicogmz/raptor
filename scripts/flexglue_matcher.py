#!/usr/bin/env python3
"""FlexGlue + SIFT Ablation Feature Matcher and Pose Refinement Engine for RAPTOR / Zenith.

Produces versioned TiePointFile JSON schemas (v1.0) consumed by Zenith (ortho-rgb & ortho-thermal)
supporting:
  - Robust EXIF/XMP-derived pair graph construction with along-track (k_along=2) and
    cross-track (k_cross=4) flight line quotas.
  - Scale-invariant downsampled SIFT keypoint detection with 'corner_0.5_distorted_native_px'
    coordinate convention.
  - Radiometric dynamic fire hotspot masking (T > max(55.0, P95)) for thermal imagery.
  - Two controlled matchers: FlexGlue ONNX (with early stopping) and SIFT + USAC-MAGSAC ablation.
  - RGB-to-Thermal synchronized pose transfer with shared ENU origin and ambiguity resolution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import urllib.request

import cv2
import numpy as np

# Constantes del contrato de interoperabilidad con Zenith v1.0
SCHEMA_VERSION = "1.0"
COORDINATE_CONVENTION = "corner_0.5_distorted_native_px"

FLEXGLUE_RELEASE_BASE_URL = "https://github.com/cvg/FlexGlue/releases/download/v0.1/"

# Hashes SHA256 verificados oficialmente
FLEXGLUE_MODEL_HASHES: Dict[str, str] = {
    "flexglue_descriptor.onnx": "93fab8a4ccde1b7331640915357d96cff0929ea87134872f37f7b0150d9914ff",
    "flexglue_matcher.onnx": "a0baf84f00b32fc613206511a464fc3e71f4321e9d34df3a83362bd7d2a73012",
    "flexglue_matcher_fp16.onnx": "f81cb68f27367171728c2c51ea5b60acab348520bc12154d4a3befa79f2dfe11",
    "flexglue_matcher_early_stop.onnx": "c86714cd8ffac70c17ee48c346270497e21846831336e12d307390591aa12ea7",
    "flexglue_matcher_early_stop_fp16.onnx": "01286e692e6396cdfe6eaf5944d3af60752f5043275b338a6419a97efa339e20",
}


def get_default_model_dir() -> Path:
    """Retorna la ruta al directorio de caché de modelos ONNX de FlexGlue."""
    env_dir = os.environ.get("FLEXGLUE_MODEL_DIR")
    if env_dir:
        return Path(env_dir)
    return Path.home() / ".cache" / "flexglue" / "onnx"


def compute_sha256(file_path: Path) -> str:
    """Calcula el hash SHA256 de un archivo en bloques de 64 KB."""
    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            sha.update(chunk)
    return sha.hexdigest()


def download_model_if_needed(model_name: str, cache_dir: Optional[Path] = None) -> Path:
    """Descarga atómicamente un modelo ONNX de FlexGlue y valida su SHA256."""
    if model_name not in FLEXGLUE_MODEL_HASHES:
        raise ValueError(f"Modelo desconocido: '{model_name}'. Disponibles: {list(FLEXGLUE_MODEL_HASHES.keys())}")

    expected_hash = FLEXGLUE_MODEL_HASHES[model_name]
    target_dir = cache_dir or get_default_model_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / model_name

    if target_path.exists():
        current_hash = compute_sha256(target_path)
        if current_hash == expected_hash:
            return target_path
        print(f"⚠ Hash SHA256 inválido para {model_name} ({current_hash} != {expected_hash}). Re-descargando...")
        target_path.unlink()

    url = f"{FLEXGLUE_RELEASE_BASE_URL}{model_name}"
    part_path = target_dir / f"{model_name}.part"
    if part_path.exists():
        part_path.unlink()

    print(f"Descargando {model_name} desde {url}...")
    try:
        urllib.request.urlretrieve(url, part_path)
    except Exception as exc:
        if part_path.exists():
            part_path.unlink()
        raise RuntimeError(f"Fallo al descargar {model_name} desde {url}: {exc}") from exc

    downloaded_hash = compute_sha256(part_path)
    if downloaded_hash != expected_hash:
        part_path.unlink()
        raise ValueError(
            f"Fallo de verificación SHA256 para {model_name}: esperado {expected_hash}, obtenido {downloaded_hash}"
        )

    part_path.rename(target_path)
    return target_path


def get_onnx_execution_provider() -> Tuple[List[str], str]:
    """Detecta si CUDA está disponible en onnxruntime y retorna la lista de proveedores."""
    try:
        import onnxruntime as ort

        available = ort.get_available_providers()
        if "CUDAExecutionProvider" in available:
            return ["CUDAExecutionProvider", "CPUExecutionProvider"], "CUDA"
        return ["CPUExecutionProvider"], "CPU"
    except Exception:
        return ["CPUExecutionProvider"], "CPU"


# ═══════════════════════════════════════════════════════════════════
# 1. METADATOS Y TELEMETRÍA (EXIF/XMP)
# ═══════════════════════════════════════════════════════════════════


def parse_image_telemetry(img_path: Union[Path, str]) -> Dict[str, Any]:
    """Extrae posición GPS (lat, lon, alt), rumbo de vuelo/gimbal y marca temporal de una imagen.

    Utiliza escaneo binario rápido de XMP (DJI) y cabeceras EXIF sin decodificar el búfer de píxeles.
    """
    path = Path(img_path)
    info: Dict[str, Any] = {
        "filename": path.name,
        "path": str(path.resolve()),
        "lat": 0.0,
        "lon": 0.0,
        "alt": 50.0,
        "yaw": 0.0,
        "timestamp": 0.0,
        "timestamp_str": "",
        "valid_gps": False,
    }

    try:
        with open(path, "rb") as f:
            header = f.read(131072)  # Leer los primeros 128 KB para metadatos EXIF/XMP

        # Escaneo de XMP DJI
        # drone-dji:FlightYawDegree="+45.2" o GimbalYawDegree
        m_yaw = re.search(rb'drone-dji:FlightYawDegree="([+-]?\d+(?:\.\d+)?)"', header)
        if not m_yaw:
            m_yaw = re.search(rb'drone-dji:GimbalYawDegree="([+-]?\d+(?:\.\d+)?)"', header)
        if m_yaw:
            info["yaw"] = float(m_yaw.group(1))

        # Altitud relativa (AGL) o absoluta (MSL)
        m_rel_alt = re.search(rb'drone-dji:RelativeAltitude="([+-]?\d+(?:\.\d+)?)"', header)
        if m_rel_alt:
            info["alt"] = max(5.0, float(m_rel_alt.group(1)))
            info["alt_is_agl"] = True
            info["alt_source"] = "dji_relative_altitude_xmp"
        else:
            m_abs_alt = re.search(rb'drone-dji:AbsoluteAltitude="([+-]?\d+(?:\.\d+)?)"', header)
            if m_abs_alt:
                info["alt"] = float(m_abs_alt.group(1))
                info["alt_is_agl"] = False
                info["alt_source"] = "dji_absolute_altitude_xmp"
            else:
                info["alt_source"] = "fallback_default"

        # Timestamp DJI o EXIF
        m_time = re.search(rb"(\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2})", header)
        if m_time:
            t_str = m_time.group(1).decode("ascii", errors="ignore")
            info["timestamp_str"] = t_str
            try:
                tt = time.strptime(t_str, "%Y:%m:%d %H:%M:%S")
                info["timestamp"] = time.mktime(tt)
            except Exception:
                pass
    except Exception as exc:
        print(f"  ⚠ Aviso al escanear XMP de {path.name}: {exc}")

    # Fallback con PIL para latitud/longitud precisa si es necesario
    try:
        from PIL import Image
        from PIL.ExifTags import GPSTAGS, TAGS

        with Image.open(path) as img:
            exif = img._getexif()
            if exif:
                for tag, val in exif.items():
                    tag_name = TAGS.get(tag, str(tag))
                    if tag_name == "DateTimeOriginal" and not info["timestamp_str"]:
                        info["timestamp_str"] = str(val)
                        try:
                            tt = time.strptime(str(val), "%Y:%m:%d %H:%M:%S")
                            info["timestamp"] = time.mktime(tt)
                        except Exception:
                            pass
                    if tag_name == "GPSInfo":
                        gps_data = {}
                        for t, v in val.items():
                            gps_data[GPSTAGS.get(t, t)] = v

                        lat_raw = gps_data.get("GPSLatitude")
                        lat_ref = gps_data.get("GPSLatitudeRef", "N")
                        lon_raw = gps_data.get("GPSLongitude")
                        lon_ref = gps_data.get("GPSLongitudeRef", "E")

                        if lat_raw and lon_raw:

                            def to_deg(coords):
                                d = float(coords[0])
                                m = float(coords[1])
                                s = float(coords[2])
                                return d + (m / 60.0) + (s / 3600.0)

                            lat = to_deg(lat_raw)
                            if lat_ref != "N":
                                lat = -lat
                            lon = to_deg(lon_raw)
                            if lon_ref != "E":
                                lon = -lon
                            info["lat"] = lat
                            info["lon"] = lon
                            info["valid_gps"] = True

                        if "GPSAltitude" in gps_data and info.get("alt_source") == "fallback_default":
                            info["alt"] = float(gps_data["GPSAltitude"])
                            info["alt_is_agl"] = False
                            info["alt_source"] = "exif_gps_altitude_msl"
    except Exception:
        pass

    return info


# ═══════════════════════════════════════════════════════════════════
# 2. CONSTRUCCIÓN DEL GRAFO DE PARES CON CUOTA CROSS-FLIGHT-LINE
# ═══════════════════════════════════════════════════════════════════


def angular_difference_deg(yaw1: float, yaw2: float) -> float:
    """Calcula la diferencia angular absoluta mínima en [-180, 180] grados."""
    diff = (yaw1 - yaw2 + 180.0) % 360.0 - 180.0
    return abs(diff)


def build_pair_graph(
    image_metas: List[Dict[str, Any]],
    k_along: int = 2,
    k_cross: int = 4,
    max_yaw_delta_along: float = 35.0,
    fov_deg: float = 70.0,
) -> Tuple[List[Tuple[int, int]], List[float]]:
    """Construye un grafo de pares con cuotas de línea longitudinal (k_along) y transversal (k_cross).

    Retorna:
      (pairs, enu_origin) donde pairs es una lista de tuplas (i, j) con i < j únicas,
      y enu_origin es [lat0, lon0, alt0].
    """
    n = len(image_metas)
    if n < 2:
        return [], [0.0, 0.0, 0.0]

    # Calcular origen común ENU
    lats = [m["lat"] for m in image_metas if m.get("valid_gps")]
    lons = [m["lon"] for m in image_metas if m.get("valid_gps")]
    alts = [m["alt"] for m in image_metas]

    lat0 = float(np.mean(lats)) if lats else 0.0
    lon0 = float(np.mean(lons)) if lons else 0.0
    alt0 = float(np.min(alts)) if alts else 0.0
    enu_origin = [lat0, lon0, alt0]

    # Proyección plana ENU aproximada en metros
    cos_lat0 = math.cos(math.radians(lat0))
    pos_enu: List[Tuple[float, float, float]] = []
    footprint_radii: List[float] = []

    tan_half_fov = math.tan(math.radians(fov_deg / 2.0))

    for m in image_metas:
        if m.get("valid_gps"):
            dx = (m["lon"] - lon0) * cos_lat0 * 111319.5
            dy = (m["lat"] - lat0) * 111319.5
        else:
            dx = 0.0
            dy = 0.0

        raw_alt = float(m.get("alt", 50.0))
        # Prioridad absoluta a RelativeAltitude (AGL directo respecto al despegue)
        if m.get("alt_is_agl"):
            agl = max(10.0, raw_alt)
        else:
            # Fallback a altitud absoluta MSL: cota respecto a la mínima de vuelo + 50m survey AGL nominal
            agl = max(20.0, (raw_alt - alt0) + 50.0)

        r = agl * tan_half_fov
        pos_enu.append((dx, dy, agl))
        footprint_radii.append(r)

    pairs_set: Set[Tuple[int, int]] = set()

    for i in range(n):
        yaw_i = image_metas[i]["yaw"]
        x_i, y_i, _ = pos_enu[i]
        r_i = footprint_radii[i]

        # 1. Candidatos longitudinales (k_along): vecinos secuenciales con rumbo coherente
        along_candidates: List[Tuple[float, int]] = []
        for offset in [1, 2, 3]:
            for j in [i - offset, i + offset]:
                if 0 <= j < n and i != j:
                    delta_yaw = angular_difference_deg(yaw_i, image_metas[j]["yaw"])
                    if delta_yaw < max_yaw_delta_along:
                        dist = math.hypot(x_i - pos_enu[j][0], y_i - pos_enu[j][1])
                        along_candidates.append((dist, j))

        along_candidates.sort(key=lambda t: t[0])
        added_along = 0
        for _, j in along_candidates:
            pair = (min(i, j), max(i, j))
            if pair not in pairs_set:
                pairs_set.add(pair)
                added_along += 1
                if added_along >= k_along:
                    break

        # 2. Candidatos transversales (k_cross): líneas adyacentes/opuestas
        cross_candidates: List[Tuple[float, int]] = []
        for j in range(n):
            if i == j:
                continue
            delta_yaw = angular_difference_deg(yaw_i, image_metas[j]["yaw"])
            index_gap = abs(i - j)
            # Condición de vuelo transversal: rumbo opuesto/desviado (>= 35°) o salto de secuencia >= 5
            is_cross = (delta_yaw >= max_yaw_delta_along) or (index_gap >= 5)
            if is_cross:
                dist = math.hypot(x_i - pos_enu[j][0], y_i - pos_enu[j][1])
                r_j = footprint_radii[j]
                # Debe haber solape geométrico de huella teórica (dist < r_i + r_j) o cercanía espacial
                if dist < (r_i + r_j) * 1.5:
                    cross_candidates.append((dist, j))

        cross_candidates.sort(key=lambda t: t[0])
        added_cross = 0
        for _, j in cross_candidates:
            pair = (min(i, j), max(i, j))
            if pair not in pairs_set:
                pairs_set.add(pair)
                added_cross += 1
                if added_cross >= k_cross:
                    break

    sorted_pairs = sorted(list(pairs_set))
    return sorted_pairs, enu_origin


# ═══════════════════════════════════════════════════════════════════
# 3. CONVERSIÓN DE COORDENADAS Y DETECCIÓN DOWNSAMPLED SIFT
# ═══════════════════════════════════════════════════════════════════


def ds_to_native_corner(
    u_ds: float,
    v_ds: float,
    scale_x: float,
    scale_y: float,
) -> Tuple[float, float]:
    """Mapea coordenadas de centro de píxel de imagen reducida a 'corner_0.5_distorted_native_px'.

    Convención matemática:
      u_native = (u_ds + 0.5) * scale_x
      v_native = (v_ds + 0.5) * scale_y
    """
    return (float(u_ds) + 0.5) * scale_x, (float(v_ds) + 0.5) * scale_y


def native_corner_to_ds(
    u_native: float,
    v_native: float,
    scale_x: float,
    scale_y: float,
) -> Tuple[float, float]:
    """Inverso exacto para validación de contrato bidireccional (error < 1e-6)."""
    return (float(u_native) / scale_x) - 0.5, (float(v_native) / scale_y) - 0.5


def extract_downsampled_sift(
    img_bgr: np.ndarray,
    max_dim: int = 1600,
    nfeatures: int = 1500,
    mask: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, np.ndarray, Tuple[float, float]]:
    """Extrae keypoints SIFT en resolución reducida y proyecta a escala nativa distorted con +0.5.

    Retorna:
      (kps_native, descriptors, (scale_x, scale_y))
    """
    h_orig, w_orig = img_bgr.shape[:2]
    max_orig = max(h_orig, w_orig)

    if max_orig > max_dim:
        scale_factor = float(max_dim) / float(max_orig)
        w_ds = max(16, int(round(w_orig * scale_factor)))
        h_ds = max(16, int(round(h_orig * scale_factor)))
        img_ds = cv2.resize(img_bgr, (w_ds, h_ds), interpolation=cv2.INTER_AREA)
        mask_ds = cv2.resize(mask, (w_ds, h_ds), interpolation=cv2.INTER_NEAREST) if mask is not None else None
    else:
        w_ds, h_ds = w_orig, h_orig
        img_ds = img_bgr
        mask_ds = mask

    scale_x = float(w_orig) / float(w_ds)
    scale_y = float(h_orig) / float(h_ds)

    gray_ds = cv2.cvtColor(img_ds, cv2.COLOR_BGR2GRAY) if len(img_ds.shape) == 3 else img_ds
    sift = cv2.SIFT_create(nfeatures=nfeatures)
    kps, des = sift.detectAndCompute(gray_ds, mask=mask_ds)

    if not kps:
        return np.empty((0, 2), dtype=np.float64), np.empty((0, 128), dtype=np.float32), (scale_x, scale_y)

    # Conversión estricta a escala nativa por ejes separados
    kps_native = np.zeros((len(kps), 2), dtype=np.float64)
    for idx, kp in enumerate(kps):
        un, vn = ds_to_native_corner(kp.pt[0], kp.pt[1], scale_x, scale_y)
        kps_native[idx, 0] = un
        kps_native[idx, 1] = vn

    return kps_native, des, (scale_x, scale_y)


# ═══════════════════════════════════════════════════════════════════
# 4. ENMASCARAMIENTO RADIOMÉTRICO Y DETECCIÓN TÉRMICA
# ═══════════════════════════════════════════════════════════════════


def mask_and_normalize_thermal(
    thermal_deg_c: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray, float, float, float]:
    """Normaliza un arreglo de temperatura float32 (°C) a uint8 [0, 255] en el rango [P2, P95].

    Enmascara dinámicamente puntos calientes de fuego (T > max(55.0, P95)) mediante
    dilatación morfológica para que ninguna llama o pluma convectiva actúe como punto de enlace.

    Retorna:
      (norm_uint8, valid_terrain_mask, p2, p95, t_hotspot)
    """
    valid_pixels = thermal_deg_c[np.isfinite(thermal_deg_c) & (thermal_deg_c > -50.0) & (thermal_deg_c < 2000.0)]
    if len(valid_pixels) == 0:
        p2, p95 = 20.0, 40.0
    else:
        p2 = float(np.percentile(valid_pixels, 2.0))
        p95 = float(np.percentile(valid_pixels, 95.0))

    t_hotspot = max(55.0, p95)

    # Máscara binaria de focos de calor a excluir
    hotspot_mask = (thermal_deg_c > t_hotspot).astype(np.uint8)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    dilated_hotspot = cv2.dilate(hotspot_mask, kernel)

    # Terreno válido: no es foco térmico ni valor corrupto
    valid_terrain = (dilated_hotspot == 0) & (thermal_deg_c >= p2 - 10.0) & (thermal_deg_c <= t_hotspot)
    valid_terrain_mask = valid_terrain.astype(np.uint8) * 255

    clipped = np.clip(thermal_deg_c, p2, p95)
    rng = max(1e-3, p95 - p2)
    norm_uint8 = np.clip((clipped - p2) / rng * 255.0, 0.0, 255.0).astype(np.uint8)

    return norm_uint8, valid_terrain_mask, p2, p95, t_hotspot


def extract_thermal_keypoints(
    thermal_data: np.ndarray,
    nfeatures: int = 1500,
    min_points_threshold: int = 200,
) -> Tuple[np.ndarray, np.ndarray, Tuple[float, float]]:
    """Extrae puntos de enlace sobre imágenes térmicas garantizando exclusión absoluta de focos de fuego.

    Si SIFT detecta menos de min_points_threshold puntos en terreno de bajo contraste, enriquece
    con una grilla dispersa de puntos de alto gradiente Sobel en terreno estático.
    """
    h, w = thermal_data.shape[:2]
    norm_uint8, terrain_mask, _, _, t_hotspot = mask_and_normalize_thermal(thermal_data)

    kps_native, des, scales = extract_downsampled_sift(
        norm_uint8,
        max_dim=1600,
        nfeatures=nfeatures,
        mask=terrain_mask,
    )

    # Invariante I2: Verificar que ningún keypoint caiga en zona de fuego
    if len(kps_native) > 0:
        valid_indices = []
        for i in range(len(kps_native)):
            u, v = kps_native[i]
            px, py = int(round(u - 0.5)), int(round(v - 0.5))
            if 0 <= py < h and 0 <= px < w:
                if thermal_data[py, px] <= t_hotspot and terrain_mask[py, px] > 0:
                    valid_indices.append(i)
        if len(valid_indices) < len(kps_native):
            kps_native = kps_native[valid_indices]
            des = des[valid_indices] if des is not None else None

    # Respaldo: grilla dispersa de alto gradiente si SIFT halla menos de 200 puntos
    if len(kps_native) < min_points_threshold:
        gx = cv2.Sobel(norm_uint8, cv2.CV_32F, 1, 0)
        gy = cv2.Sobel(norm_uint8, cv2.CV_32F, 0, 1)
        grad_mag = np.hypot(gx, gy)
        grad_mag[terrain_mask == 0] = 0.0

        step = 24
        grid_kps = []
        for gy_coord in range(step // 2, h, step):
            for gx_coord in range(step // 2, w, step):
                if terrain_mask[gy_coord, gx_coord] > 0 and grad_mag[gy_coord, gx_coord] > 15.0:
                    grid_kps.append(cv2.KeyPoint(float(gx_coord), float(gy_coord), 10.0))

        if grid_kps:
            sift = cv2.SIFT_create()
            grid_kps, grid_des = sift.compute(norm_uint8, grid_kps)
            if grid_kps and grid_des is not None:
                extra_kps = np.array([[kp.pt[0] + 0.5, kp.pt[1] + 0.5] for kp in grid_kps], dtype=np.float64)
                if len(kps_native) == 0:
                    kps_native = extra_kps
                    des = grid_des
                else:
                    kps_native = np.vstack([kps_native, extra_kps])
                    des = np.vstack([des, grid_des])

    return kps_native, des, scales


# ═══════════════════════════════════════════════════════════════════
# 5. MATCHERS: SIFT + USAC-MAGSAC Y FLEXGLUE ONNX
# ═══════════════════════════════════════════════════════════════════


def match_pair_sift(
    kp_i: np.ndarray,
    des_i: np.ndarray,
    kp_j: np.ndarray,
    des_j: np.ndarray,
    ratio_threshold: float = 0.75,
    max_reproj_err_px: float = 3.0,
) -> Optional[Dict[str, Any]]:
    """Ejecuta la línea base de ablación SIFT clásica:

    BFMatcher(L2) + Lowe ratio test (0.75) + cv2.findFundamentalMat(USAC_MAGSAC, 3.0 px).
    """
    if des_i is None or des_j is None or len(des_i) < 8 or len(des_j) < 8:
        return None

    matcher = cv2.BFMatcher(cv2.NORM_L2, crossCheck=False)
    try:
        raw_matches = matcher.knnMatch(des_i, des_j, k=2)
    except Exception:
        return None

    good_matches = []
    for pair in raw_matches:
        if len(pair) == 2 and pair[0].distance < ratio_threshold * pair[1].distance:
            good_matches.append(pair[0])

    if len(good_matches) < 8:
        return None

    pts_i = np.float32([kp_i[m.queryIdx] for m in good_matches])
    pts_j = np.float32([kp_j[m.trainIdx] for m in good_matches])

    f_mat, inlier_mask = cv2.findFundamentalMat(
        pts_i,
        pts_j,
        cv2.USAC_MAGSAC,
        max_reproj_err_px,
        0.99,
        2000,
    )

    if f_mat is None or inlier_mask is None:
        return None

    inliers_idx = [idx for idx, mask_val in enumerate(inlier_mask.ravel()) if mask_val == 1]
    if len(inliers_idx) < 8:
        return None

    inlier_pairs = []
    inlier_scores = []
    for idx in inliers_idx:
        m = good_matches[idx]
        inlier_pairs.append([int(m.queryIdx), int(m.trainIdx)])
        # Score normalizado proporcional a la proximidad en el descriptor
        score = max(0.01, min(1.0, 1.0 - (m.distance / 512.0)))
        inlier_scores.append(round(score, 4))

    return {
        "num_inliers": len(inliers_idx),
        "inlier_matches": inlier_pairs,
        "inlier_scores": inlier_scores,
    }


class FlexGlueEngine:
    """Motor de inferencia ONNX para FlexGlue con selector dinámico CPU/CUDA y early stopping."""

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir or get_default_model_dir()
        self.providers, self.device = get_onnx_execution_provider()
        self.desc_session = None
        self.matcher_session = None
        self.warmup_s = 0.0

    def load_and_warmup(self):
        """Descarga atómica (si se requiere), inicialización de sesiones ONNX y warmup timing."""
        import onnxruntime as ort

        t0 = time.time()
        desc_path = download_model_if_needed("flexglue_descriptor.onnx", self.cache_dir)
        matcher_name = (
            "flexglue_matcher_early_stop_fp16.onnx"
            if "CUDAExecutionProvider" in self.providers
            else "flexglue_matcher_early_stop.onnx"
        )
        matcher_path = download_model_if_needed(matcher_name, self.cache_dir)

        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self.desc_session = ort.InferenceSession(str(desc_path), opts, providers=self.providers)
        self.matcher_session = ort.InferenceSession(str(matcher_path), opts, providers=self.providers)

        # Warmup forward pass con tensor realista 512x512
        dummy_img = np.zeros((1, 3, 512, 512), dtype=np.float32)
        desc_inputs = self.desc_session.get_inputs()
        _ = self.desc_session.run(None, {desc_inputs[0].name: dummy_img})

        self.warmup_s = time.time() - t0

    def compute_descriptor(self, img_bgr: np.ndarray) -> np.ndarray:
        """Calcula el mapa denso [1, 256, 64, 64] reescalando bilinealmente a 512x512."""
        if self.desc_session is None:
            self.load_and_warmup()

        img_512 = cv2.resize(img_bgr, (512, 512), interpolation=cv2.INTER_LINEAR)
        img_rgb = cv2.cvtColor(img_512, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        # Transponer a [1, 3, 512, 512]
        tensor = np.transpose(img_rgb, (2, 0, 1))[np.newaxis, ...]

        desc_inputs = self.desc_session.get_inputs()
        desc_out = self.desc_session.run(None, {desc_inputs[0].name: tensor})
        return desc_out[0]  # [1, 256, 64, 64]

    def match_pair(
        self,
        kp_i: np.ndarray,
        desc_i: np.ndarray,
        kp_j: np.ndarray,
        desc_j: np.ndarray,
        filter_threshold: float = 0.15,
        min_covisibility: float = 0.35,
    ) -> Optional[Dict[str, Any]]:
        """Inferencia del Matcher Transformer de FlexGlue con umbrales de covisibilidad."""
        if self.matcher_session is None:
            self.load_and_warmup()

        if len(kp_i) < 8 or len(kp_j) < 8:
            return None

        matcher_inputs = self.matcher_session.get_inputs()
        input_dict = {}

        # Mapeo dinámico de inputs según nombres de la sesión ONNX exportada con soporte FP16
        for inp in matcher_inputs:
            name = inp.name.lower()
            is_zero = ("0" in name) or ("_a" in name) or ("src" in name)
            is_one = ("1" in name) or ("_b" in name) or ("dst" in name)
            is_kp = any(k in name for k in ("kpt", "kp", "point", "coord"))
            is_desc = any(k in name for k in ("desc", "feat", "map"))

            dtype = np.float16 if inp.type == "tensor(float16)" else np.float32

            if is_kp:
                kps = kp_i if (is_zero or not is_one) else kp_j
                input_dict[inp.name] = kps[np.newaxis, ...].astype(dtype)
            elif is_desc:
                desc = desc_i if (is_zero or not is_one) else desc_j
                input_dict[inp.name] = desc.astype(dtype)
            else:
                if is_zero or not is_one:
                    input_dict[inp.name] = desc_i.astype(dtype)
                else:
                    input_dict[inp.name] = desc_j.astype(dtype)

        try:
            outputs = self.matcher_session.run(None, input_dict)
            covisibility = float(outputs[0]) if len(outputs) > 0 else 0.5
            if covisibility < min_covisibility:
                return None  # Early stop

            matches_raw = outputs[1] if len(outputs) > 1 else np.array([])
            scores_raw = outputs[2] if len(outputs) > 2 else np.array([])

            inliers = []
            scores = []
            for m_idx, score in enumerate(scores_raw):
                if score >= filter_threshold:
                    inliers.append([int(matches_raw[m_idx, 0]), int(matches_raw[m_idx, 1])])
                    scores.append(round(float(score), 4))

            if len(inliers) < 8:
                return None

            return {
                "num_inliers": len(inliers),
                "inlier_matches": inliers,
                "inlier_scores": scores,
            }
        except Exception as exc:
            print(f"  ⚠ Aviso en matcher ONNX FlexGlue: {exc}")
            return None


# ═══════════════════════════════════════════════════════════════════
# 6. TRANSFERENCIA DE POSES RGB -> TÉRMICA (SINCRONIZACIÓN DJI)
# ═══════════════════════════════════════════════════════════════════


def is_zoom_image(name: str) -> bool:
    """Detecta de forma robusta e insensible a mayúsculas fotogramas de lente Zoom."""
    lname = Path(name).name.lower()
    stem = Path(name).stem.lower()
    return stem.endswith("_z") or "_z_" in stem or lname.endswith(("_z.jpg", "_z.jpeg", "_z.png"))


def transfer_poses_to_thermal(
    refined_rgb_poses_path: Union[Path, str],
    thermal_images_dir: Union[Path, str],
    output_tie_points_path: Union[Path, str],
    enu_origin: Optional[List[float]] = None,
) -> Dict[str, Any]:
    """Empareja fotogramas térmicos (*_T.JPG) con sus homólogos RGB refinados (*_V o *_W).

    Invariantes respetados:
      - Nunca se empareja con lentes Zoom (_Z.JPG, insensible a mayúsculas).
      - Resolución de ambigüedades determinista (nombre exacto -> secuencia con validación temporal <= 1.0s).
      - Detección estricta de colisiones entre vuelos/carpetas que reinician numeración.
      - Mismo origen ENU que el vuelo visual para evitar desfases espaciales en ortho-thermal.
      - Supuesto de boresight documentado explícitamente en 'diagnostics'.
    """
    poses_path = Path(refined_rgb_poses_path)
    if not poses_path.exists():
        raise FileNotFoundError(f"No existe el archivo de poses RGB refinadas: {poses_path}")

    with open(poses_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Normalizar poses a diccionario {filename: pose_dict}
    rgb_poses: Dict[str, Dict[str, Any]] = {}
    if isinstance(data, dict):
        if "pose_overrides" in data and isinstance(data["pose_overrides"], list):
            for item in data["pose_overrides"]:
                name = item.get("frame_id") or item.get("image") or item.get("filename")
                if name:
                    rgb_poses[Path(name).name] = item
        elif "poses" in data and isinstance(data["poses"], list):
            for item in data["poses"]:
                name = item.get("image") or item.get("filename") or item.get("name") or item.get("frame_id")
                if name:
                    rgb_poses[Path(name).name] = item
        else:
            for k, v in data.items():
                if isinstance(v, dict):
                    rgb_poses[Path(k).name] = v
    elif isinstance(data, list):
        for item in data:
            name = item.get("frame_id") or item.get("image") or item.get("filename") or item.get("name")
            if name:
                rgb_poses[Path(name).name] = item

    if not rgb_poses:
        raise ValueError(f"Formato no reconocido o archivo vacío en poses RGB refinadas: {poses_path}")

    t_dir = Path(thermal_images_dir)
    raw_thermal_files = list(t_dir.glob("*_T.JPG")) + list(t_dir.glob("*_T.jpg")) + list(t_dir.glob("*_T.jpeg"))
    seen_names = set()
    thermal_files = []
    for tf in sorted(raw_thermal_files):
        if tf.name not in seen_names:
            seen_names.add(tf.name)
            thermal_files.append(tf)

    # Pre-escanear telemetría de las térmicas si se requiere emparejamiento temporal
    thermal_metas = [parse_image_telemetry(p) for p in thermal_files]
    rgb_metas: Dict[str, Dict[str, Any]] = {name: parse_image_telemetry(t_dir / name) for name in rgb_poses}

    pose_overrides = []
    unmatched_count = 0
    collision_count = 0

    for idx, t_file in enumerate(thermal_files):
        stem = t_file.stem  # ej: DJI_0042_T
        base_prefix = stem[:-2] if stem.endswith("_T") else stem

        matched_rgb_name = None

        # 1. Emparejamiento por nombre exacto (_V o _W, nunca _Z)
        candidate_v = f"{base_prefix}_V{t_file.suffix}"
        candidate_w = f"{base_prefix}_W{t_file.suffix}"
        candidate_plain = f"{base_prefix}{t_file.suffix}"

        if candidate_v in rgb_poses and not is_zoom_image(candidate_v):
            matched_rgb_name = candidate_v
        elif candidate_w in rgb_poses and not is_zoom_image(candidate_w):
            matched_rgb_name = candidate_w
        elif candidate_plain in rgb_poses and not is_zoom_image(candidate_plain):
            matched_rgb_name = candidate_plain

        # 2. Emparejamiento por secuencia DJI de 4 dígitos anclada en la misma carpeta
        if not matched_rgb_name:
            m_seq = re.search(r"(\d{4})_T", t_file.name) or re.search(r"DJI_(\d{4})", stem)
            if m_seq:
                seq_digits = m_seq.group(1)
                matching_seq = [name for name in rgb_poses if seq_digits in name and not is_zoom_image(name)]
                if len(matching_seq) == 1:
                    t_cand = matching_seq[0]
                    # Validación temporal anti-colisión si ambos tienen timestamp
                    t_time = thermal_metas[idx]["timestamp"]
                    r_time = rgb_metas.get(t_cand, {}).get("timestamp", 0.0)
                    if t_time > 0 and r_time > 0 and abs(t_time - r_time) > 1.0:
                        collision_count += 1
                    else:
                        matched_rgb_name = t_cand
                elif len(matching_seq) > 1:
                    collision_count += 1

        # 3. Emparejamiento por proximidad temporal EXIF (<= 1.0 s, rechaza empates ambiguos)
        if not matched_rgb_name and thermal_metas[idx]["timestamp"] > 0:
            t_time = thermal_metas[idx]["timestamp"]
            temporal_candidates = []
            for r_name, r_meta in rgb_metas.items():
                if is_zoom_image(r_name):
                    continue
                r_time = r_meta.get("timestamp", 0.0)
                if r_time > 0 and abs(t_time - r_time) <= 1.0:
                    temporal_candidates.append((abs(t_time - r_time), r_name))

            temporal_candidates.sort(key=lambda x: x[0])
            if len(temporal_candidates) == 1:
                matched_rgb_name = temporal_candidates[0][1]
            elif len(temporal_candidates) > 1:
                # Si hay empate estricto en diferencia temporal, rechazar por ambigüedad
                if abs(temporal_candidates[0][0] - temporal_candidates[1][0]) > 0.1:
                    matched_rgb_name = temporal_candidates[0][1]
                else:
                    collision_count += 1

        if matched_rgb_name:
            source_pose = rgb_poses[matched_rgb_name]
            dx = float(source_pose.get("delta_x_m", 0.0))
            dy = float(source_pose.get("delta_y_m", 0.0))
            dyaw = float(source_pose.get("delta_yaw_deg", 0.0))
            pos = source_pose.get("position", [0.0, 0.0, 0.0])
            yaw = source_pose.get("yaw_deg", 0.0)
            pitch = source_pose.get("pitch_deg", -90.0)
            roll = source_pose.get("roll_deg", 0.0)

            # Cumple tanto FramePoseDelta de Zenith Rust como compatibilidad con formato extendido
            pose_overrides.append(
                {
                    "frame_id": t_file.name,
                    "image": t_file.name,
                    "delta_x_m": dx,
                    "delta_y_m": dy,
                    "delta_yaw_deg": dyaw,
                    "source_rgb": matched_rgb_name,
                    "position": pos,
                    "yaw_deg": yaw,
                    "pitch_deg": pitch,
                    "roll_deg": roll,
                }
            )
        else:
            unmatched_count += 1

    # Invariante I4: Compartir y verificar el origen ENU del vuelo
    if not enu_origin:
        enu_origin = data.get("enu_origin", [0.0, 0.0, 0.0])
    assert len(enu_origin) == 3, f"enu_origin inválido: {enu_origin}"

    tie_point_file: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "matcher": "pose_transfer",
        "coordinate_convention": COORDINATE_CONVENTION,
        "enu_origin": enu_origin,
        "matches": [],
        "pose_overrides": pose_overrides,
        "timing_breakdown": {
            "warmup_s": 0.0,
            "keypoint_extract_s": 0.0,
            "descriptor_s": 0.0,
            "matcher_s": 0.0,
            "total_matching_s": 0.0,
            "peak_vram_mb": 0.0,
        },
        "diagnostics": {
            "total_thermal_images": len(thermal_files),
            "matched_thermal_count": len(pose_overrides),
            "unmatched_thermal": unmatched_count,
            "collision_count": collision_count,
            "boresight_assumption": "identity_lever_arm",
            "excluded_zoom_camera": True,
        },
    }

    out_p = Path(output_tie_points_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    with open(out_p, "w", encoding="utf-8") as f:
        json.dump(tie_point_file, f, indent=2)

    return tie_point_file


# ═══════════════════════════════════════════════════════════════════
# 7. PIPELINE MAESTRO DE EMPAREJAMIENTO (RGB / TÉRMICO)
# ═══════════════════════════════════════════════════════════════════


def run_feature_matching_pipeline(
    images_dir: Union[Path, str],
    output_tie_points_path: Union[Path, str],
    matcher_type: str = "flexglue",
    is_thermal: bool = False,
    raw_dir: Optional[Union[Path, str]] = None,
    max_dim: int = 1600,
    k_along: int = 2,
    k_cross: int = 4,
) -> Dict[str, Any]:
    """Ejecuta el pipeline completo de emparejamiento para un conjunto de imágenes (RGB o térmicas)."""
    t_start = time.time()
    images_p = Path(images_dir)
    out_p = Path(output_tie_points_path)

    # 1. Clasificar y listar imágenes
    valid_exts = {".jpg", ".jpeg", ".JPG", ".JPEG"}
    image_paths = sorted([p for p in images_p.iterdir() if p.suffix in valid_exts and p.is_file()])
    if not image_paths:
        raise ValueError(f"No se hallaron imágenes válidas en {images_p}")

    # Filtrar imágenes zoom si existieran
    image_paths = [p for p in image_paths if not p.name.endswith("_Z.JPG") and not p.name.endswith("_Z.jpg")]

    # Filtrar por canal (térmico o visual RGB) para directorios mixtos
    if is_thermal:
        image_paths = [p for p in image_paths if p.stem.endswith("_T") or "_T_" in p.stem]
        if not image_paths:
            raise ValueError(f"No se hallaron imágenes térmicas (*_T.JPG) en {images_p}")
    else:
        # En modo visual RGB, excluir fotogramas térmicos
        filtered_rgb = [p for p in image_paths if not (p.stem.endswith("_T") or "_T_" in p.stem)]
        if filtered_rgb:
            image_paths = filtered_rgb

    # 2. Metadatos de telemetría y grafo de pares
    metas = [parse_image_telemetry(p) for p in image_paths]
    pairs, enu_origin = build_pair_graph(metas, k_along=k_along, k_cross=k_cross)

    # 3. Inicializar matcher
    flexglue_engine = None
    warmup_s = 0.0
    matcher_device = "CPU"
    if matcher_type.lower() == "flexglue":
        flexglue_engine = FlexGlueEngine()
        flexglue_engine.load_and_warmup()
        warmup_s = flexglue_engine.warmup_s
        matcher_device = flexglue_engine.device

    # 4. Extracción de keypoints y descriptores densos en un solo paso con liberación inmediata de RAM (C4)
    t_kp_start = time.time()
    t_desc_total = 0.0
    all_kps: List[np.ndarray] = []
    all_des: List[Optional[np.ndarray]] = []
    all_flexglue_features: List[Optional[np.ndarray]] = []
    unreadable_images: List[str] = []

    for idx, img_path in enumerate(image_paths):
        if is_thermal:
            # Buscar archivo float32 .raw correspondiente si existe
            raw_path = None
            if raw_dir:
                candidate = Path(raw_dir) / f"{img_path.stem}.raw"
                if candidate.exists():
                    raw_path = candidate

            if raw_path:
                thermal_buf = np.fromfile(raw_path, dtype=np.float32)
                # Formato nominal térmico DJI 512x640
                if len(thermal_buf) == 512 * 640:
                    thermal_data = thermal_buf.reshape((512, 640))
                else:
                    thermal_data = thermal_buf.reshape((int(math.sqrt(len(thermal_buf))), -1))
            else:
                img_bgr = cv2.imread(str(img_path), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
                if img_bgr is None:
                    unreadable_images.append(img_path.name)
                    all_kps.append(np.empty((0, 2), dtype=np.float64))
                    all_des.append(np.empty((0, 128), dtype=np.float32))
                    all_flexglue_features.append(None)
                    continue
                thermal_data = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
                img_bgr = None

            kps, des, _ = extract_thermal_keypoints(thermal_data)
            all_kps.append(kps)
            all_des.append(des)

            if flexglue_engine:
                t0_d = time.time()
                norm_uint8, _, _, _, _ = mask_and_normalize_thermal(thermal_data)
                th_bgr = cv2.cvtColor(norm_uint8, cv2.COLOR_GRAY2BGR)
                feat = flexglue_engine.compute_descriptor(th_bgr)
                all_flexglue_features.append(feat)
                t_desc_total += time.time() - t0_d
                th_bgr = None
            else:
                all_flexglue_features.append(None)
        else:
            img_bgr = cv2.imread(str(img_path), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
            if img_bgr is None:
                unreadable_images.append(img_path.name)
                all_kps.append(np.empty((0, 2), dtype=np.float64))
                all_des.append(np.empty((0, 128), dtype=np.float32))
                all_flexglue_features.append(None)
                continue

            kps, des, _ = extract_downsampled_sift(img_bgr, max_dim=max_dim, nfeatures=1500)
            all_kps.append(kps)
            all_des.append(des)

            if flexglue_engine:
                t0_d = time.time()
                feat = flexglue_engine.compute_descriptor(img_bgr)
                all_flexglue_features.append(feat)
                t_desc_total += time.time() - t0_d
            else:
                all_flexglue_features.append(None)

            img_bgr = None  # Liberar memoria de imagen 45MP inmediatamente para evitar OOM

    keypoint_extract_s = max(0.0, time.time() - t_kp_start - t_desc_total)
    descriptor_s = t_desc_total

    # 5. Emparejamiento de pares con control estricto de inliers y formato TiePointMatch compacto (C5)
    t_match_start = time.time()
    matches_output: List[Dict[str, Any]] = []
    total_inliers = 0

    for i, j in pairs:
        name_i = image_paths[i].name
        name_j = image_paths[j].name

        res = None
        if flexglue_engine and all_flexglue_features[i] is not None and all_flexglue_features[j] is not None:
            res = flexglue_engine.match_pair(
                all_kps[i],
                all_flexglue_features[i],
                all_kps[j],
                all_flexglue_features[j],
            )
        else:
            # Línea base SIFT
            res = match_pair_sift(all_kps[i], all_des[i], all_kps[j], all_des[j])

        if res:
            total_inliers += res["num_inliers"]
            covis = res.get("covisibility", 1.0)
            for m_pair, m_score in zip(res["inlier_matches"], res["inlier_scores"]):
                idx_i, idx_j = m_pair
                pt_i = all_kps[i][idx_i]
                pt_j = all_kps[j][idx_j]
                matches_output.append(
                    {
                        "frame_i": name_i,
                        "frame_j": name_j,
                        "u_i": round(float(pt_i[0]), 3),
                        "v_i": round(float(pt_i[1]), 3),
                        "u_j": round(float(pt_j[0]), 3),
                        "v_j": round(float(pt_j[1]), 3),
                        "score": round(float(m_score), 4),
                        "covisibility": round(float(covis), 4),
                    }
                )

    matcher_s = time.time() - t_match_start
    total_matching_s = time.time() - t_start

    # Diagnóstico de VRAM si torch está disponible
    peak_vram_mb = 0.0
    try:
        import torch

        if torch.cuda.is_available():
            peak_vram_mb = round(float(torch.cuda.max_memory_allocated() / (1024 * 1024)), 2)
    except Exception:
        pass

    result: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "matcher": matcher_type.lower(),
        "coordinate_convention": COORDINATE_CONVENTION,
        "enu_origin": enu_origin,
        "matches": matches_output,
        "pose_overrides": [],
        "timing_breakdown": {
            "warmup_s": round(warmup_s, 4),
            "keypoint_extract_s": round(keypoint_extract_s, 4),
            "descriptor_s": round(descriptor_s, 4),
            "matcher_s": round(matcher_s, 4),
            "total_matching_s": round(total_matching_s, 4),
            "peak_vram_mb": peak_vram_mb,
        },
        "diagnostics": {
            "num_images": len(image_paths),
            "num_pairs_evaluated": len(pairs),
            "num_pairs_matched": len(set((m["frame_i"], m["frame_j"]) for m in matches_output)),
            "total_inliers": total_inliers,
            "device": matcher_device,
            "is_thermal": is_thermal,
            "unreadable_images": unreadable_images,
        },
    }

    out_p.parent.mkdir(parents=True, exist_ok=True)
    with open(out_p, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    return result


# ═══════════════════════════════════════════════════════════════════
# 8. INTERFAZ CLI
# ═══════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(
        description="FlexGlue + SIFT Ablation Feature Matcher & Pose Refinement for RAPTOR/Zenith"
    )
    parser.add_argument("--images", "-i", help="Directorio con imágenes de entrada (RGB o Térmicas)")
    parser.add_argument("--output", "-o", help="Archivo de salida TiePointFile (.json)")
    parser.add_argument(
        "--matcher",
        choices=["flexglue", "sift"],
        default="flexglue",
        help="Algoritmo de emparejamiento ('flexglue' o línea base 'sift')",
    )
    parser.add_argument("--is-thermal", action="store_true", help="Activar modo de enmascaramiento térmico")
    parser.add_argument("--raw-dir", help="Directorio con búferes float32 .raw de radiometría térmica")
    parser.add_argument("--max-dim", type=int, default=1600, help="Dimensión máxima para downsampling de SIFT")
    parser.add_argument("--k-along", type=int, default=2, help="Cuota de vecinos en la misma pasada longitudinal")
    parser.add_argument("--k-cross", type=int, default=4, help="Cuota de vecinos en pasadas transversales/opuestas")

    # Modos de transferencia de pose RGB -> Térmico
    parser.add_argument(
        "--transfer-poses-to-thermal",
        action="store_true",
        help="Transferir poses RGB refinadas a fotogramas térmicos homólogos",
    )
    parser.add_argument("--refined-rgb-poses", help="Ruta al archivo JSON de poses RGB refinadas emitido por Zenith")
    parser.add_argument("--thermal-images-dir", help="Directorio con fotogramas térmicos para transferencia de poses")

    args = parser.parse_args()

    if args.transfer_poses_to_thermal:
        if not args.refined_rgb_poses or not args.thermal_images_dir or not args.output:
            parser.error("--transfer-poses-to-thermal requiere --refined-rgb-poses, --thermal-images-dir y --output")
        transfer_poses_to_thermal(
            refined_rgb_poses_path=args.refined_rgb_poses,
            thermal_images_dir=args.thermal_images_dir,
            output_tie_points_path=args.output,
        )
        print(f"✓ Poses RGB transferidas exitosamente a {args.output}")
        sys.exit(0)

    if not args.images or not args.output:
        parser.error("Se requieren los argumentos --images y --output para ejecutar el emparejamiento")

    run_feature_matching_pipeline(
        images_dir=args.images,
        output_tie_points_path=args.output,
        matcher_type=args.matcher,
        is_thermal=args.is_thermal,
        raw_dir=args.raw_dir,
        max_dim=args.max_dim,
        k_along=args.k_along,
        k_cross=args.k_cross,
    )
    print(f"✓ Manifiesto de puntos de enlace generado en {args.output}")


if __name__ == "__main__":
    main()
