"""generate_tiles.py: zoom derivado de la resolución real + qué capas se
tesela de verdad (capas_disponibles en bounds.json).

El módulo corre TODO su cuerpo al importarse (no tiene guarda
`if __name__=='__main__'`), así que "correrlo" es sencillamente importarlo con
el cwd ya preparado — mismo patrón que el resto de la suite usa para este
archivo (ver tests/test_trim_characterization.py para el precedente con
trim_low_overlap_edges.py, que sí tiene la guarda).
"""
import json
import os
import sys

import numpy as np
import pytest
from osgeo import gdal, osr

gdal.UseExceptions()

REPO_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")


def _raster(path, px_m, epsg=32618, w=64, h=64, dtype=gdal.GDT_Byte, nodata=None):
    s = osr.SpatialReference()
    s.ImportFromEPSG(epsg)
    ds = gdal.GetDriverByName("GTiff").Create(path, w, h, 1, dtype)
    ds.SetGeoTransform((466000.0, px_m, 0.0, 708900.0, 0.0, -px_m))
    ds.SetProjection(s.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(np.zeros((h, w), np.uint8))
    if nodata is not None:
        ds.GetRasterBand(1).SetNoDataValue(nodata)
    ds = None
    return path


def _dsm():
    """generate_tiles.py solo escribe bounds.json si existe AL MENOS uno de
    RGB/térmico/MS/DSM (necesita un centro real para el geovisor) — no es
    parte de lo que prueba este archivo, así que los tests de
    capas_disponibles siempre ponen un DSM mínimo para que el resto del
    bloque corra."""
    os.makedirs("outputs", exist_ok=True)
    _raster("outputs/dsm.tif", 0.1, dtype=gdal.GDT_Float32)


@pytest.fixture
def correr(tmp_path, monkeypatch):
    """Devuelve una función que importa (=corre) generate_tiles.py DESPUÉS de
    que el test prepare outputs/ — así se ejercita el código real, no una
    simulación aparte de su lógica."""
    monkeypatch.chdir(tmp_path)

    def _ejecutar():
        sys.modules.pop("generate_tiles", None)
        if REPO_SCRIPTS not in sys.path:
            sys.path.insert(0, REPO_SCRIPTS)
        import generate_tiles as G
        return G
    return _ejecutar


def _bounds():
    with open("geovisor/tiles/bounds.json") as f:
        return json.load(f)


class TestZoomRange:
    """zoom_range() es una función pura: no hace falta correr todo el
    módulo, solo importarlo (con cwd escribible, por los efectos de
    importación) y llamarla directo."""

    @pytest.mark.parametrize("px_m,esperado,desc", [
        (0.08, 21, "RGB/DSM a 8 cm — antes cortaba en 20 (14.8 cm) y tiraba detalle"),
        (0.21, 20, "térmico a 21 cm"),
        (0.02, 23, "hipotético 2 cm"),
        (1.20, 17, "grueso 1.2 m"),
    ])
    def test_deriva_el_zoom_de_la_resolucion_real(self, correr, tmp_path, px_m, esperado, desc):
        G = correr()
        p = _raster(str(tmp_path / f"t_{px_m}.tif"), px_m)
        assert G.zoom_range(p) == esperado, desc

    def test_nunca_supera_el_tope_duro(self, correr, tmp_path):
        G = correr()
        p = _raster(str(tmp_path / "milimetrico.tif"), 0.0001)
        assert G.zoom_range(p) == G.ZOOM_MAX_HARD


class TestCapasDisponibles:
    """El signal que usa el geovisor (app.js, CAPAS_DISPONIBLES) para no
    ofrecer en el panel severidad/hotspot/índices clasificados cuando la
    misión no tiene los datos de origen."""

    def test_sin_impacto_la_lista_no_los_incluye(self, correr):
        """Un DSM solo (sin severidad/hotspot/índices) no debe listar
        ninguno de esos tres — es el caso de una misión sin resultados de
        impacto todavía, o que nunca los va a tener (sin multiespectral)."""
        _dsm()
        correr()
        b = _bounds()["capas_disponibles"]
        assert not ({"severidad", "hotspot_termico", "ndvi_class"} & set(b)), b

    def test_solo_lista_lo_que_realmente_existe(self, correr):
        _dsm()
        os.makedirs("outputs/indices", exist_ok=True)
        _raster("outputs/severidad_class.tif", 0.1)
        # SIN termico_hotspot_class.tif ni los índices clasificados: no deben
        # aparecer aunque estén en el mismo loop de generate_tiles.py.
        correr()
        b = _bounds()
        assert "severidad" in b["capas_disponibles"]
        assert "hotspot_termico" not in b["capas_disponibles"]
        assert "ndvi_class" not in b["capas_disponibles"]

    @pytest.mark.parametrize("presentes,esperado", [
        (["severidad_class.tif"], {"severidad"}),
        (["severidad_class.tif", "termico_hotspot_class.tif"], {"severidad", "hotspot_termico"}),
        (["indices/ndvi_class.tif"], {"ndvi_class"}),
    ])
    def test_combinaciones_de_insumos(self, correr, presentes, esperado):
        _dsm()
        os.makedirs("outputs/indices", exist_ok=True)
        for rel in presentes:
            _raster(f"outputs/{rel}", 0.1)
        correr()
        todas = {"severidad", "hotspot_termico", "ndvi_class", "gndvi_class", "ndre_class", "msavi2_class"}
        assert set(_bounds()["capas_disponibles"]) & todas == esperado

    def test_rgb_thermal_hillshade_tambien_se_reportan(self, correr):
        """Estos tres no están gateados client-side (ver app.js), pero se
        registran igual en bounds.json por consistencia con el resto."""
        os.makedirs("outputs", exist_ok=True)
        # to_8bit(bands=3) espera un ráster de 3+ bandas (RGB u RGBA real) —
        # a diferencia de severidad/hotspot/índices, que son Byte de 1 banda.
        s = osr.SpatialReference(); s.ImportFromEPSG(32618)
        ds = gdal.GetDriverByName("GTiff").Create("outputs/rgb_orthomosaic.tif", 64, 64, 4, gdal.GDT_Byte)
        ds.SetGeoTransform((466000.0, 0.08, 0.0, 708900.0, 0.0, -0.08))
        ds.SetProjection(s.ExportToWkt())
        for b in range(4):
            ds.GetRasterBand(b + 1).WriteArray(np.full((64, 64), 200, np.uint8))
        ds = None
        correr()
        assert "rgb" in _bounds()["capas_disponibles"]
