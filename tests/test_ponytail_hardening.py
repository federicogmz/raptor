"""Tests de hardening Ponytail para RAPTOR.

Cubre:
1. POST /api/missions/{mission}/save-area: 404 en misión inexistente (cero directorio fantasma),
   escritura atómica con total_area_ha > 0 en misión existente, cero escrituras a RAPTOR_BENCHMARK_DIR.
2. DELETE /api/missions/{mission}: borrado de misión ociosa y 409 si está activa en _state.
3. Concurrencia protegida por _start_lock en start_mission (una tiene éxito, la segunda 409).
4. Catálogo de exportación: 'area' en _available_export_products y exportación de polígono experto.
5. Cargador seguro de configuración en raptor: respeta espacios y previene inyección de comandos.
6. Procesamiento por tiras (strip/windowed) con paridad matemática.
"""

import asyncio
import importlib
import json
from pathlib import Path
import subprocess
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def webapp_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("RAPTOR_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("EXPORT_HOST_DIR", "/home/usuario/entregas")
    montaje = tmp_path / "export"
    montaje.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EXPORT_MOUNT", str(montaje))
    from fastapi.testclient import TestClient
    from core import scan

    importlib.reload(scan)
    from webapp import main as M

    importlib.reload(M)
    M._state = None
    return TestClient(M.app), M, tmp_path / "runs", str(montaje)


class TestSaveAreaHardening:
    def test_save_area_404_and_atomic_success(self, webapp_fixture, tmp_path, monkeypatch):
        c, M, runs, _ = webapp_fixture

        # 1. 404 en misión inexistente y cero directorio fantasma creado
        res = c.post(
            "/api/missions/mision_inexistente/save-area",
            json={
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [-75.580, 6.250],
                                    [-75.579, 6.250],
                                    [-75.579, 6.251],
                                    [-75.580, 6.251],
                                    [-75.580, 6.250],
                                ]
                            ],
                        },
                        "properties": {},
                    }
                ],
            },
        )
        assert res.status_code == 404
        assert not (runs / "mision_inexistente").exists(), "No debe crearse directorio fantasma tras 404"

        # 2. Misión existente: guardado atómico con total_area_ha > 0
        mission_dir = runs / "mision_valida"
        mission_dir.mkdir(parents=True, exist_ok=True)

        bench_dir = tmp_path / "benchmark_mock"
        bench_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("RAPTOR_BENCHMARK_DIR", str(bench_dir))

        payload = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [-75.580, 6.250],
                                [-75.579, 6.250],
                                [-75.579, 6.251],
                                [-75.580, 6.251],
                                [-75.580, 6.250],
                            ]
                        ],
                    },
                    "properties": {},
                }
            ],
        }

        res2 = c.post("/api/missions/mision_valida/save-area", json=payload)
        assert res2.status_code == 200
        body = res2.json()
        assert body["ok"] is True
        assert body["total_area_ha"] > 0
        assert body["total_area_m2"] > 0

        exp_file = mission_dir / "outputs" / "area_afectada_experto.geojson"
        root_exp = mission_dir / "area_afectada_experto.geojson"
        assert exp_file.is_file()
        assert root_exp.is_file()

        # Verificar que no se escriba en RAPTOR_BENCHMARK_DIR
        assert list(bench_dir.iterdir()) == []


class TestDeleteMissionHardening:
    def test_delete_mission_idle_and_active_guard(self, webapp_fixture):
        c, M, runs, _ = webapp_fixture

        # 1. Borrado de misión ociosa
        idle_dir = runs / "mision_idle"
        idle_dir.mkdir(parents=True, exist_ok=True)
        (idle_dir / "dummy.txt").write_text("ok", encoding="utf-8")
        res = c.delete("/api/missions/mision_idle")
        assert res.status_code == 200
        assert not idle_dir.exists()

        # 2. Error 409 si la misión está activa en _state
        active_dir = runs / "mision_activa"
        active_dir.mkdir(parents=True, exist_ok=True)

        mock_run = MagicMock()
        M._state = M.RunState(
            "mision_activa",
            active_dir,
            mock_run,
            "3d",
            False,
            False,
        )
        res_active = c.delete("/api/missions/mision_activa")
        assert res_active.status_code == 409
        assert active_dir.exists()


