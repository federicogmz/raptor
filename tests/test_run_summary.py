"""Resumen JSON de la corrida (automatización y CI).

Es el contrato que consume `./raptor run --json`. Lo que más importa: que se
genere TAMBIÉN cuando la corrida falla —en CI, saber en qué etapa murió y qué
alcanzó a producir vale tanto como el código de salida— y que las rutas que
reporta sean las del HOST, no las internas del contenedor, que no le sirven a
quien lee el resumen desde afuera.
"""
import json
import os
import sys

import numpy as np
import pytest
from osgeo import gdal, ogr, osr

import run_summary

gdal.UseExceptions()
GT = (4744868.0, 0.2, 0.0, 2267229.0, 0.0, -0.2)


def _wkt(epsg):
    s = osr.SpatialReference()
    s.ImportFromEPSG(epsg)
    return s.ExportToWkt()


@pytest.fixture
def salida(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("outputs/indices")
    n = 600
    # Alpha con cobertura conocida: 400x400 de 600x600 = 44.4%
    alpha = np.zeros((n, n), np.uint8)
    alpha[100:500, 100:500] = 255
    rgb = np.concatenate([np.full((3, n, n), 120, np.uint8), alpha[np.newaxis, ...]])
    ds = gdal.GetDriverByName("GTiff").Create("outputs/rgb_orthomosaic.tif", n, n, 4, gdal.GDT_Byte)
    ds.SetGeoTransform(GT); ds.SetProjection(_wkt(9377))
    ds.GetRasterBand(4).SetColorInterpretation(gdal.GCI_AlphaBand)
    for i in range(4):
        ds.GetRasterBand(i + 1).WriteArray(rgb[i])
    ds = None

    wgs = osr.SpatialReference(); wgs.ImportFromEPSG(4326)
    wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    v = ogr.GetDriverByName("GeoJSON").CreateDataSource("outputs/area_afectada.geojson")
    lyr = v.CreateLayer("a", wgs, ogr.wkbPolygon)
    for _ in range(3):
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetGeometry(ogr.CreateGeometryFromWkt(
            "POLYGON((-75.5 6.4,-75.4 6.4,-75.4 6.5,-75.5 6.5,-75.5 6.4))"))
        lyr.CreateFeature(f)
    v = None

    for k in list(os.environ):
        if k.startswith("EXPORT_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MODE", "rgb+thermal")
    return tmp_path


def _correr(monkeypatch, codigo="0"):
    """run_summary lee el código de salida de sys.argv."""
    monkeypatch.setattr(sys, "argv", ["run_summary.py", codigo])
    return run_summary.main()


def _leer():
    with open("outputs/run_summary.json") as f:
        return json.load(f)


class TestResumen:
    def test_describe_los_productos_generados(self, salida, monkeypatch):
        assert _correr(monkeypatch) == 0
        r = _leer()
        assert r["ok"] is True and r["exit_code"] == 0
        assert "rgb" in r["productos"]
        p = r["productos"]["rgb"]
        assert p["crs"] == "EPSG:9377"
        assert p["bandas"] == 4
        assert abs(p["gsd_m"] - 0.2) < 1e-6

    def test_mide_cobertura_real(self, salida, monkeypatch):
        """El número que dice si el recorte de bordes dejó algo utilizable."""
        _correr(monkeypatch)
        cob = _leer()["productos"]["rgb"]["cobertura_pct"]
        assert 40 < cob < 50, f"se esperaba ~44%, dio {cob}%"

    def test_cuenta_entidades_vectoriales(self, salida, monkeypatch):
        _correr(monkeypatch)
        assert _leer()["vectores"]["area_afectada"]["entidades"] == 3

    def test_se_genera_tambien_si_la_corrida_falla(self, salida, monkeypatch):
        """Lo que hace útil el resumen en CI: 137 es un OOM kill, y saber qué
        productos alcanzó a escribir antes de morir orienta el diagnóstico."""
        assert _correr(monkeypatch, "137") == 0
        r = _leer()
        assert r["ok"] is False and r["exit_code"] == 137
        assert "rgb" in r["productos"], "el resumen de un fallo igual lista lo producido"

    def test_la_entrega_apunta_al_host(self, salida, monkeypatch, tmp_path):
        """EXPORT_DIR es la ruta interna del contenedor; el resumen tiene que
        reportar la del host, que es la única que le sirve a quien lo lee."""
        entrega = tmp_path / "entrega"
        entrega.mkdir()
        monkeypatch.setenv("EXPORT_DIR", str(entrega))
        monkeypatch.setenv("EXPORT_HOST_DIR", "/home/usuario/entregas/la_clara")
        monkeypatch.setenv("EXPORT_EPSG", "9377")
        _correr(monkeypatch)
        r = _leer()
        assert r["entrega"]["carpeta"] == "/home/usuario/entregas/la_clara"
        assert r["entrega"]["crs"] == "9377"

    def test_deja_copia_en_la_carpeta_de_entrega(self, salida, monkeypatch, tmp_path):
        """Quien recibe los productos ve con qué corrida salieron, sin acceso
        al directorio de trabajo."""
        entrega = tmp_path / "entrega"
        entrega.mkdir()
        monkeypatch.setenv("EXPORT_DIR", str(entrega))
        _correr(monkeypatch)
        assert (entrega / "run_summary.json").is_file()

    def test_es_json_valido_y_serializable(self, salida, monkeypatch):
        _correr(monkeypatch)
        # Round-trip: si algo quedó como tipo numpy, json.dump ya habría fallado
        assert json.loads(json.dumps(_leer()))["modo"] == "rgb+thermal"
