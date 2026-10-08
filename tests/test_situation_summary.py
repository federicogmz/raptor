"""compute_situation_summary.py: resumen de situación (outputs/situation.json).

Antes había tres modos posibles (_resumen_completo para multiespectral+
térmico, _resumen_solo_termico para RGB+térmico sin M3M, _resumen_sin_
impacto para una corrida completa que no detectó área). El módulo que
generaba los dos primeros insumos multiespectral (severidad_class.tif +
area_afectada.geojson, vía detect_area_afectada.py/compute_severity_
classes.py) fue eliminado por completo. Ahora hay UN SOLO modo, siempre con
esta forma (antes era la forma exclusiva de "solo térmico"), para toda
misión con térmico, con o sin multiespectral: focos térmicos discretos
(temperatura ABSOLUTA, no depende de NDVI) + confianza por cobertura del
ortomosaico térmico + fecha de captura desde flight_path.geojson.
"""
import json
import os

import numpy as np
import pytest
from osgeo import gdal, osr

gdal.UseExceptions()

GT = (466000.0, 0.2, 0.0, 708900.0, 0.0, -0.2)


def _wkt(epsg=32618):
    s = osr.SpatialReference()
    s.ImportFromEPSG(epsg)
    return s.ExportToWkt()


def _thermal(path, temps_2d):
    h, w = temps_2d.shape
    ds = gdal.GetDriverByName("GTiff").Create(path, w, h, 1, gdal.GDT_Float32)
    ds.SetGeoTransform(GT)
    ds.SetProjection(_wkt())
    ds.GetRasterBand(1).WriteArray(temps_2d.astype(np.float32))
    ds.GetRasterBand(1).SetNoDataValue(float("nan"))
    ds = None


def _hotspot_class(path, clases_2d):
    h, w = clases_2d.shape
    ds = gdal.GetDriverByName("GTiff").Create(path, w, h, 1, gdal.GDT_Byte)
    ds.SetGeoTransform(GT)
    ds.SetProjection(_wkt())
    ds.GetRasterBand(1).WriteArray(clases_2d.astype(np.uint8))
    ds.GetRasterBand(1).SetNoDataValue(0)
    ds = None


def _flight_path(path, tiempos):
    features = [{"type": "Feature", "properties": {"kind": "capture", "time": t},
                "geometry": {"type": "Point", "coordinates": [-75.5, 6.4]}} for t in tiempos]
    with open(path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)


@pytest.fixture
def mision(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("outputs/indices", exist_ok=True)
    return tmp_path


def _correr():
    import importlib
    import compute_situation_summary as S
    importlib.reload(S)
    return S.main()


class TestResumen:
    def test_genera_situation_json_con_focos_activos(self, mision):
        n = 40
        temps = np.full((n, n), 20.0)
        temps[5:10, 5:10] = 95.0  # un foco activo real (>=88°C)
        _thermal("outputs/thermal_orthomosaic.tif", temps)
        clases = np.ones((n, n), np.uint8)
        clases[5:10, 5:10] = 4
        _hotspot_class("outputs/termico_hotspot_class.tif", clases)
        _flight_path("outputs/flight_path.geojson",
                     ["2026-08-01T10:00:00", "2026-08-01T10:05:00"])

        assert _correr() == 0
        assert os.path.isfile("outputs/situation.json")
        with open("outputs/situation.json") as f:
            s = json.load(f)

        assert s["captura"] == "2026-08-01T10:05:00", \
            "la fecha de captura tiene que salir de flight_path.geojson, no quedar en None"
        assert s["hotspots_activos"] == 1
        assert s["hotspots"][0]["temp_c"] == 95.0
        assert "severidad" not in s["hotspots"][0], \
            "sin polígono de área afectada, el hotspot ya no lleva severidad"
        # Reportado: sin focos activos, el resumen (y la imagen exportada)
        # no mostraban NINGÚN valor de temperatura — solo el conteo de focos.
        # temp_max/temp_promedio salen de TODO el ortomosaico térmico, no
        # solo de los focos, así que siempre están disponibles.
        assert s["temp_max"] == 95.0
        assert s["temp_promedio"] == pytest.approx(21.2, abs=0.05)
        assert s["cobertura_pct"] == 100.0
        # Las claves del modo multiespectral (área/severidad/vegetación),
        # producidas por módulos ya eliminados, no existen más en absoluto.
        for clave in ("solo_termico", "area_ha", "severidad",
                      "vegetacion_comprometida_pct", "sin_impacto_detectado"):
            assert clave not in s, f"'{clave}' ya no debería aparecer en situation.json"

    def test_sin_focos_activos_confianza_por_cobertura_termica(self, mision):
        n = 20
        temps = np.full((n, n), 25.0)
        _thermal("outputs/thermal_orthomosaic.tif", temps)
        _hotspot_class("outputs/termico_hotspot_class.tif", np.ones((n, n), np.uint8))
        _flight_path("outputs/flight_path.geojson", ["2026-08-01T09:00:00"])
        _correr()
        with open("outputs/situation.json") as f:
            s = json.load(f)
        assert s["hotspots_activos"] == 0
        assert s["confianza"] == "alta"  # cobertura 100% del ortomosaico
        assert s["cobertura_pct"] == 100.0
        # Reportado: sin focos activos no había NINGÚN dato de temperatura
        # que mostrar en el resumen — pero sí hay temperatura real medida,
        # simplemente ningún píxel superó el umbral de foco activo.
        assert s["temp_max"] == 25.0
        assert s["temp_promedio"] == 25.0

    def test_con_multiespectral_tambien_no_cambia_de_forma(self, mision):
        """Antes CON multiespectral el resumen tenía una forma distinta
        (área/severidad). Ahora es siempre la misma forma térmica, exista o
        no exista multiespectral en la misión — este test solo constata que
        la presencia de outputs/indices/*.tif (señal de que hubo M3M) no
        cambia nada del resultado."""
        n = 20
        temps = np.full((n, n), 30.0)
        _thermal("outputs/thermal_orthomosaic.tif", temps)
        _hotspot_class("outputs/termico_hotspot_class.tif", np.ones((n, n), np.uint8))
        _flight_path("outputs/flight_path.geojson", ["2026-08-01T11:00:00"])
        ndvi_ds = gdal.GetDriverByName("GTiff").Create(
            "outputs/indices/ndvi.tif", n, n, 1, gdal.GDT_Float32)
        ndvi_ds.SetGeoTransform(GT); ndvi_ds.SetProjection(_wkt())
        ndvi_ds.GetRasterBand(1).WriteArray(np.full((n, n), 0.5, np.float32))
        ndvi_ds = None

        assert _correr() == 0
        with open("outputs/situation.json") as f:
            s = json.load(f)
        assert s["captura"] == "2026-08-01T11:00:00"
        assert s["temp_max"] == 30.0
        for clave in ("solo_termico", "area_ha", "severidad"):
            assert clave not in s

    def test_sin_hotspot_ni_termico_omite_el_archivo(self, mision, capsys):
        """Sin outputs/termico_hotspot_class.tif ni outputs/thermal_
        orthomosaic.tif, no hay nada que resumir — main() lo omite sin
        error."""
        assert _correr() == 0
        assert not os.path.isfile("outputs/situation.json")
        assert "omitiendo situation.json" in capsys.readouterr().out