class TestConcurrentStartMissionLock:
    def test_concurrent_start_mission_lock(self, tmp_path, monkeypatch):
        async def _test():
            monkeypatch.setenv("RAPTOR_RUNS_ROOT", str(tmp_path / "runs"))
            from core import scan

            importlib.reload(scan)
            from webapp import main as M

            importlib.reload(M)
            M._state = None
            monkeypatch.setattr(M, "activate_mission", lambda d: None)

            m_dir = tmp_path / "runs" / "mision_concurrent" / "raw" / "rgb_thermal"
            m_dir.mkdir(parents=True, exist_ok=True)
            (m_dir / "photo1.JPG").write_bytes(b"dummy")

            async def delayed_start(self):
                await asyncio.sleep(0.05)

            async def active_read_output(self):
                await asyncio.sleep(0.5)
                yield "active line\n"

            with (
                patch.object(M.PipelineRun, "start", delayed_start),
                patch.object(M.PipelineRun, "read_output", active_read_output),
            ):

                async def call_start():
                    try:
                        return await M.start_mission(
                            mission="mision_concurrent",
                            mode="3d",
                            has_multispectral=False,
                            dband=False,
                            preset="cartografico",
                            terreno="escarpado",
                            subsample=0,
                            thermal_emissivity=None,
                            thermal_humidity=None,
                            thermal_distance=None,
                            thermal_reflection=None,
                        )
                    except M.HTTPException as exc:
                        return exc

                r1, r2 = await asyncio.gather(call_start(), call_start())
                results = [r1, r2]

                successes = [r for r in results if isinstance(r, dict) and r.get("ok")]
                conflicts = [r for r in results if isinstance(r, M.HTTPException) and r.status_code == 409]
                assert len(successes) == 1
                assert len(conflicts) == 1

        asyncio.run(_test())


class TestExportProductsArea:
    def test_export_catalog_and_area_priority(self, tmp_path, monkeypatch):
        from scripts.export_products import CATALOG
        from webapp.main import _available_export_products

        assert "area" in CATALOG
        label, kind, sources = CATALOG["area"]
        assert kind == "vector"
        assert "area_afectada_experto.geojson" in sources[0]

        m_dir = tmp_path / "mission_area"
        out_dir = m_dir / "outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        valid_feature_collection = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [-75.580, 6.250],
                                [-75.579, 6.250],
                                [-75.579, 6.251],
                                [-75.580, 6.251],
                                [-75.580, 6.250],
                            ]
                        ],
                    },
                    "properties": {"name": "test"},
                }
            ],
        }
        (out_dir / "area_afectada_experto.geojson").write_text(json.dumps(valid_feature_collection), encoding="utf-8")

        available = _available_export_products(m_dir)
        assert "area" in available

        try:
            from osgeo import gdal, ogr, osr

            has_osgeo = gdal is not None and ogr is not None and osr is not None
        except ImportError:
            has_osgeo = False

        if not has_osgeo:
            pytest.skip("osgeo no disponible en este entorno de host")

        (out_dir / "area_afectada.geojson").write_text(json.dumps(valid_feature_collection), encoding="utf-8")
        export_dest = tmp_path / "delivery"
        monkeypatch.setenv("EXPORT_DIR", str(export_dest))
        monkeypatch.setenv("EXPORT_PRODUCTS", "area")
        monkeypatch.setenv("EXPORT_VECTOR_FORMAT", "geojson")
        monkeypatch.setenv("RAPTOR_EXPORT_SOURCE_DIR", str(out_dir))

        from scripts import export_products

        importlib.reload(export_products)

        ret = export_products.main()
        assert ret == 0
        assert (export_dest / "area_afectada_experto.geojson").exists()
        assert not (export_dest / "area_afectada.geojson").exists()


class TestRaptorLauncherConfigLoader:
    def test_launcher_config_loader_preserves_spaces_and_prevents_injection(self, tmp_path):
        pwn_file = tmp_path / "pwned.txt"
        cfg_file = tmp_path / "raptor.cfg"
        cfg_file.write_text(f"""# Configuración
RAPTOR_EXPORT_DIR="/ruta con espacios/and $(touch {pwn_file})"
RAPTOR_PORT=9099
RAPTOR_RUNS_DIR='/runs con comillas'
EXTRA_VAR=ignorar
""")
        raptor_bin = Path(__file__).resolve().parent.parent / "raptor"
        bash_cmd = f"""
source <(grep -A 30 '_load_raptor_config()' "{raptor_bin}" | sed '/^cmd_/q')
_load_raptor_config "{cfg_file}"
printf '%s\\n' "$RAPTOR_EXPORT_DIR"
printf '%s\\n' "$RAPTOR_PORT"
printf '%s\\n' "$RAPTOR_RUNS_DIR"
printf '%s\\n' "${{EXTRA_VAR:-UNSET}}"
"""
        proc = subprocess.run(["bash", "-c", bash_cmd], capture_output=True, text=True, check=True)
        lines = proc.stdout.strip().split("\n")
        assert lines[0] == f"/ruta con espacios/and $(touch {pwn_file})"
        assert lines[1] == "9099"
        assert lines[2] == "/runs con comillas"
        assert lines[3] == "UNSET"
        assert not pwn_file.exists(), "No debe ejecutarse payload command injection"


