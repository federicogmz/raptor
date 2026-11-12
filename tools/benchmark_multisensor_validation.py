#!/usr/bin/env python3
"""Arnés de Benchmark y Validación Científica Multi-Sensor para RAPTOR / ZENITH.

Provee:
1. ResourceMonitor: Guardián de recursos WSL (MemAvailable < 6 GB aborta proceso,
   monitoreo de RSS total del árbol de procesos y % de CPU a 2 Hz).
2. Evaluación geométrica, radiométrica y de continuidad de costuras (SGDR, ΔE,
   Tenengrad, Laplaciano, PSNR, SSIM, IoU de cobertura) entre pares de ortomosaicos.
3. Evaluación térmica radiométrica y detección de hotspots (pico P99.9, MAE, RMSE,
   correlación de Pearson, F1/IoU de focos de calor).
4. Experimento sintético controlado para evaluación de compensación de ganancia y
   modos de mezcla (Best-Nadir vs Micro-Feather).
"""

import argparse
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

# ══════════════════════════════════════════════════════════════════════════
# 1. GUARDIÁN DE RECURSOS WSL (ResourceMonitor)
# ══════════════════════════════════════════════════════════════════════════


class ResourceMonitor:
    """Monitorea el uso de recursos a frecuencia configurable (default 2 Hz).

    Monitorea MemAvailable desde /proc/meminfo y RSS del proceso y todos sus hijos.
    Si MemAvailable cae por debajo de threshold_mb (default 6144 MB), aborta el
    proceso inmediatamente para proteger la memoria de WSL de un kernel panic / OOM.
    """

    def __init__(
        self,
        target_pid: Optional[int] = None,
        sampling_interval_s: float = 0.5,
        min_mem_available_threshold_mb: float = 6144.0,
    ):
        self.target_pid = target_pid
        self.sampling_interval_s = sampling_interval_s
        self.min_mem_available_threshold_mb = min_mem_available_threshold_mb
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self.min_mem_avail_mb = float("inf")
        self.peak_rss_mb = 0.0
        self.cpu_samples: List[float] = []
        self.wall_time_s = 0.0
        self.aborted_low_mem = False
        self._proc_handle: Optional[subprocess.Popen] = None
        self._page_size = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

    def set_process_handle(self, proc: subprocess.Popen):
        self._proc_handle = proc
        self.target_pid = proc.pid

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop(self) -> Dict[str, Any]:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

        mean_cpu = float(np.mean(self.cpu_samples)) if self.cpu_samples else 0.0
        peak_cpu = float(np.max(self.cpu_samples)) if self.cpu_samples else 0.0
        min_avail = self.min_mem_avail_mb if self.min_mem_avail_mb != float("inf") else 0.0

        return {
            "peak_rss_mb": round(self.peak_rss_mb, 2),
            "min_mem_avail_mb": round(min_avail, 2),
            "mean_cpu_pct": round(mean_cpu, 1),
            "peak_cpu_pct": round(peak_cpu, 1),
            "wall_time_s": round(self.wall_time_s, 2),
            "aborted_low_mem": self.aborted_low_mem,
        }

    def _read_mem_available_mb(self) -> float:
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        parts = line.split()
                        return float(parts[1]) / 1024.0
        except Exception:
            pass
        return float("inf")

    def _get_tree_pids(self, root_pid: int) -> List[int]:
        pids = [root_pid]
        frontier = [root_pid]
        while frontier:
            p = frontier.pop()
            task_children = f"/proc/{p}/task/{p}/children"
            try:
                with open(task_children, "r", encoding="utf-8") as f:
                    children = [int(x) for x in f.read().split()]
                    pids.extend(children)
                    frontier.extend(children)
            except Exception:
                pass
        return pids

    def _calculate_total_rss_mb(self, root_pid: int) -> float:
        total_pages = 0
        pids = self._get_tree_pids(root_pid)
        for pid in pids:
            statm_path = f"/proc/{pid}/statm"
            try:
                with open(statm_path, "r", encoding="utf-8") as f:
                    fields = f.read().split()
                    if len(fields) >= 2:
                        total_pages += int(fields[1])
            except Exception:
                pass
        return (total_pages * self._page_size) / (1024.0 * 1024.0)

    def _read_cgroup_memory_mb(self) -> Optional[float]:
        if not os.path.exists("/.dockerenv"):
            return None
        for path in ["/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"]:
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        val = int(f.read().strip())
                        return val / (1024.0 * 1024.0)
                except Exception:
                    pass
        return None

    def _monitor_loop(self):
        t0 = time.time()
        last_cpu_time = 0.0
        last_sys_time = time.time()

        while not self._stop_event.is_set():
            now = time.time()
            self.wall_time_s = now - t0

            # 1. Chequeo de MemAvailable
            avail_mb = self._read_mem_available_mb()
            if avail_mb < self.min_mem_avail_mb:
                self.min_mem_avail_mb = avail_mb

            if avail_mb < self.min_mem_available_threshold_mb:
                print(
                    f"\n🚨 [ResourceMonitor] GUARDIA DE MEMORIA ACTIVADA: MemAvailable ({avail_mb:.1f} MB) "
                    f"bajó de {self.min_mem_available_threshold_mb:.0f} MB. Abortando proceso para proteger WSL...",
                    file=sys.stderr,
                )
                self.aborted_low_mem = True
                self._abort_target()
                break

            # 2. Medición de RSS (árbol de procesos + cgroup)
            if self.target_pid:
                tree_rss = self._calculate_total_rss_mb(self.target_pid)
                cg_rss = self._read_cgroup_memory_mb() or 0.0
                current_rss = max(tree_rss, cg_rss)
                if current_rss > self.peak_rss_mb:
                    self.peak_rss_mb = current_rss

            # 3. Muestreo de CPU
            if self.target_pid:
                total_utime_stime = 0.0
                for pid in self._get_tree_pids(self.target_pid):
                    stat_file = f"/proc/{pid}/stat"
                    try:
                        with open(stat_file, "r", encoding="utf-8") as f:
                            content = f.read()
                            # Extraer campos después del paréntesis del nombre
                            r_idx = content.rfind(")")
                            if r_idx != -1:
                                fields = content[r_idx + 1 :].split()
                                utime = float(fields[11])
                                stime = float(fields[12])
                                total_utime_stime += (utime + stime) / 100.0
                    except Exception:
                        pass
                dt = now - last_sys_time
                if dt > 0.05 and last_cpu_time > 0:
                    cpu_pct = ((total_utime_stime - last_cpu_time) / dt) * 100.0
                    self.cpu_samples.append(max(0.0, min(cpu_pct, 1600.0)))
                last_cpu_time = total_utime_stime
                last_sys_time = now

            self._stop_event.wait(self.sampling_interval_s)

    def _abort_target(self):
        if self._proc_handle:
            try:
                pgid = os.getpgid(self._proc_handle.pid)
                if pgid == self._proc_handle.pid:
                    os.killpg(pgid, signal.SIGTERM)
                    time.sleep(0.5)
                    if self._proc_handle.poll() is None:
                        os.killpg(pgid, signal.SIGKILL)
                else:
                    self._proc_handle.terminate()
                    time.sleep(0.5)
                    if self._proc_handle.poll() is None:
                        self._proc_handle.kill()
            except Exception:
                try:
                    self._proc_handle.kill()
                except Exception:
                    pass
        elif self.target_pid:
            try:
                os.kill(self.target_pid, signal.SIGKILL)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════
