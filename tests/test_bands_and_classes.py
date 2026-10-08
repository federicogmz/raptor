"""Resolución de bandas multiespectrales, índices y clasificación.

`_find_band` es la corrección del bug P0 #2: resolver las bandas por NOMBRE y
no por posición. El test que más importa es que "RedEdge" no se confunda con
"Red" — esa confusión invalida el NDVI, del que dependen todos los índices de
vegetación.

classify()/write_class_tif() vivían en compute_severity_classes.py (ya
eliminado junto con la funcionalidad de área afectada/severidad); ahora son
utilidades compartidas en scripts/raster_classify.py, usadas por
compute_thermal_hotspot.py y classify_vegetation_indices.py.
"""
import numpy as np
import pytest
from osgeo import gdal

from raster_classify import classify, write_class_tif
from compute_vegetation_indices import _find_band, _msavi2

gdal.UseExceptions()


def _ds_con_bandas(tmp_path, nombres):
    path = str(tmp_path / "ms.tif")
    ds = gdal.GetDriverByName("GTiff").Create(path, 4, 4, len(nombres), gdal.GDT_Float32)
    for i, n in enumerate(nombres):
        ds.GetRasterBand(i + 1).SetDescription(n)
    ds = None
    return gdal.Open(path)


class TestFindBand:
    @pytest.mark.parametrize("orden", [
        ["Red", "Green", "NIR", "RedEdge"],
        ["NIR", "RedEdge", "Red", "Green"],
        ["Green", "Red", "RedEdge", "NIR"],
    ])
    def test_encuentra_cada_banda_en_cualquier_orden(self, tmp_path, orden):
        ds = _ds_con_bandas(tmp_path, orden)
        for logico, nombre in [("nir", "NIR"), ("red", "Red"),
                               ("green", "Green"), ("rededge", "RedEdge")]:
            idx = _find_band(ds, logico)
            assert idx is not None, f"no encontró {logico} en {orden}"
            assert ds.GetRasterBand(idx).GetDescription() == nombre, \
                f"{logico} resolvió a {ds.GetRasterBand(idx).GetDescription()} en {orden}"

    def test_rededge_no_se_confunde_con_red(self, tmp_path):
        """El fallo que invalidaría el NDVI: si 'RedEdge' matchea el alias
        'red', el índice se calcula con la banda equivocada y nada falla."""
        ds = _ds_con_bandas(tmp_path, ["RedEdge", "Red", "NIR", "Green"])
        assert ds.GetRasterBand(_find_band(ds, "red")).GetDescription() == "Red"
        assert ds.GetRasterBand(_find_band(ds, "rededge")).GetDescription() == "RedEdge"

    def test_acepta_prefijos_del_fabricante(self, tmp_path):
        ds = _ds_con_bandas(tmp_path, ["Band_Red", "Band_Green", "Band_NIR", "Band_RedEdge"])
        assert _find_band(ds, "red") == 1
        assert _find_band(ds, "nir") == 3

    def test_devuelve_none_si_falta(self, tmp_path):
        ds = _ds_con_bandas(tmp_path, ["Red", "Green"])
        assert _find_band(ds, "nir") is None

    def test_es_insensible_a_mayusculas_y_espacios(self, tmp_path):
        ds = _ds_con_bandas(tmp_path, ["  red  ", "GREEN", "nir", "Red Edge"])
        assert _find_band(ds, "red") == 1
        assert _find_band(ds, "green") == 2
        assert _find_band(ds, "rededge") == 4


class TestMsavi2:
    def test_valor_analitico_conocido(self):
        # nir=0.5, red=0.1 -> (2*0.5+1 - sqrt((2)^2 - 8*0.4))/2 = (2 - sqrt(0.8))/2
        got = _msavi2(np.array([0.5]), np.array([0.1]))[0]
        esperado = (2.0 - np.sqrt(4.0 - 3.2)) / 2
        assert abs(got - esperado) < 1e-9

    def test_el_guard_evita_nan_con_radicando_negativo(self):
        """np.maximum(...,0) adentro de la raíz: sin él, un nir bajo con red
        alto produce NaN silencioso en vez de un valor acotado."""
        nir = np.array([0.05, 0.0, 0.1])
        red = np.array([0.9, 1.0, 0.8])
        out = _msavi2(nir, red)
        assert np.isfinite(out).all(), f"MSAVI2 no puede devolver NaN acá: {out}"

    def test_monotono_en_nir(self):
        red = np.full(5, 0.2)
        nir = np.array([0.2, 0.3, 0.4, 0.5, 0.6])
        out = _msavi2(nir, red)
        assert np.all(np.diff(out) > 0), "más NIR con red fijo debe subir el índice"


