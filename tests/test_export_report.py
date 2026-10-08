"""scripts/export_report.py — informe de emergencia HTML imprimible con los
números reales del pipeline (situation, cobertura vs vuelo, productos).
"""
import json
import os
import subprocess

import numpy as np
from osgeo import gdal

gdal.UseExceptions()

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "export_report.py")


def _minimal_outputs(tmp_path):
    outputs = tmp_path / "outputs"
    outputs.mkdir(exist_ok=True)
    (outputs / "situation.json").write_text(json.dumps({
        "hotspots_activos": 2,
        "hotspots": [{"lat": 6.334963, "lon": -75.488735, "px": 126,
                      "temp_c": 125.9}],
        "temp_max": 125.9, "temp_promedio": 34.2, "confianza": "media",
        "cobertura_pct": 55.0,
        "captura": "2026-08-08T11:49:18",
        "alerta_cobertura_baja": True,
    }))
    (outputs / "coverage.json").write_text(json.dumps({
        "area_volada_km2": 3.14, "alerta": True,
        "productos": {"rgb": {"cobertura_area_volada_pct": 44.9}},
    }))
    (outputs / "flight_quality.json").write_text(json.dumps({
        "equipo": "DJI Mavic 3T", "calidad": "buena", "solape_p50": 80.1,
        "velocidad_media_ms": 6.9,
    }))
    (outputs / "run_summary.json").write_text(json.dumps({
        "terminado": "2026-08-11T09:27:54+00:00", "productos": {
            "rgb": {"gsd_m": 0.15, "crs": "EPSG:32618", "cobertura_pct": 44.9},
        },
    }))
    # Vista previa: 1×1 GTiff es suficiente para que el data-URI salga.
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(str(outputs / "rgb_orthomosaic.tif"), 1, 1, 1, gdal.GDT_Byte)
    ds.SetGeoTransform((0, 1, 0, 1, 0, -1))
    ds.SetProjection('EPSG:32618')
    ds.GetRasterBand(1).WriteArray(np.array([[255]], dtype=np.uint8))
    ds = None
    return outputs


class TestInforme:
    def test_genera_el_html_con_los_numeros(self, tmp_path):
        _minimal_outputs(tmp_path)
        r = subprocess.run(["python3", SCRIPT], cwd=str(tmp_path),
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr
        html = (tmp_path / "outputs" / "reporte_emergencia.html").read_text()
        assert "Informe de emergencia" in html
        assert "125.9" in html            # temperatura pico del foco
        assert "44.9" in html             # cobertura vs vuelo y del ráster
        assert "Cobertura baja" in html   # aviso embebido
        assert "data:image/png;base64," in html  # vista previa embebida
        assert "DJI Mavic 3T" in html

    def test_copia_a_la_carpeta_de_entrega(self, tmp_path, monkeypatch):
        _minimal_outputs(tmp_path)
        entrega = tmp_path / "entrega"
        entrega.mkdir()
        env = dict(os.environ, EXPORT_DIR=str(entrega))
        r = subprocess.run(["python3", SCRIPT], cwd=str(tmp_path),
                           capture_output=True, text=True, timeout=60, env=env)
        assert r.returncode == 0
        assert (entrega / "reporte_emergencia.html").exists()

    def test_sin_datos_de_impacto_no_revienta(self, tmp_path):
        """Ya no existen los modos "sin_impacto_detectado"/"solo_termico"
        (situation.json siempre tiene la misma forma térmica única, ver
        compute_situation_summary.py) — el único caso real de "sin datos de
        impacto" es que situation.json ni siquiera exista (misión sin
        térmico, o que no llegó a esa etapa)."""
        outputs = tmp_path / "outputs"
        outputs.mkdir(exist_ok=True)
        r = subprocess.run(["python3", SCRIPT], cwd=str(tmp_path),
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0
        html = (tmp_path / "outputs" / "reporte_emergencia.html").read_text()
        assert "Sin datos de impacto" in html

    def test_con_situation_pero_sin_focos_muestra_las_tarjetas_igual(self, tmp_path):
        """Con situation.json presente pero hotspots_activos=0 (ninguna
        detección, misión sí llegó a esa etapa), las 4 tarjetas ejecutivas
        se muestran igual, con los valores reales (0 focos, temperaturas si
        las hay) — no el mensaje de "sin datos"."""
        outputs = tmp_path / "outputs"
        outputs.mkdir(exist_ok=True)
        (outputs / "situation.json").write_text(json.dumps({
            "hotspots_activos": 0, "hotspots": [],
            "temp_max": 25.0, "temp_promedio": 22.0,
            "cobertura_pct": 100.0, "confianza": "alta", "captura": None,
        }))
        r = subprocess.run(["python3", SCRIPT], cwd=str(tmp_path),
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0
        html = (tmp_path / "outputs" / "reporte_emergencia.html").read_text()
        assert "Sin datos de impacto" not in html
        assert "Focos térmicos activos" in html
