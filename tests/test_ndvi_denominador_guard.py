"""compute_vegetation_indices.py calcula NDVI = (nir-red)/(nir+red) —
matemáticamente acotado a [-1,1] con reflectancias no negativas, PERO un
denominador apenas distinto de cero (ruido de calibración en píxeles
oscuros/sombra) dispara valores absurdos. Por eso el módulo exige
np.abs(a+b) > 1e-4 antes de dividir (documentado ahí: "sin este piso, NDVI
salía hasta ±14000 en <0.01% de los píxeles") — sin ese piso, un píxel de
puro ruido numérico podía colarse con un NDVI de cientos/miles.

Nota histórica: esta fórmula estaba DUPLICADA (sin el piso) en
detect_area_afectada.py, que confirmó en vivo NDVI entre -166 y 486 en una
misión real. Ese módulo (y detect_polygon()/NDVI_CEILING, que consumían ese
NDVI sin piso) fue eliminado por completo junto con la funcionalidad de área
afectada — compute_vegetation_indices.py es ahora la ÚNICA fuente de NDVI,
y siempre tuvo el piso, así que el bug de raíz ya no puede repetirse por
duplicación de fórmula.
"""
import os
import sys

import numpy as np
import pytest
from osgeo import gdal, osr

gdal.UseExceptions()

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

GT = (466000.0, 0.1, 0.0, 708900.0, 0.0, -0.1)
EPSG = 32618


def _ms_raster(path, red, green, nir, rededge, alpha=None):
    h, w = red.shape
    n_bands = 5 if alpha is not None else 4
    ds = gdal.GetDriverByName("GTiff").Create(path, w, h, n_bands, gdal.GDT_Float32)
    ds.SetGeoTransform(GT)
    s = osr.SpatialReference(); s.ImportFromEPSG(EPSG)
    ds.SetProjection(s.ExportToWkt())
    for i, (desc, arr) in enumerate([("Red", red), ("Green", green), ("NIR", nir), ("RedEdge", rededge)], start=1):
        b = ds.GetRasterBand(i)
        b.WriteArray(arr.astype(np.float32))
        b.SetDescription(desc)
    if alpha is not None:
        ab = ds.GetRasterBand(5)
        ab.WriteArray(alpha.astype(np.uint8))
        ab.SetColorInterpretation(gdal.GCI_AlphaBand)
    ds = None


@pytest.fixture
def mision(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("outputs")
    import compute_vegetation_indices as mod
    return mod


class TestPisoDeNdvi:
    def test_pixel_con_denominador_casi_cero_no_produce_ndvi_absurdo(self, mision):
        h, w = 6, 6
        red = np.full((h, w), 0.05, np.float32)
        nir = np.full((h, w), 0.12, np.float32)
        # Un píxel de "ruido puro": nir+red ~1e-6, con nir-red no nulo —
        # antes del fix esto disparaba NDVI de cientos/miles.
        red[3, 3] = 1e-7
        nir[3, 3] = -1e-7
        rededge = np.full((h, w), 0.08, np.float32)
        green = np.full((h, w), 0.06, np.float32)
        alpha = np.full((h, w), 255, np.uint8)

        _ms_raster("outputs/multispectral_orthomosaic.tif", red, green, nir, rededge, alpha)

        mision.main()
        ds = gdal.Open("outputs/indices/ndvi.tif")
        ndvi = ds.GetRasterBand(1).ReadAsArray()
        ds = None

        valid = np.isfinite(ndvi)
        assert np.all(np.abs(ndvi[valid]) <= 1.0 + 1e-6), \
            f"NDVI fuera de [-1,1] en píxeles válidos: {ndvi[valid]}"
        assert not valid[3, 3], "el píxel con denominador casi-cero debería quedar como NaN"

    def test_pixeles_normales_no_se_ven_afectados(self, mision):
        h, w = 4, 4
        red = np.full((h, w), 0.05, np.float32)
        nir = np.full((h, w), 0.15, np.float32)
        rededge = np.full((h, w), 0.08, np.float32)
        green = np.full((h, w), 0.06, np.float32)
        alpha = np.full((h, w), 255, np.uint8)

        _ms_raster("outputs/multispectral_orthomosaic.tif", red, green, nir, rededge, alpha)

        mision.main()
        ds = gdal.Open("outputs/indices/ndvi.tif")
        ndvi = ds.GetRasterBand(1).ReadAsArray()
        ds = None

        esperado = (0.15 - 0.05) / (0.15 + 0.05)
        assert np.isfinite(ndvi).all()
        assert np.allclose(ndvi, esperado)