class TestClassify:
    def test_n_cortes_producen_n_mas_1_clases(self):
        vals = np.array([0.0, 1.5, 2.5, 3.5, 9.0])
        valid = np.ones(5, bool)
        out = classify(vals, [1.0, 2.0, 3.0], valid)
        assert out.tolist() == [1, 2, 3, 4, 4]

    def test_el_borde_cae_en_la_clase_superior(self):
        """Los cortes son [breaks[i], breaks[i+1]) — el valor exacto del corte
        pertenece a la clase de ARRIBA."""
        vals = np.array([1.0, 2.0, 3.0])
        out = classify(vals, [1.0, 2.0, 3.0], np.ones(3, bool))
        assert out.tolist() == [2, 3, 4]

    def test_lo_invalido_queda_en_cero(self):
        vals = np.array([0.5, 5.0, 5.0])
        valid = np.array([True, False, True])
        out = classify(vals, [1.0, 2.0], valid)
        assert out[1] == 0, "0 es el nodata de las clases"
        assert out.tolist() == [1, 0, 3]

    def test_los_nan_no_se_clasifican_si_estan_marcados_invalidos(self):
        vals = np.array([np.nan, 1.5])
        out = classify(vals, [1.0], np.array([False, True]))
        assert out.tolist() == [0, 2]

    def test_devuelve_uint8(self):
        out = classify(np.array([1.0]), [0.5], np.ones(1, bool))
        assert out.dtype == np.uint8, "se escribe como GDT_Byte con nodata 0"


class TestSieveMMU:
    """write_class_tif() funde con gdal.SieveFilter los blobs más chicos que
    la MMU (MMU_M2, ~1 m²) con el polígono vecino más grande — reportado en
    vivo: focos térmicos de 1-2 px (ruido de reconstrucción, no un foco real)
    disparaban alertas. El umbral en píxeles sale de la resolución REAL de
    CADA ráster, no de un número fijo — por eso el fixture usa un GT con
    tamaño de píxel explícito en vez de uno arbitrario."""

    GT_10CM = (466000.0, 0.1, 0.0, 708900.0, 0.0, -0.1)  # 0.01 m²/px

    def _leer(self, path):
        ds = gdal.Open(path)
        arr = ds.GetRasterBand(1).ReadAsArray()
        ds = None
        return arr

    def test_blob_menor_a_la_mmu_se_funde_con_el_vecino(self, tmp_path):
        # Canvas 20×20 a 10cm/px = 0.01 m²/px → MMU 1 m² = 100 px. Clase 3
        # (caliente) llena todo, con un speck de 2×2=4px (clase 4, foco
        # activo) en el centro — muy por debajo del umbral.
        arr = np.full((20, 20), 3, dtype=np.uint8)
        arr[9:11, 9:11] = 4
        path = str(tmp_path / "hotspot.tif")
        write_class_tif(path, arr, self.GT_10CM, "")
        out = self._leer(path)
        assert not (out == 4).any(), "el speck de 4px (< 100px de MMU) tiene que fundirse con la clase 3 que lo rodea"
        assert (out == 3).sum() == arr.size, "fundido, no borrado: la cobertura total no cambia"

    def test_blob_mayor_a_la_mmu_sobrevive(self, tmp_path):
        # Mismo canvas, pero el bloque de clase 4 es 12×12=144px (> 100px) —
        # un foco real a esta resolución, no debe tocarse.
        arr = np.full((20, 20), 3, dtype=np.uint8)
        arr[4:16, 4:16] = 4
        path = str(tmp_path / "hotspot.tif")
        write_class_tif(path, arr, self.GT_10CM, "")
        out = self._leer(path)
        assert (out == 4).sum() == 144, "un blob por encima de la MMU no se filtra"

    def test_a_resolucion_mas_gruesa_hacen_falta_menos_pixeles(self, tmp_path):
        # Mismo blob de 2×2 que en el primer test, pero a 1m/px (100x más
        # área por píxel) — 4px ya alcanzan la MMU de 1 m² y sobreviven. La
        # MMU es un área real, no una cuenta fija de píxeles.
        gt_1m = (466000.0, 1.0, 0.0, 708900.0, 0.0, -1.0)
        arr = np.full((20, 20), 3, dtype=np.uint8)
        arr[9:11, 9:11] = 4
        path = str(tmp_path / "hotspot.tif")
        write_class_tif(path, arr, gt_1m, "")
        out = self._leer(path)
        assert (out == 4).sum() == 4, "a 1 m²/px, un blob de 4px ya cubre la MMU de 1 m² y no se filtra"
