"""generate_tiles.py: caché de rangos de color y conversión a 8 bits por
bloques.

El entrypoint invoca este script ~6 veces por corrida (publish_partial, para
que el geovisor vaya publicando en vez de aparecer todo junto al final). Antes
cada pasada recalculaba los percentiles de TODAS las capas —térmico, 4 índices
y 4 bandas espectrales— releyendo cada ráster entero, para después informar
"ya al día, omitiendo". Con el ortomosaico multiespectral de una misión real
(11128×9294, 5 bandas) eso son ~12 GB leídos por pasada sin producir nada.

Dos defensas, que es lo que se verifica acá:
  · los percentiles salen de una submuestra decimada por GDAL (MAX_PX_MUESTRA),
    indistinguible del percentil exacto para lo que es una escala de leyenda;
  · el resultado se cachea contra el mtime del ráster fuente, igual que
    up_to_date() hace con los tiles.
"""
import json
import os
import sys

import numpy as np
import pytest
from osgeo import gdal, osr

gdal.UseExceptions()

REPO_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "scripts")
if REPO_SCRIPTS not in sys.path:
    sys.path.insert(0, REPO_SCRIPTS)


def _raster_float(path, w=64, h=64, bandas=2, valores=None, px_m=0.1):
    """GeoTIFF float32 con banda de dato + alpha, como los que produce ODM."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    s = osr.SpatialReference()
    s.ImportFromEPSG(32618)
    ds = gdal.GetDriverByName("GTiff").Create(path, w, h, bandas, gdal.GDT_Float32)
    ds.SetGeoTransform((466000.0, px_m, 0.0, 708900.0, 0.0, -px_m))
    ds.SetProjection(s.ExportToWkt())
    dato = valores if valores is not None else np.linspace(0, 1, w * h).reshape(h, w)
    ds.GetRasterBand(1).WriteArray(dato.astype(np.float32))
    for b in range(2, bandas + 1):
        ds.GetRasterBand(b).WriteArray(np.full((h, w), 255, np.float32))
    ds = None
    return path


@pytest.fixture
def modulo(tmp_path, monkeypatch):
    """generate_tiles.py corre su cuerpo al importarse, así que hay que
    preparar el cwd antes (mismo patrón que tests/test_generate_tiles.py)."""
    monkeypatch.chdir(tmp_path)
    _raster_float("outputs/dsm.tif")   # sin esto no se escribe bounds.json

    def _ejecutar():
        sys.modules.pop("generate_tiles", None)
        import generate_tiles as G
        return G
    return _ejecutar


class TestCacheDeRangos:
    def test_no_recalcula_si_el_raster_no_cambio(self, modulo, tmp_path):
        _raster_float("outputs/thermal_orthomosaic.tif",
                      valores=np.linspace(20, 60, 64 * 64).reshape(64, 64))
        G = modulo()
        primero = G.THERMAL_CLIP

        cache = json.loads((tmp_path / "geovisor/tiles/.ranges.json").read_text())
        assert cache["thermal"]["rango"] == pytest.approx(list(primero))

        # Segunda pasada sobre el MISMO ráster: el caché tiene que evitar el
        # cálculo entero, no solo devolver el mismo número por casualidad.
        # rango_cacheado() recibe la función de cálculo, así que se le pasa
        # una que grita si la llaman.
        veces = []

        def _no_deberia_correr():
            veces.append(1)
            return (0.0, 1.0)

        assert G.rango_cacheado("thermal", "outputs/thermal_orthomosaic.tif",
                                _no_deberia_correr) == pytest.approx(primero)
        assert veces == [], "recalculó el percentil con el caché al día"

    def test_recalcula_si_cambia_el_mtime(self, modulo, tmp_path):
        ruta = "outputs/thermal_orthomosaic.tif"
        _raster_float(ruta, valores=np.linspace(20, 30, 64 * 64).reshape(64, 64))
        primero = modulo().THERMAL_CLIP

        # Otro dato → otro rango. El mtime cambia, así que el caché no aplica.
        _raster_float(ruta, valores=np.linspace(100, 200, 64 * 64).reshape(64, 64))
        segundo = modulo().THERMAL_CLIP

        assert segundo[1] > primero[1] + 50, (primero, segundo)

    def test_force_tiles_ignora_el_cache(self, modulo, tmp_path, monkeypatch):
        ruta = "outputs/thermal_orthomosaic.tif"
        _raster_float(ruta, valores=np.linspace(20, 30, 64 * 64).reshape(64, 64))
        modulo()
        cache = tmp_path / "geovisor/tiles/.ranges.json"
        # Se ensucia el caché a mano: con FORCE_TILES=1 tiene que recalcular
        # igual y pisarlo.
        datos = json.loads(cache.read_text())
        datos["thermal"]["rango"] = [-999.0, 999.0]
        cache.write_text(json.dumps(datos))

        monkeypatch.setenv("FORCE_TILES", "1")
        assert modulo().THERMAL_CLIP[0] > -999.0

    def test_un_cache_corrupto_no_tumba_la_corrida(self, modulo, tmp_path):
        _raster_float("outputs/thermal_orthomosaic.tif")
        os.makedirs("geovisor/tiles", exist_ok=True)
        (tmp_path / "geovisor/tiles/.ranges.json").write_text("{ esto no es json")
        assert modulo().THERMAL_CLIP is not None


class TestSubmuestreo:
    def test_percentil_sobre_la_muestra_coincide_con_el_exacto(self, modulo):
        """La submuestra tiene que dar prácticamente el mismo rango que leer
        el ráster entero — si no, cambiaría la leyenda que ve el usuario."""
        G = modulo()
        rng = np.random.default_rng(0)
        datos = (rng.normal(30, 5, 400 * 400)).reshape(400, 400)
        _raster_float("outputs/thermal_orthomosaic.tif", w=400, h=400, valores=datos)

        G.MAX_PX_MUESTRA = 10_000          # fuerza el camino decimado
        muestra = G._muestra_valida("outputs/thermal_orthomosaic.tif", 1, alpha_idx=2)
        assert muestra.size < 400 * 400

        lo_m, hi_m = np.percentile(muestra, [1, 99])
        lo_e, hi_e = np.percentile(datos, [1, 99])
        assert lo_m == pytest.approx(lo_e, abs=1.0)
        assert hi_m == pytest.approx(hi_e, abs=1.0)

    def test_sin_decimar_si_el_raster_ya_entra(self, modulo):
        G = modulo()
        ds = gdal.Open(_raster_float("outputs/chico.tif", w=50, h=40))
        assert G._tamano_muestra(ds) == (50, 40)


class TestConversionPorBloques:
    def test_identico_a_convertir_de_una(self, modulo, tmp_path):
        """El pico de memoria baja, el resultado no cambia."""
        G = modulo()
        rng = np.random.default_rng(3)
        datos = rng.random((300, 120)).astype(np.float32)
        src = _raster_float("outputs/ms.tif", w=120, h=300, valores=datos)

        G.FILAS_POR_BLOQUE = 10_000        # una sola pasada
        G._banda_a_8bit(src, str(tmp_path / "entero.tif"), 1, (0.0, 1.0))
        G.FILAS_POR_BLOQUE = 32            # ~10 bloques
        G._banda_a_8bit(src, str(tmp_path / "bloques.tif"), 1, (0.0, 1.0))

        a = gdal.Open(str(tmp_path / "entero.tif")).ReadAsArray()
        b = gdal.Open(str(tmp_path / "bloques.tif")).ReadAsArray()
        assert np.array_equal(a, b)

    def test_alpha_cero_queda_en_nodata(self, modulo, tmp_path):
        """alpha=0 es la única señal confiable de 'fuera de cobertura': el
        relleno de ODM en la banda de dato no siempre es NaN (visto: 2**32)."""
        G = modulo()
        s = osr.SpatialReference(); s.ImportFromEPSG(32618)
        ruta = str(tmp_path / "conalpha.tif")
        ds = gdal.GetDriverByName("GTiff").Create(ruta, 8, 8, 2, gdal.GDT_Float32)
        ds.SetGeoTransform((466000.0, 0.1, 0.0, 708900.0, 0.0, -0.1))
        ds.SetProjection(s.ExportToWkt())
        ds.GetRasterBand(1).WriteArray(np.full((8, 8), 2.0 ** 32, np.float32))
        alpha = np.full((8, 8), 255, np.float32)
        alpha[:4] = 0
        ds.GetRasterBand(2).WriteArray(alpha)
        ds = None

        G.FILAS_POR_BLOQUE = 3   # el corte cae dentro de la zona inválida
        salida = str(tmp_path / "out.tif")
        G._banda_a_8bit(ruta, salida, 1, (0.0, 1.0))

        arr = gdal.Open(salida).ReadAsArray()
        assert (arr[:4] == 0).all(), "alpha=0 tenía que quedar en 0 (nodata)"
