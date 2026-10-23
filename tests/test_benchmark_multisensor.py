"""Tests unitarios para el arnés de benchmark y validación científica.

Verifica ResourceMonitor, métricas colorimétricas y radiométricas, y evaluación de pares.
"""

from pathlib import Path
import shutil
import subprocess
import time

import numpy as np
from PIL import Image
import pytest

from scripts.benchmark_multisensor_validation import (
    ResourceMonitor,
    compute_sobel_gradient_magnitude,
    delta_e_cie76,
    evaluate_orthomosaic_pair,
    evaluate_thermal_pair,
)


class TestResourceMonitor:
    def test_monitor_normal_execution(self):
        """Verifica que el monitor muestree recursos y devuelva métricas razonables."""
        monitor = ResourceMonitor(sampling_interval_s=0.1, min_mem_available_threshold_mb=100.0)
        proc = subprocess.Popen(["sleep", "0.4"], start_new_session=True)
        monitor.set_process_handle(proc)
        monitor.start()

        proc.wait()
        metrics = monitor.stop()

        assert metrics["wall_time_s"] >= 0.3
        assert metrics["peak_rss_mb"] >= 0.0
        assert metrics["min_mem_avail_mb"] > 0.0
        assert metrics["aborted_low_mem"] is False

    def test_monitor_aborts_on_low_memory(self):
        """Verifica que el monitor aborte inmediatamente si la memoria disponible está por debajo del umbral."""
        monitor = ResourceMonitor(sampling_interval_s=0.05, min_mem_available_threshold_mb=99999999.0)
        proc = subprocess.Popen(["sleep", "10"], start_new_session=True)
        monitor.set_process_handle(proc)
        monitor.start()

        time.sleep(0.3)
        metrics = monitor.stop()

        assert metrics["aborted_low_mem"] is True
        assert proc.poll() is not None  # El proceso fue terminado


class TestColorAndMetrics:
    def test_delta_e_cie76_identical_and_different(self):
        rgb1 = np.full((10, 10, 3), 128, dtype=np.uint8)
        rgb2 = np.full((10, 10, 3), 128, dtype=np.uint8)
        de_zero = delta_e_cie76(rgb1, rgb2)
        assert np.allclose(de_zero, 0.0)

        rgb3 = np.full((10, 10, 3), 200, dtype=np.uint8)
        de_diff = delta_e_cie76(rgb1, rgb3)
        assert np.all(de_diff > 10.0)

    def test_sobel_gradient(self):
        flat = np.full((20, 20), 100.0, dtype=np.float32)
        grad_flat = compute_sobel_gradient_magnitude(flat)
        assert np.allclose(grad_flat[2:-2, 2:-2], 0.0)

        step = np.zeros((20, 20), dtype=np.float32)
        step[:, 10:] = 200.0
        grad_step = compute_sobel_gradient_magnitude(step)
        assert np.max(grad_step[:, 9:12]) > 100.0


@pytest.mark.skipif(shutil.which("gdal_translate") is None, reason="Requiere herramientas CLI de GDAL")
class TestEvaluationPipelines:
    def _create_mock_geotiff(self, path: Path, arr: np.ndarray, is_thermal: bool = False):
        img = Image.fromarray(arr)
        img.save(path)
        # Añadir geotransform mínima con gdal_translate
        tmp_geo = path.with_suffix(".tmp.tif")
        cmd = [
            "gdal_translate",
            "-a_srs",
            "EPSG:9377",
            "-a_ullr",
            "5000000",
            "2000000",
            "5000100",
            "1999900",
            str(path),
            str(tmp_geo),
        ]
        subprocess.run(cmd, capture_output=True, check=True)
        tmp_geo.replace(path)

    def test_evaluate_thermal_pair(self, tmp_path):
        work_dir = tmp_path / "work_thermal"
        t_ref_arr = np.full((100, 100), 25.0, dtype=np.float32)
        t_ref_arr[40:50, 40:50] = 85.0  # Hotspot

        t_cand_arr = np.full((100, 100), 26.0, dtype=np.float32)
        t_cand_arr[40:50, 40:50] = 84.0  # Hotspot detectado

        ref_tif = tmp_path / "ref_thermal.tif"
        cand_tif = tmp_path / "cand_thermal.tif"
        self._create_mock_geotiff(ref_tif, t_ref_arr, is_thermal=True)
        self._create_mock_geotiff(cand_tif, t_cand_arr, is_thermal=True)

        res = evaluate_thermal_pair(cand_tif, ref_tif, work_dir, resolution_m=0.35)
        assert res["sensor"] == "thermal"
        assert res["peak_temp_cand_p99_c"] > 50.0
        assert res["mae_c"] <= 1.5
        assert res["hotspot_f1"] > 0.8

    def test_evaluate_orthomosaic_pair_rgb(self, tmp_path):
        work_dir = tmp_path / "work_rgb"
        # Imagen sintética RGB de 100x100
        ref_arr = np.full((100, 100, 3), 120, dtype=np.uint8)
        ref_arr[20:60, 20:60] = [180, 150, 100]

        cand_arr = ref_arr.copy()

        ref_tif = tmp_path / "ref_rgb.tif"
        cand_tif = tmp_path / "cand_rgb.tif"
        self._create_mock_geotiff(ref_tif, ref_arr)
        self._create_mock_geotiff(cand_tif, cand_arr)

        res = evaluate_orthomosaic_pair(cand_tif, ref_tif, work_dir, resolution_m=0.25)
        assert res["sensor"] == "rgb"
        assert res["valid_area_ha"] > 0.0
        assert res["coverage_iou_pct"] > 95.0
        assert res["psnr_db"] > 40.0
        assert res["ssim_index"] > 0.95