class TestStripProcessingParity:
    def test_strip_processing_parity(self, tmp_path, monkeypatch):
        try:
            from osgeo import gdal

            if gdal is None:
                pytest.skip("gdal no disponible")
        except ImportError:
            pytest.skip("gdal no disponible")

        import numpy as np

        # 1. compute_thermal_hotspot con STRIP_ROWS=2
        H, W = 10, 8
        temp_data = np.array(
            [
                [25.0, 30.0, 35.0, 42.0, 45.0, 62.0, 70.0, 95.0],
                [20.0, 22.0, 39.0, 41.0, 50.0, 65.0, 89.0, 100.0],
            ]
            * 5,
            dtype=np.float32,
        )

        th_path = tmp_path / "thermal_orthomosaic.tif"
        drv = gdal.GetDriverByName("GTiff")
        ds = drv.Create(str(th_path), W, H, 1, gdal.GDT_Float32)
        ds.SetGeoTransform([0, 1, 0, 0, 0, -1])
        ds.GetRasterBand(1).WriteArray(temp_data)
        ds.FlushCache()
        ds = None

        from scripts import compute_thermal_hotspot

        out_path_strip = tmp_path / "termico_hotspot_class.tif"
        monkeypatch.setattr(compute_thermal_hotspot, "TH_PATH", str(th_path))
        monkeypatch.setattr(compute_thermal_hotspot, "OUT_PATH", str(out_path_strip))
        monkeypatch.setattr(compute_thermal_hotspot, "STRIP_ROWS", 2)

        compute_thermal_hotspot.main()

        res_strip_ds = gdal.Open(str(out_path_strip))
        res_strip_arr = res_strip_ds.GetRasterBand(1).ReadAsArray()
        res_strip_ds = None

        assert res_strip_arr.shape == (H, W)
        assert np.all((res_strip_arr >= 0) & (res_strip_arr <= 4))

        # 2. trim_rgb con STRIP_ROWS=2
        from scripts import trim_low_overlap_edges

        rgb_in = tmp_path / "rgb_in.tif"
        rgb_out = tmp_path / "rgb_out.tif"
        ds_rgb = drv.Create(str(rgb_in), W, H, 4, gdal.GDT_Byte)
        ds_rgb.SetGeoTransform([0, 1, 0, 0, 0, -1])
        for b in range(3):
            ds_rgb.GetRasterBand(b + 1).WriteArray(np.full((H, W), 100 + b * 20, dtype=np.uint8))
        ds_rgb.GetRasterBand(4).WriteArray(np.full((H, W), 255, dtype=np.uint8))
        ds_rgb.FlushCache()
        ds_rgb = None

        monkeypatch.setattr(trim_low_overlap_edges, "RGB_ODM_SRC", str(rgb_in))
        monkeypatch.setattr(trim_low_overlap_edges, "RGB_PATH", str(rgb_out))
        monkeypatch.setattr(trim_low_overlap_edges, "STRIP_ROWS", 2)
        monkeypatch.setattr(trim_low_overlap_edges, "_apply_hull", lambda valid, *args, **kwargs: valid)

        trim_low_overlap_edges.trim_rgb()

        res_rgb_ds = gdal.Open(str(rgb_out))
        assert res_rgb_ds.RasterCount == 4
        assert res_rgb_ds.RasterXSize == W
        assert res_rgb_ds.RasterYSize == H
        assert res_rgb_ds.GetRasterBand(4).GetColorInterpretation() == gdal.GCI_AlphaBand
        rgb_alpha = res_rgb_ds.GetRasterBand(4).ReadAsArray()
        assert np.all(rgb_alpha == 255)
        for b in range(3):
            band_arr = res_rgb_ds.GetRasterBand(b + 1).ReadAsArray()
            assert np.all(band_arr == 100 + b * 20)
        res_rgb_ds = None

        # 3. compute_vegetation_indices con STRIP_ROWS=2
        from scripts import compute_vegetation_indices

        ms_in = tmp_path / "ms_in.tif"
        ms_out_dir = tmp_path / "indices_out"
        ds_ms = drv.Create(str(ms_in), W, H, 5, gdal.GDT_Float32)
        ds_ms.SetGeoTransform([0, 1, 0, 0, 0, -1])
        band_names = ["NIR", "Red", "Green", "RedEdge", "Alpha"]
        for i, name in enumerate(band_names):
            b = ds_ms.GetRasterBand(i + 1)
            b.SetDescription(name)
            if name == "Alpha":
                b.SetColorInterpretation(gdal.GCI_AlphaBand)
                b.WriteArray(np.full((H, W), 255.0, dtype=np.float32))
            elif name == "NIR":
                b.WriteArray(np.full((H, W), 0.5, dtype=np.float32))
            elif name == "Red":
                b.WriteArray(np.full((H, W), 0.2, dtype=np.float32))
            else:
                b.WriteArray(np.full((H, W), 0.3, dtype=np.float32))
        ds_ms.FlushCache()
        ds_ms = None

        monkeypatch.setattr(compute_vegetation_indices, "MS_PATH", str(ms_in))
        monkeypatch.setattr(compute_vegetation_indices, "OUT_DIR", str(ms_out_dir))
        monkeypatch.setattr(compute_vegetation_indices, "STRIP_ROWS", 2)

        compute_vegetation_indices.main()

        ndvi_path = ms_out_dir / "ndvi.tif"
        assert ndvi_path.exists()
        ndvi_ds = gdal.Open(str(ndvi_path))
        ndvi_arr = ndvi_ds.GetRasterBand(1).ReadAsArray()
        expected_ndvi = (0.5 - 0.2) / (0.5 + 0.2)
        assert np.allclose(ndvi_arr, expected_ndvi, atol=1e-5)
        ndvi_ds = None
