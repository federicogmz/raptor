"""Pruebas del modo de solo conversión térmica (R-JPEG → GeoTIFF Float32).

Verifica que el nuevo modo 'thermal-convert' permita procesar y descargar imágenes
térmicas sin requerir fotos RGB ni ejecutar reconstrucción 3D con ODM.
"""
import importlib
import pytest


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("RAPTOR_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("EXPORT_HOST_DIR", "/home/usuario/entregas")
    montaje = tmp_path / "export"
    montaje.mkdir()
    monkeypatch.setenv("EXPORT_MOUNT", str(montaje))
    from fastapi.testclient import TestClient
    from core import scan
    importlib.reload(scan)
    from webapp import main as M
    importlib.reload(M)
    M._state = None
    return TestClient(M.app), M, tmp_path / "runs", str(montaje)


class TestThermalConvertValidation:
    def test_thermal_convert_pasa_con_solo_termicas(self, app):
        c, M, runs, montaje = app
        uploads = {
            "rgb_thermal": M.classify_files(["DJI_0001_T.JPG", "DJI_0002_T.JPG"]),
            "multispectral": M.classify_files([])
        }
        errs = M._validate("thermal-convert", False, uploads)
        assert errs == [], f"Con solo fotos térmicas debe pasar: {errs}"

    def test_thermal_convert_falla_sin_termicas(self, app):
        c, M, runs, montaje = app
        uploads = {
            "rgb_thermal": M.classify_files(["DJI_0001_V.JPG"]),
            "multispectral": M.classify_files([])
        }
        errs = M._validate("thermal-convert", False, uploads)
        assert len(errs) > 0
        assert "no hay fotos térmicas" in errs[0].lower()

    def test_quality_estimate_para_thermal_convert(self, app):
        c, M, runs, montaje = app
        res = c.get("/api/missions/mision_test/quality-estimate?mode=thermal-convert&lang=es")
        assert res.status_code == 200
        data = res.json()
        assert "1 minuto" in data["tiempo_texto"]
        assert "Float32" in data["resolucion_texto"]


class TestThermalConvertDownloadAndStatus:
    def test_status_y_descarga_zip_termicas(self, app):
        c, M, runs, montaje = app
        m_dir = runs / "mision_termica"
        outputs = m_dir / "outputs"
        outputs.mkdir(parents=True, exist_ok=True)
        raw = m_dir / "raw" / "rgb_thermal"
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "DJI_0001_T.JPG").write_text("dummy")

        # Antes de tener outputs
        st = c.get("/api/missions/mision_termica/status").json()
        assert st["has_thermal_converted"] is False

        # Simulamos que se generó outputs/termicas_convertidas_tiff.zip
        zip_file = outputs / "termicas_convertidas_tiff.zip"
        zip_file.write_bytes(b"PK\x05\x06" + b"\x00" * 18)  # empty zip

        st2 = c.get("/api/missions/mision_termica/status").json()
        assert st2["has_thermal_converted"] is True

        # Descarga
        dl = c.get("/api/missions/mision_termica/thermal-tiffs-zip")
        assert dl.status_code == 200
        assert dl.headers["content-type"] == "application/zip"