# 2. TRANSFORMACIONES ESPACIALES Y COLORIMÉTRICAS
# ══════════════════════════════════════════════════════════════════════════


def srgb_to_linear(srgb: np.ndarray) -> np.ndarray:
    """Convierte canales sRGB [0, 255] a luminancia lineal [0.0, 1.0]."""
    v = np.clip(srgb.astype(np.float32) / 255.0, 0.0, 1.0)
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)


def srgb_to_cielab(rgb_arr: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Convierte array RGB (H, W, 3) [0..255] a componentes CIELAB (L*, a*, b*) usando D65."""
    r_lin = srgb_to_linear(rgb_arr[:, :, 0])
    g_lin = srgb_to_linear(rgb_arr[:, :, 1])
    b_lin = srgb_to_linear(rgb_arr[:, :, 2])

    # Matriz sRGB -> XYZ (D65)
    X = r_lin * 0.4124564 + g_lin * 0.3575761 + b_lin * 0.1804375
    Y = r_lin * 0.2126729 + g_lin * 0.7151522 + b_lin * 0.0721750
    Z = r_lin * 0.0193339 + g_lin * 0.1191920 + b_lin * 0.9503041

    # Normalización D65
    xn, yn, zn = 0.95047, 1.00000, 1.08883
    xr, yr, zr = X / xn, Y / yn, Z / zn

    def f_lab(t: np.ndarray) -> np.ndarray:
        delta = 6.0 / 29.0
        return np.where(t > delta**3, np.cbrt(t), (t / (3.0 * delta**2)) + (4.0 / 29.0))

    fx, fy, fz = f_lab(xr), f_lab(yr), f_lab(zr)
    L_star = 116.0 * fy - 16.0
    a_star = 500.0 * (fx - fy)
    b_star = 200.0 * (fy - fz)
    return L_star, a_star, b_star


def delta_e_cie76(rgb1: np.ndarray, rgb2: np.ndarray) -> np.ndarray:
    """Calcula ΔE_ab (CIE76) entre dos arrays de color RGB."""
    l1, a1, b1 = srgb_to_cielab(rgb1)
    l2, a2, b2 = srgb_to_cielab(rgb2)
    return np.sqrt((l1 - l2) ** 2 + (a1 - a2) ** 2 + (b1 - b2) ** 2)


def compute_sobel_gradient_magnitude(gray: np.ndarray) -> np.ndarray:
    """Calcula la magnitud del gradiente de Sobel sin requerir scipy/cv2."""
    h, w = gray.shape
    gx = np.zeros_like(gray, dtype=np.float32)
    gy = np.zeros_like(gray, dtype=np.float32)

    # Sobel X: [[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]]
    # Sobel Y: [[-1, -2, -1], [0, 0, 0], [1, 2, 1]]
    p = np.pad(gray.astype(np.float32), 1, mode="edge")
    gx = (
        -1.0 * p[:-2, :-2]
        + 1.0 * p[:-2, 2:]
        - 2.0 * p[1:-1, :-2]
        + 2.0 * p[1:-1, 2:]
        - 1.0 * p[2:, :-2]
        + 1.0 * p[2:, 2:]
    )
    gy = (
        -1.0 * p[:-2, :-2]
        - 2.0 * p[:-2, 1:-1]
        - 1.0 * p[:-2, 2:]
        + 1.0 * p[2:, :-2]
        + 2.0 * p[2:, 1:-1]
        + 1.0 * p[2:, 2:]
    )
    return np.sqrt(gx**2 + gy**2)


def compute_laplacian_variance(gray: np.ndarray, mask: Optional[np.ndarray] = None) -> float:
    """Calcula la varianza del Laplaciano (métrica de nitidez contra desenfoque)."""
    p = np.pad(gray.astype(np.float32), 1, mode="edge")
    # Kernel 3x3: [[0, 1, 0], [1, -4, 1], [0, 1, 0]]
    lap = p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:] - 4.0 * p[1:-1, 1:-1]
    if mask is not None:
        valid_vals = lap[mask]
        return float(np.var(valid_vals)) if len(valid_vals) > 0 else 0.0
    return float(np.var(lap))


def compute_tenengrad(gray: np.ndarray, mask: Optional[np.ndarray] = None) -> float:
    """Calcula la energía de Tenengrad (sumatoria de gradientes de Sobel al cuadrado)."""
    mag = compute_sobel_gradient_magnitude(gray)
    sq = mag**2
    if mask is not None:
        valid_sq = sq[mask]
        return float(np.mean(valid_sq)) if len(valid_sq) > 0 else 0.0
    return float(np.mean(sq))


def compute_ssim_numpy(img1: np.ndarray, img2: np.ndarray, mask: np.ndarray) -> float:
    """Calcula el índice SSIM medio sobre la región válida sin scipy/skimage."""
    c1 = (0.01 * 255.0) ** 2
    c2 = (0.03 * 255.0) ** 2

    # Promedios locales por ventanas de 8x8
    win_size = 8
    h, w = img1.shape
    ssim_vals = []

    for y in range(0, h - win_size, win_size):
        for x in range(0, w - win_size, win_size):
            m_win = mask[y : y + win_size, x : x + win_size]
            if np.count_nonzero(m_win) < (win_size * win_size * 0.75):
                continue
            w1 = img1[y : y + win_size, x : x + win_size].astype(np.float64)
            w2 = img2[y : y + win_size, x : x + win_size].astype(np.float64)

            mu1 = np.mean(w1)
            mu2 = np.mean(w2)
            sigma1_sq = np.var(w1)
            sigma2_sq = np.var(w2)
            sigma12 = np.mean((w1 - mu1) * (w2 - mu2))

            num = (2.0 * mu1 * mu2 + c1) * (2.0 * sigma12 + c2)
            den = (mu1**2 + mu2**2 + c1) * (sigma1_sq + sigma2_sq + c2)
            ssim_vals.append(num / max(1e-6, den))

    return float(np.mean(ssim_vals)) if ssim_vals else 1.0


# ══════════════════════════════════════════════════════════════════════════
# 3. ALINEACIÓN Y REPROYECCIÓN GDAL
# ══════════════════════════════════════════════════════════════════════════


def get_gdal_extent_and_crs(tif_path: Path) -> Tuple[Optional[Tuple[float, float, float, float]], Optional[str]]:
    """Obtiene (xmin, ymin, xmax, ymax) y CRS de un GeoTIFF usando gdalinfo -json."""
    try:
        res = subprocess.run(["gdalinfo", "-json", str(tif_path)], capture_output=True, text=True, check=True)
        info = json.loads(res.stdout)
        coords = info.get("cornerCoordinates", {})
        ul = coords.get("upperLeft", [])
        lr = coords.get("lowerRight", [])
        if ul and lr:
            xmin = min(ul[0], lr[0])
            xmax = max(ul[0], lr[0])
            ymin = min(ul[1], lr[1])
            ymax = max(ul[1], lr[1])
            crs = info.get("coordinateSystem", {}).get("wkt", "")
            return (xmin, ymin, xmax, ymax), crs
    except Exception as exc:
        print(f"  ⚠ Aviso en gdalinfo: {exc}", file=sys.stderr)
    return None, None


def align_and_warp_pair(
    cand_tif: Path,
    ref_tif: Path,
    work_dir: Path,
    resolution_m: float = 0.25,
    target_crs: str = "EPSG:9377",
) -> Tuple[Path, Path]:
    """Reproyecta y remuestrea cand_tif y ref_tif sobre la MISMA grilla en EPSG:9377 usando gdalwarp."""
    work_dir.mkdir(parents=True, exist_ok=True)
    cand_warped = work_dir / f"cand_warped_{resolution_m}m.tif"
    ref_warped = work_dir / f"ref_warped_{resolution_m}m.tif"

    # 1. Warp de la referencia para fijar la cuadrícula común (reutiliza si ya existe en caché local)
    if not ref_warped.exists() or ref_warped.stat().st_size == 0:
        cmd_ref = [
            "gdalwarp",
            "-t_srs",
            target_crs,
            "-tr",
            str(resolution_m),
            str(resolution_m),
            "-tap",
            "-r",
            "bilinear",
            "-overwrite",
            str(ref_tif),
            str(ref_warped),
        ]
        subprocess.run(cmd_ref, capture_output=True, check=True)

    # 2. Leer la extensión exacta del ráster de referencia ya proyectado
    ext, _ = get_gdal_extent_and_crs(ref_warped)
    if not ext:
        raise RuntimeError(f"No se pudo determinar la extensión geodésica de {ref_warped}")
    xmin, ymin, xmax, ymax = ext

    # 3. Warp del candidato con exactamente el mismo -te para cuadrícula píxel a píxel idéntica
    cmd_cand = [
        "gdalwarp",
        "-t_srs",
        target_crs,
        "-tr",
        str(resolution_m),
        str(resolution_m),
        "-te",
        str(xmin),
        str(ymin),
        str(xmax),
        str(ymax),
        "-tap",
        "-r",
        "bilinear",
        "-overwrite",
        str(cand_tif),
        str(cand_warped),
    ]
    subprocess.run(cmd_cand, capture_output=True, check=True)

    return cand_warped, ref_warped


# ══════════════════════════════════════════════════════════════════════════
# 4. EVALUACIÓN DE ORTOMOSAICOS VISUALES (RGB)
# ══════════════════════════════════════════════════════════════════════════


def _load_rgb_raster(tif_path: Path, work_dir: Path, tag: str) -> np.ndarray:
    """Lee un GeoTIFF RGB(A) como array (H, W, 3) uint8, con fallback a ENVI vía GDAL si PIL falla."""
    try:
        img = np.array(Image.open(tif_path))
        if img.ndim == 2:
            return np.stack([img] * 3, axis=-1).astype(np.uint8)
        return img[:, :, :3].astype(np.uint8)
    except Exception:
        info = json.loads(subprocess.check_output(["gdalinfo", "-json", str(tif_path)], text=True))
        w, h = info["size"]
        bands = len(info.get("bands", []))
        raw_p = work_dir / f"{tag}_rgb.bin"
        b_args = ["-b", "1", "-b", "2", "-b", "3"] if bands >= 3 else ["-b", "1"]
        subprocess.run(
            ["gdal_translate", "-of", "ENVI", "-ot", "Byte", "-co", "INTERLEAVE=BIP"]
            + b_args
            + [str(tif_path), str(raw_p)],
            capture_output=True,
            check=True,
        )
        n_ch = 3 if bands >= 3 else 1
        data = np.fromfile(raw_p, dtype=np.uint8).reshape((h, w, n_ch))
        if n_ch == 1:
            data = np.repeat(data, 3, axis=-1)
        return data


def evaluate_orthomosaic_pair(
    cand_tif: Path,
    ref_tif: Path,
    work_dir: Path,
    resolution_m: float = 0.25,
    camera_positions_9377: Optional[List[Tuple[float, float]]] = None,
) -> Dict[str, Any]:
    """Evalúa calidad geométrica, cobertura, continuidad de costuras y nitidez entre 2 ortomosaicos RGB."""
    t0 = time.time()
    cand_w, ref_w = align_and_warp_pair(cand_tif, ref_tif, work_dir, resolution_m=resolution_m)

    # Leer imágenes como arrays numpy (con fallback GDAL si TIFF es BigTIFF/4-bandas MINISBLACK)
    img_cand = _load_rgb_raster(cand_w, work_dir, "cand")
    img_ref = _load_rgb_raster(ref_w, work_dir, "ref")

    h, w, _ = img_cand.shape

    # 1. Máscaras de validez (no negro puro 0,0,0 ni blanco puro de fondo nodata 255,255,255)
    mask_cand = np.any(img_cand > 0, axis=-1) & (~np.all(img_cand == 255, axis=-1))
    mask_ref = np.any(img_ref > 0, axis=-1) & (~np.all(img_ref == 255, axis=-1))
    common_mask = mask_cand & mask_ref

    cand_valid_pixels = int(np.count_nonzero(mask_cand))
    ref_valid_pixels = int(np.count_nonzero(mask_ref))
    intersection_pixels = int(np.count_nonzero(common_mask))
    union_pixels = int(np.count_nonzero(mask_cand | mask_ref))

    valid_area_ha = (cand_valid_pixels * resolution_m * resolution_m) / 10000.0
    coverage_iou_pct = (intersection_pixels / max(1, union_pixels)) * 100.0

    # 2. Registro Geométrico Residual (Phase Correlation en ventanas de 100x100 m = ~400 px)
    win_px = int(round(100.0 / resolution_m))
    shifts_m = []
    gray_cand = (0.299 * img_cand[:, :, 0] + 0.587 * img_cand[:, :, 1] + 0.114 * img_cand[:, :, 2]).astype(np.float32)
    gray_ref = (0.299 * img_ref[:, :, 0] + 0.587 * img_ref[:, :, 1] + 0.114 * img_ref[:, :, 2]).astype(np.float32)

    for y in range(0, h - win_px, win_px):
        for x in range(0, w - win_px, win_px):
            m_win = common_mask[y : y + win_px, x : x + win_px]
            if np.count_nonzero(m_win) < (win_px * win_px * 0.85):
                continue

            w_c = gray_cand[y : y + win_px, x : x + win_px]
            w_r = gray_ref[y : y + win_px, x : x + win_px]
            if np.std(w_c) < 5.0 or np.std(w_r) < 5.0:
                continue  # Ventana sin textura suficiente

            # FFT Phase Correlation
            F1 = np.fft.fft2(w_c)
            F2 = np.fft.fft2(w_r)
            cross_power = (F1 * np.conj(F2)) / np.maximum(1e-9, np.abs(F1 * np.conj(F2)))
            corr = np.fft.ifft2(cross_power).real

            peak_y, peak_x = np.unravel_index(np.argmax(corr), corr.shape)
            dy = peak_y if peak_y < win_px // 2 else peak_y - win_px
            dx = peak_x if peak_x < win_px // 2 else peak_x - win_px

            shift_dist_m = math.sqrt(dx**2 + dy**2) * resolution_m
            if shift_dist_m < 15.0:  # Descartar outliers espurios
                shifts_m.append(shift_dist_m)

    shift_median_m = float(np.median(shifts_m)) if shifts_m else 0.0
    shift_rmse_m = float(np.sqrt(np.mean(np.array(shifts_m) ** 2))) if shifts_m else 0.0
    shift_ce90_m = float(np.percentile(shifts_m, 90)) if shifts_m else 0.0

    # 3. Construcción del Grafo de Voronoi y Máscaras de Costura
    # Si no se pasan centros de cámara, generar una cuadrícula simulada representativa del vuelo
    ext, _ = get_gdal_extent_and_crs(ref_w)
    xmin, ymin, xmax, ymax = ext if ext else (0.0, 0.0, 1000.0, 1000.0)

    if not camera_positions_9377:
        # Generar centros de paso de vuelo simulados
        grid_step_x = (xmax - xmin) / 6.0
        grid_step_y = (ymax - ymin) / 10.0
        camera_positions_9377 = []
        for gx in np.linspace(xmin + grid_step_x / 2, xmax - grid_step_x / 2, 6):
            for gy in np.linspace(ymin + grid_step_y / 2, ymax - grid_step_y / 2, 10):
                camera_positions_9377.append((float(gx), float(gy)))

    # Mapear coordenadas de centros de cámara a píxeles
    cams_px = []
    for cx, cy in camera_positions_9377:
        px = int(round((cx - xmin) / resolution_m))
        py = int(round((ymax - cy) / resolution_m))
        if 0 <= px < w and 0 <= py < h:
            cams_px.append((px, py))

    # Asignación de Voronoi rápida por bloques
    voronoi_id = np.zeros((h, w), dtype=np.int32)
    if cams_px:
        cams_arr = np.array(cams_px, dtype=np.float32)  # (N, 2)
        # Procesamiento en bloques de 512x512 para eficiencia de memoria
        blk = 512
        for by in range(0, h, blk):
            for bx in range(0, w, blk):
                y_end = min(h, by + blk)
                x_end = min(w, bx + blk)
                yy, xx = np.mgrid[by:y_end, bx:x_end]
                coords = np.stack([xx.ravel(), yy.ravel()], axis=-1)  # (K, 2)
                # Distancia a todas las cámaras (K, N)
                diff = coords[:, None, :] - cams_arr[None, :, :]
                dists_sq = np.sum(diff**2, axis=-1)
                nearest = np.argmin(dists_sq, axis=1)
                voronoi_id[by:y_end, bx:x_end] = nearest.reshape((y_end - by, x_end - bx))

    # Fronteras de Voronoi (cambio de id con respecto al vecino derecho o abajo)
    seam_raw = (voronoi_id[:-1, :-1] != voronoi_id[1:, :-1]) | (voronoi_id[:-1, :-1] != voronoi_id[:-1, 1:])
    seam_raw = np.pad(seam_raw, ((0, 1), (0, 1)), mode="constant", constant_values=False)

    # Dilatar frontera a ancho 3 px para seam_mask
    p_seam = np.pad(seam_raw, 1, mode="constant", constant_values=False)
    seam_mask = np.zeros_like(seam_raw)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            seam_mask |= p_seam[1 + dy : 1 + dy + h, 1 + dx : 1 + dx + w]

    seam_mask &= common_mask

    # Máscara interior: región entre 6 y 18 px de la costura
    p_seam_far = np.pad(seam_raw, 18, mode="constant", constant_values=False)
    mask_buffer_6 = np.zeros_like(seam_raw)
    mask_buffer_18 = np.zeros_like(seam_raw)
    for dy in range(-6, 7):
        for dx in range(-6, 7):
            if dy**2 + dx**2 <= 36:
                mask_buffer_6 |= p_seam_far[18 + dy : 18 + dy + h, 18 + dx : 18 + dx + w]

    for dy in range(-18, 19):
        for dx in range(-18, 19):
            if dy**2 + dx**2 <= 324:
                mask_buffer_18 |= p_seam_far[18 + dy : 18 + dy + h, 18 + dx : 18 + dx + w]

    interior_mask = mask_buffer_18 & (~mask_buffer_6) & common_mask

    # 4. Métrica SGDR (Seam Gradient Discontinuity Ratio)
    sobel_cand = compute_sobel_gradient_magnitude(gray_cand)
    sobel_ref = compute_sobel_gradient_magnitude(gray_ref)

    mean_seam_grad = float(np.mean(sobel_cand[seam_mask])) if np.count_nonzero(seam_mask) > 10 else 1.0
    mean_int_grad = float(np.mean(sobel_cand[interior_mask])) if np.count_nonzero(interior_mask) > 10 else 1.0
    sgdr = mean_seam_grad / max(1e-3, mean_int_grad)

    ref_seam_grad = float(np.mean(sobel_ref[seam_mask])) if np.count_nonzero(seam_mask) > 10 else 1.0
    ref_int_grad = float(np.mean(sobel_ref[interior_mask])) if np.count_nonzero(interior_mask) > 10 else 1.0
    sgdr_ref_control = ref_seam_grad / max(1e-3, ref_int_grad)

    # 5. Salto de Color en Costura (ΔE_ab CIE76 a ±3 px de la costura y contra referencia)
    delta_e_map = delta_e_cie76(img_cand, img_ref)
    seam_delta_e = float(np.mean(delta_e_map[seam_mask])) if np.count_nonzero(seam_mask) > 0 else 0.0

    # Salto de color intrínseco a través de las fronteras de Voronoi dentro de img_cand (±2 px)
    cross_step_h = delta_e_cie76(img_cand[:, :-4, :], img_cand[:, 4:, :])
    vor_diff_h = (voronoi_id[:, :-4] != voronoi_id[:, 4:]) & common_mask[:, :-4] & common_mask[:, 4:]
    vor_same_h = (voronoi_id[:, :-4] == voronoi_id[:, 4:]) & common_mask[:, :-4] & common_mask[:, 4:]
    seam_cross_de = float(np.mean(cross_step_h[vor_diff_h])) if np.count_nonzero(vor_diff_h) > 10 else seam_delta_e
    int_cross_de = (
        float(np.mean(cross_step_h[vor_same_h])) if np.count_nonzero(vor_same_h) > 10 else max(1.0, seam_cross_de)
    )
    seam_cross_de_ratio = seam_cross_de / max(1e-3, int_cross_de)

    # 6. Nitidez Espacial (Tenengrad y Varianza del Laplaciano en región interior común)
    tenengrad_cand = compute_tenengrad(gray_cand, interior_mask)
    tenengrad_ref = compute_tenengrad(gray_ref, interior_mask)
    laplacian_var_cand = compute_laplacian_variance(gray_cand, interior_mask)
    laplacian_var_ref = compute_laplacian_variance(gray_ref, interior_mask)

    # 7. Acuerdo Espectral con Oficial tras igualación lineal de ganancia/offset en intersección
    valid_c = img_cand[common_mask].astype(np.float64)
    valid_r = img_ref[common_mask].astype(np.float64)

    # Normalización lineal de ganancia/offset por canal
    equalized_cand = img_cand.astype(np.float64).copy()
    for ch in range(3):
        mean_c = np.mean(valid_c[:, ch])
        std_c = np.std(valid_c[:, ch])
        mean_r = np.mean(valid_r[:, ch])
        std_r = np.std(valid_r[:, ch])
        if std_c > 1e-3:
            gain = std_r / std_c
            offset = mean_r - gain * mean_c
            equalized_cand[:, :, ch] = np.clip(equalized_cand[:, :, ch] * gain + offset, 0.0, 255.0)

    eq_valid_c = equalized_cand[common_mask]
    diff = eq_valid_c - valid_r
    mae_dn = float(np.mean(np.abs(diff)))
    rmse_dn = float(np.sqrt(np.mean(diff**2)))
    psnr_db = float(20.0 * math.log10(255.0 / max(1e-4, rmse_dn)))
    ssim_idx = compute_ssim_numpy(equalized_cand[:, :, 1], img_ref[:, :, 1], common_mask)

    elapsed = time.time() - t0
    return {
        "sensor": "rgb",
        "resolution_eval_m": resolution_m,
        "valid_area_ha": round(valid_area_ha, 2),
        "coverage_iou_pct": round(coverage_iou_pct, 2),
        "shift_median_m": round(shift_median_m, 3),
        "shift_rmse_m": round(shift_rmse_m, 3),
        "shift_ce90_m": round(shift_ce90_m, 3),
        "seam_gradient_discontinuity_ratio": round(sgdr, 3),
        "seam_gradient_control_ratio": round(sgdr_ref_control, 3),
        "seam_delta_e": round(seam_delta_e, 2),
        "seam_cross_delta_e": round(seam_cross_de, 2),
        "seam_cross_delta_e_ratio": round(seam_cross_de_ratio, 3),
        "tenengrad_sharpness_cand": round(tenengrad_cand, 2),
        "tenengrad_sharpness_ref": round(tenengrad_ref, 2),
        "laplacian_var_cand": round(laplacian_var_cand, 2),
        "laplacian_var_ref": round(laplacian_var_ref, 2),
        "psnr_db": round(psnr_db, 2),
        "ssim_index": round(ssim_idx, 4),
        "mae_dn": round(mae_dn, 2),
        "rmse_dn": round(rmse_dn, 2),
        "evaluation_elapsed_s": round(elapsed, 2),
    }


# ══════════════════════════════════════════════════════════════════════════
# 5. EVALUACIÓN DE ORTOMOSAICOS TÉRMICOS (Float32 °C)
# ══════════════════════════════════════════════════════════════════════════


def _load_float32_raster(tif_path: Path, work_dir: Path, tag: str) -> np.ndarray:
    """Lee un GeoTIFF Float32 como array 2D numpy, con fallback a ENVI binario si PIL falla."""
    try:
        arr = np.array(Image.open(tif_path)).astype(np.float32)
        if arr.ndim > 2:
            arr = arr[:, :, 0]
        return arr
    except Exception:
        raw_p = work_dir / f"{tag}_f32.bin"
        subprocess.run(
            ["gdal_translate", "-of", "ENVI", "-ot", "Float32", str(tif_path), str(raw_p)],
            capture_output=True,
            check=True,
        )
        info = json.loads(subprocess.check_output(["gdalinfo", "-json", str(tif_path)], text=True))
        w, h = info["size"]
        data = np.fromfile(raw_p, dtype=np.float32).reshape((h, w))
        return data


def evaluate_thermal_pair(
    cand_tif: Path,
    ref_tif: Path,
    work_dir: Path,
    resolution_m: float = 0.35,
) -> Dict[str, Any]:
    """Evalúa métricas radiométricas, preservación de picos de incendios y detección de focos de calor."""
    t0 = time.time()
    cand_w, ref_w = align_and_warp_pair(cand_tif, ref_tif, work_dir, resolution_m=resolution_m)

    # Cargar float32 °C
    t_cand = _load_float32_raster(cand_w, work_dir, "cand")
    t_ref = _load_float32_raster(ref_w, work_dir, "ref")

    # Máscaras de temperaturas válidas (típico sensor térmico no refrigerado: -40 °C a 650 °C)
    mask_cand = (t_cand > -40.0) & (t_cand < 1000.0) & (~np.isnan(t_cand))
    mask_ref = (t_ref > -40.0) & (t_ref < 1000.0) & (~np.isnan(t_ref))
    common_mask = mask_cand & mask_ref

    if np.count_nonzero(common_mask) == 0:
        raise ValueError("No se encontraron píxeles térmicos válidos en la intersección entre candidato y referencia.")

    vals_cand = t_cand[common_mask]
    vals_ref = t_ref[common_mask]

    # 1. Preservación de Pico Térmico
    peak_p99_cand = float(np.percentile(vals_cand, 99.9))
    peak_max_cand = float(np.max(vals_cand))
    peak_p99_ref = float(np.percentile(vals_ref, 99.9))
    peak_max_ref = float(np.max(vals_ref))
    peak_temp_error_c = abs(peak_p99_cand - peak_p99_ref)

    # 2. Precisión Radiométrica (absoluta y corregida por sesgo constante de emisividad/ambiente)
    diff = vals_cand - vals_ref
    mae_c = float(np.mean(np.abs(diff)))
    rmse_c = float(np.sqrt(np.mean(diff**2)))

    mu_cand = float(np.mean(vals_cand))
    sigma_cand = float(np.std(vals_cand))
    mu_ref = float(np.mean(vals_ref))
    sigma_ref = float(np.std(vals_ref))
    bias_c = mu_cand - mu_ref
    diff_unbiased = (vals_cand - bias_c) - vals_ref
    bias_corrected_mae_c = float(np.mean(np.abs(diff_unbiased)))
    bias_corrected_rmse_c = float(np.sqrt(np.mean(diff_unbiased**2)))

    # Correlación de Pearson
    r_matrix = np.corrcoef(vals_cand, vals_ref)
    pearson_r = float(r_matrix[0, 1]) if not np.isnan(r_matrix[0, 1]) else 0.0

    # 3. Detección de Hotspots (Focos de Calor): anomalía térmica sobre el fondo (μ + 2.5σ)
    hotspot_threshold = mu_ref + 2.5 * sigma_ref
    cand_threshold = (mu_cand + 2.5 * sigma_cand) if abs(bias_c) > 1.5 else hotspot_threshold

    hot_cand = vals_cand >= cand_threshold
    hot_ref = vals_ref >= hotspot_threshold

    tp = int(np.count_nonzero(hot_cand & hot_ref))
    fp = int(np.count_nonzero(hot_cand & (~hot_ref)))
    fn = int(np.count_nonzero((~hot_cand) & hot_ref))

    precision = tp / max(1, tp + fp)
    recall = tp / max(1, tp + fn)
    f1 = 2.0 * (precision * recall) / max(1e-6, (precision + recall))
    iou = tp / max(1, tp + fp + fn)

    elapsed = time.time() - t0
    return {
        "sensor": "thermal",
        "resolution_eval_m": resolution_m,
        "peak_temp_cand_p99_c": round(peak_p99_cand, 2),
        "peak_temp_cand_max_c": round(peak_max_cand, 2),
        "peak_temp_ref_p99_c": round(peak_p99_ref, 2),
        "peak_temp_ref_max_c": round(peak_max_ref, 2),
        "peak_temp_error_c": round(peak_temp_error_c, 2),
        "mae_c": round(mae_c, 2),
        "rmse_c": round(rmse_c, 2),
        "bias_c": round(bias_c, 2),
        "bias_corrected_mae_c": round(bias_corrected_mae_c, 2),
        "bias_corrected_rmse_c": round(bias_corrected_rmse_c, 2),
        "pearson_r": round(pearson_r, 4),
        "hotspot_threshold_c": round(hotspot_threshold, 2),
        "hotspot_true_positives": tp,
        "hotspot_precision": round(precision, 4),
        "hotspot_recall": round(recall, 4),
        "hotspot_f1": round(f1, 4),
        "hotspot_iou": round(iou, 4),
        "evaluation_elapsed_s": round(elapsed, 2),
    }


# ══════════════════════════════════════════════════════════════════════════
# 6. EXPERIMENTO SINTÉTICO CONTROLADO (Compensación de Ganancia)
# ══════════════════════════════════════════════════════════════════════════


def run_synthetic_gain_test(
    source_images_dir: Path,
    output_base_dir: Path,
    zenith_bin: str = "zenith",
) -> Dict[str, Any]:
    """Ejecuta experimento sintético perturbando la exposición y midiendo la recuperación."""
    output_base_dir.mkdir(parents=True, exist_ok=True)
    stage_dir = output_base_dir / "synthetic_stage"
    stage_dir.mkdir(parents=True, exist_ok=True)

    all_jpgs = sorted(list(source_images_dir.glob("*.jpg")) + list(source_images_dir.glob("*.JPG")))
    # Filtrar solo fotos RGB (descartar térmicas)
    rgb_jpgs = [p for p in all_jpgs if not (p.stem.endswith("_T") or "_T_" in p.stem)]

    if len(rgb_jpgs) < 6:
        raise ValueError(f"Se requieren al menos 6 fotos RGB para el test sintético (encontradas: {len(rgb_jpgs)}).")

    subset_photos = rgb_jpgs[: min(40, len(rgb_jpgs))]
    print(f"🔬 Preparando experimento sintético sobre {len(subset_photos)} fotos RGB...")

    clean_dir = stage_dir / "clean_images"
    perturbed_dir = stage_dir / "perturbed_images"
    clean_dir.mkdir(parents=True, exist_ok=True)
    perturbed_dir.mkdir(parents=True, exist_ok=True)

    # 1. Copiar imágenes y aplicar perturbación simétrica de ±12%
    for idx, p in enumerate(subset_photos):
        clean_target = clean_dir / p.name
        perturbed_target = perturbed_dir / p.name

        shutil.copy2(p, clean_target)

        # Perturbar brillo multiplicando el raster y preservando todos los segmentos APP0/APP1 (EXIF + DJI XMP)
        orig_bytes = p.read_bytes()
        # Extraer segmentos de metadatos entre SOI (FF D8) y el primer DQT/SOF (FF DB / FF C0 / FF C2)
        app_header = b""
        idx_b = 2
        while idx_b + 4 < len(orig_bytes) and orig_bytes[idx_b] == 0xFF:
            marker = orig_bytes[idx_b + 1]
            if 0xE0 <= marker <= 0xEF:
                seg_len = (orig_bytes[idx_b + 2] << 8) | orig_bytes[idx_b + 3]
                idx_b += 2 + seg_len
            else:
                break
        if idx_b > 2:
            app_header = orig_bytes[2:idx_b]

        with Image.open(p) as img:
            factor = 1.15 if (idx % 2 == 1) else 0.85
            arr = np.array(img).astype(np.float32) * factor
            arr = np.clip(arr, 0.0, 255.0).astype(np.uint8)
            pert_img = Image.fromarray(arr)
            pert_img.save(perturbed_target, quality=95)

        if app_header:
            new_bytes = perturbed_target.read_bytes()
            # Saltar APP0 de PIL si existe para inyectar cabecera original intacta
            p_idx = 2
            while p_idx + 4 < len(new_bytes) and new_bytes[p_idx] == 0xFF and (0xE0 <= new_bytes[p_idx + 1] <= 0xEF):
                s_len = (new_bytes[p_idx + 2] << 8) | new_bytes[p_idx + 3]
                p_idx += 2 + s_len
            perturbed_target.write_bytes(b"\xff\xd8" + app_header + new_bytes[p_idx:])

    results = {}
    methods = [
        ("best_nadir", ["--blend-mode", "best-nadir", "--no-gain-compensation"]),
        ("micro_feather_no_gain", ["--blend-mode", "micro-feather", "--no-gain-compensation"]),
        ("micro_feather_with_gain", ["--blend-mode", "micro-feather"]),
    ]

    # Generar mosaico limpio de control
    control_out = stage_dir / "control_ortho"
    control_out.mkdir(parents=True, exist_ok=True)
    print("  Ejecutando mosaico de control (sin perturbar)...")
    local_dem = Path("/home/fdgmz/raptor/DEM_VA/dem_VA.tif")
    dem_args = ["--dem", str(local_dem)] if local_dem.exists() else ["--dem-source", "auto"]
    cmd_ctrl = [
        zenith_bin,
        "ortho-rgb",
        "--images",
        str(clean_dir),
        "--output",
        str(control_out / "ortho.raw"),
        "--manifest",
        str(control_out / "manifest.json"),
        "--blend-mode",
        "micro-feather",
    ] + dem_args
    subprocess.run(cmd_ctrl, check=True)
    ctrl_tif = control_out / "ortho.tif"

    for name, flags in methods:
        method_out = stage_dir / f"method_{name}"
        method_out.mkdir(parents=True, exist_ok=True)
        print(f"  Ejecutando método: {name} con flags: {' '.join(flags)}...")
        cmd = (
            [
                zenith_bin,
                "ortho-rgb",
                "--images",
                str(perturbed_dir),
                "--output",
                str(method_out / "ortho.raw"),
                "--manifest",
                str(method_out / "manifest.json"),
            ]
            + dem_args
            + flags
        )
        subprocess.run(cmd, check=True)
        cand_tif = method_out / "ortho.tif"

        # Evaluar contra el mosaico de control
        eval_res = evaluate_orthomosaic_pair(cand_tif, ctrl_tif, method_out / "eval_work")
        results[name] = {
            "seam_gradient_discontinuity_ratio": eval_res["seam_gradient_discontinuity_ratio"],
            "seam_delta_e": eval_res["seam_delta_e"],
            "seam_cross_delta_e": eval_res["seam_cross_delta_e"],
            "seam_cross_delta_e_ratio": eval_res["seam_cross_delta_e_ratio"],
            "psnr_db": eval_res["psnr_db"],
            "ssim_index": eval_res["ssim_index"],
            "tenengrad_sharpness": eval_res["tenengrad_sharpness_cand"],
        }

    summary = {
        "ok": True,
        "experiment": "synthetic_gain_recovery",
        "photos_count": len(subset_photos),
        "results": results,
    }

    out_file = output_base_dir / "synthetic_validation_summary.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(f"\n✅ Experimento sintético completado. Resumen guardado en: {out_file}")
    return summary


# ══════════════════════════════════════════════════════════════════════════
# 7. CLI PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(
        description="Arnés de Benchmark y Validación Científica Multi-Sensor (RAPTOR / ZENITH)"
    )
    parser.add_argument("--candidate", type=Path, help="Ruta al ortomosaico GeoTIFF candidato")
    parser.add_argument("--reference", type=Path, help="Ruta al ortomosaico GeoTIFF oficial de referencia")
    parser.add_argument("--sensor", choices=["rgb", "thermal"], default="rgb", help="Tipo de sensor a evaluar")
    parser.add_argument(
        "--resolution", type=float, default=None, help="Resolución de evaluación (default 0.25m RGB, 0.35m thermal)"
    )
    parser.add_argument("--work-dir", type=Path, default=Path("/tmp/raptor_bench_eval"), help="Directorio de trabajo")
    parser.add_argument("--output-json", type=Path, help="Ruta para guardar el informe JSON con las métricas")
    parser.add_argument("--run-synthetic-test", action="store_true", help="Ejecutar prueba sintética controlada")
    parser.add_argument("--synthetic-source-dir", type=Path, help="Directorio con imágenes para la prueba sintética")
    parser.add_argument(
        "--synthetic-output-dir",
        type=Path,
        default=Path("/tmp/raptor_synthetic_bench"),
        help="Directorio de salida sintética",
    )
    parser.add_argument(
        "--zenith-bin",
        default=os.environ.get("ZENITH_BIN", "zenith"),
        help="Ruta al binario de Zenith",
    )

    args = parser.parse_args()

    if args.run_synthetic_test:
        if not args.synthetic_source_dir or not args.synthetic_source_dir.is_dir():
            print("❌ ERROR: --synthetic-source-dir es obligatorio para --run-synthetic-test", file=sys.stderr)
            return 1
        summary = run_synthetic_gain_test(args.synthetic_source_dir, args.synthetic_output_dir, args.zenith_bin)
        print(json.dumps(summary, indent=2))
        return 0

    if not args.candidate or not args.reference:
        parser.print_help()
        return 0

    if not args.candidate.exists():
        print(f"❌ ERROR: El ortomosaico candidato no existe: {args.candidate}", file=sys.stderr)
        return 1
    if not args.reference.exists():
        print(f"❌ ERROR: El ortomosaico de referencia no existe: {args.reference}", file=sys.stderr)
        return 1

    res_eval = args.resolution or (0.25 if args.sensor == "rgb" else 0.35)

    if args.sensor == "rgb":
        print(f"🔍 Evaluando par de ortomosaicos RGB a {res_eval} m/px...")
        metrics = evaluate_orthomosaic_pair(args.candidate, args.reference, args.work_dir, resolution_m=res_eval)
    else:
        print(f"🌡️ Evaluando par de ortomosaicos térmicos a {res_eval} m/px...")
        metrics = evaluate_thermal_pair(args.candidate, args.reference, args.work_dir, resolution_m=res_eval)

    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(metrics, f, indent=2)
        print(f"💾 Informe guardado en: {args.output_json}")

    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
