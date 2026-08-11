"""compute_situation_summary.py en modo SOLO TÉRMICO (sin multiespectral).

Antes situation.json se omitía por completo sin severidad_class.tif +
area_afectada.geojson (que EXIGEN multiespectral, ver detect_area_afectada.py:
su señal primaria es NDVI) — una misión RGB+térmico sin M3M nunca tenía
situation.json, así que ni siquiera la fecha de captura (que sale de
flight_path.geojson, independiente del sensor) llegaba a mostrarse en el
geovisor. Esto verifica el modo nuevo de punta a punta con GDAL real.
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


class TestSoloTermico:
    def test_genera_situation_json_sin_multiespectral(self, mision):
        """El caso puntual reportado: RGB+térmico sin M3M no tenía
        situation.json en absoluto — ni la fecha de captura."""
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

        assert s["solo_termico"] is True
        assert s["captura"] == "2026-08-01T10:05:00", \
            "la fecha de captura tiene que salir de flight_path.geojson, no quedar en None"
        assert s["area_ha"] is None
        assert s["severidad"] is None
        assert s["vegetacion_comprometida_pct"] is None
        assert s["hotspots_activos"] == 1
        assert s["hotspots"][0]["temp_c"] == 95.0
        assert s["hotspots"][0]["severidad"] is None
        # Reportado: sin focos activos, el resumen (y la imagen exportada)
        # no mostraban NINGÚN valor de temperatura — solo el conteo de focos.
        # temp_max/temp_promedio salen de TODO el ortomosaico térmico, no
        # solo de los focos, así que siempre están disponibles.
        assert s["temp_max"] == 95.0
        assert s["temp_promedio"] == pytest.approx(21.2, abs=0.05)
        assert s["cobertura_pct"] == 100.0

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

    def test_sin_ni_severidad_ni_hotspot_omite_el_archivo(self, mision, capsys):
        """Ninguno de los dos insumos (ni multiespectral, ni siquiera
        térmico): sigue sin haber nada que resumir."""
        assert _correr() == 0
        assert not os.path.isfile("outputs/situation.json")
        assert "omitiendo situation.json" in capsys.readouterr().out


class TestSinImpactoDetectado:
    """Misión CON multiespectral+térmico, la corrida completa (detect_area_
    afectada.py SÍ corrió — _deteccion_data.npz existe) pero no encontró
    ningún píxel que superara el umbral. Bug real, reportado en vivo: el
    geovisor mostraba "esta misión no tiene ni multiespectral+térmico ni
    térmico solo, o la corrida no llegó a esa etapa" para una misión que
    tenía LOS DOS sensores y SÍ había terminado — el mensaje era
    directamente falso."""

    def test_escribe_situation_json_en_vez_de_omitirlo(self, mision):
        n = 20
        temp_abs = np.full((n, n), 25.0, np.float32)
        temp_abs[0, 0] = 45.0
        valid = np.ones((n, n), bool)
        np.savez("outputs/_deteccion_data.npz", z_severidad=np.zeros((n, n), np.float32),
                 temp_abs=temp_abs, valid=valid, ndvi=np.zeros((n, n), np.float32), ref_pop_size=100)
        _flight_path("outputs/flight_path.geojson", ["2026-08-06T09:00:00"])

        assert _correr() == 0
        assert os.path.isfile("outputs/situation.json"), \
            "con el cache de detección presente, situation.json NO debe omitirse aunque no haya área/hotspot"
        with open("outputs/situation.json") as f:
            s = json.load(f)
        assert s["sin_impacto_detectado"] is True
        assert s["solo_termico"] is False
        assert s["area_ha"] is None
        assert s["severidad"] is None
        assert s["vegetacion_comprometida_pct"] is None
        assert s["hotspots_activos"] == 0
        assert s["hotspots"] == []
        assert s["captura"] == "2026-08-06T09:00:00"
        # temp_max/promedio SÍ se pueden reportar — vienen del cache, no de
        # un polígono que no existe.
        assert s["temp_max"] == 45.0
        assert s["temp_promedio"] == pytest.approx(25.05, abs=0.1)

    def test_prioriza_area_completa_por_encima_de_este_modo(self, mision):
        """Si de verdad hay severidad+área, ese modo gana aunque el cache
        de detección también exista — no tiene sentido degradar un
        resultado completo a "sin impacto"."""
        n = 20
        temp_abs = np.full((n, n), 25.0, np.float32)
        valid = np.ones((n, n), bool)
        np.savez("outputs/_deteccion_data.npz", z_severidad=np.zeros((n, n), np.float32),
                 temp_abs=temp_abs, valid=valid, ndvi=np.zeros((n, n), np.float32), ref_pop_size=100)

        sev = np.ones((n, n), np.uint8)
        s_ds = gdal.GetDriverByName("GTiff").Create("outputs/severidad_class.tif", n, n, 1, gdal.GDT_Byte)
        s_ds.SetGeoTransform(GT); s_ds.SetProjection(_wkt())
        s_ds.GetRasterBand(1).WriteArray(sev); s_ds.GetRasterBand(1).SetNoDataValue(0); s_ds = None

        wgs = osr.SpatialReference(); wgs.ImportFromEPSG(4326)
        wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        from osgeo import ogr
        v = ogr.GetDriverByName("GeoJSON").CreateDataSource("outputs/area_afectada.geojson")
        lyr = v.CreateLayer("a", wgs, ogr.wkbPolygon)
        lyr.CreateField(ogr.FieldDefn("area_m2", ogr.OFTReal))
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetGeometry(ogr.CreateGeometryFromWkt(
            "POLYGON((-75.5 6.4,-75.4 6.4,-75.4 6.5,-75.5 6.5,-75.5 6.4))"))
        f.SetField("area_m2", 10000.0)
        lyr.CreateFeature(f); v = None
        _flight_path("outputs/flight_path.geojson", ["2026-08-06T09:00:00"])

        assert _correr() == 0
        with open("outputs/situation.json") as f:
            s = json.load(f)
        assert "sin_impacto_detectado" not in s
        assert s["area_ha"] == 1.0


class TestModoCompletoSigueIgual:
    """El modo CON multiespectral no debería haber cambiado de comportamiento
    con el refactor — mismo resultado que antes."""

    def test_con_severidad_y_area_no_es_solo_termico(self, mision):
        n = 30
        sev = np.ones((n, n), np.uint8)
        sev[0:5, 0:5] = 4
        s_ds = gdal.GetDriverByName("GTiff").Create("outputs/severidad_class.tif", n, n, 1, gdal.GDT_Byte)
        s_ds.SetGeoTransform(GT); s_ds.SetProjection(_wkt())
        s_ds.GetRasterBand(1).WriteArray(sev); s_ds.GetRasterBand(1).SetNoDataValue(0); s_ds = None

        wgs = osr.SpatialReference(); wgs.ImportFromEPSG(4326)
        wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        from osgeo import ogr
        v = ogr.GetDriverByName("GeoJSON").CreateDataSource("outputs/area_afectada.geojson")
        lyr = v.CreateLayer("a", wgs, ogr.wkbPolygon)
        lyr.CreateField(ogr.FieldDefn("area_m2", ogr.OFTReal))
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetGeometry(ogr.CreateGeometryFromWkt(
            "POLYGON((-75.5 6.4,-75.4 6.4,-75.4 6.5,-75.5 6.5,-75.5 6.4))"))
        f.SetField("area_m2", 50000.0)
        lyr.CreateFeature(f); v = None

        _flight_path("outputs/flight_path.geojson", ["2026-08-01T11:00:00"])
        assert _correr() == 0
        with open("outputs/situation.json") as f:
            s = json.load(f)
        assert s["solo_termico"] is False
        assert s["area_ha"] == 5.0
        assert s["severidad"]["dominante"] in ("leve", "moderado", "severo")
        assert s["captura"] == "2026-08-01T11:00:00"
        # Sin _deteccion_data.npz (no se generó en este test) cobertura_pct
        # cae al respaldo de 100.0 — el mismo criterio que confianza="alta"
        # por defecto, no un valor inventado aparte.
        assert s["cobertura_pct"] == 100.0
