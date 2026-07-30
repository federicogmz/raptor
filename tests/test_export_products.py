"""Exportación de la entrega: formato, CRS y georreferenciación.

El test que más importa es `test_el_cog_conserva_la_georreferenciacion`.
gdal.Warp NO puede escribir directo a COG —ese driver solo implementa
CreateCopy()— y el binding de Python, a diferencia del ejecutable gdalwarp,
devuelve un GeoTIFF con el geotransform DESTRUIDO sin lanzar ningún error. El
archivo abre bien y está en cualquier parte del planeta menos donde
corresponde: exactamente el fallo que nadie nota hasta que el producto ya se
entregó.
"""
import json
import os

import numpy as np
import pytest
from osgeo import gdal, ogr, osr

import export_products

gdal.UseExceptions()

UTM18N, MAGNA = 32618, 9377
# Un punto real de Antioquia y su equivalente en EPSG:9377, para comprobar que
# la reproyección deja el ráster DONDE VA y no solo con la etiqueta correcta.
GT_UTM = (466000.0, 0.2, 0.0, 708900.0, 0.0, -0.2)
ORIGEN_9377 = (4744868.0, 2267229.0)


def _wkt(epsg):
    s = osr.SpatialReference()
    s.ImportFromEPSG(epsg)
    return s.ExportToWkt()


def _raster(path, arr, dtype, nodata=None, alpha=False):
    if arr.ndim == 2:
        arr = arr[np.newaxis, ...]
    n, h, w = arr.shape
    ds = gdal.GetDriverByName("GTiff").Create(path, w, h, n, dtype)
    ds.SetGeoTransform(GT_UTM)
    ds.SetProjection(_wkt(UTM18N))
    if alpha:
        ds.GetRasterBand(n).SetColorInterpretation(gdal.GCI_AlphaBand)
    for i in range(n):
        b = ds.GetRasterBand(i + 1)
        b.WriteArray(arr[i])
        if nodata is not None:
            b.SetNoDataValue(nodata)
    ds = None


@pytest.fixture
def mision(tmp_path, monkeypatch):
    """outputs/ sintético con un producto continuo, uno de clases y un vector."""
    monkeypatch.chdir(tmp_path)
    os.makedirs("outputs/indices")
    rng = np.random.default_rng(3)
    # 1200 px: por encima del tamaño de bloque del COG, para que genere
    # overviews reales en vez de saltearlos por ráster chico.
    n = 1200
    _raster("outputs/dsm.tif", rng.uniform(1900, 2100, (n, n)).astype(np.float32),
            gdal.GDT_Float32, nodata=float("nan"))
    _raster("outputs/severidad_class.tif",
            rng.integers(0, 5, (n, n)).astype(np.uint8), gdal.GDT_Byte, nodata=0)
    _raster("outputs/rgb_orthomosaic.tif",
            np.concatenate([rng.integers(0, 255, (3, n, n), dtype=np.uint8),
                            np.full((1, n, n), 255, np.uint8)]),
            gdal.GDT_Byte, alpha=True)

    wgs = osr.SpatialReference(); wgs.ImportFromEPSG(4326)
    wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ds = ogr.GetDriverByName("GeoJSON").CreateDataSource("outputs/area_afectada.geojson")
    lyr = ds.CreateLayer("area", wgs, ogr.wkbPolygon)
    lyr.CreateField(ogr.FieldDefn("area_m2", ogr.OFTReal))
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetGeometry(ogr.CreateGeometryFromWkt(
        "POLYGON((-75.5 6.4,-75.499 6.4,-75.499 6.401,-75.5 6.401,-75.5 6.4))"))
    f.SetField("area_m2", 12345.0)
    lyr.CreateFeature(f)
    ds = None

    destino = str(tmp_path / "entrega")
    for k in list(os.environ):
        if k.startswith("EXPORT_"):
            monkeypatch.delenv(k, raising=False)
    return tmp_path, destino


def _exportar(destino, monkeypatch, **env):
    monkeypatch.setenv("EXPORT_DIR", destino)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return export_products.main()


class TestGeorreferenciacion:
    def test_el_cog_conserva_la_georreferenciacion(self, mision, monkeypatch):
        """La regresión que motivó el camino de dos pasos (warp a GTiff
        temporal + Translate). Con gdal.Warp(format="COG") el geotransform
        salía en (0,0) con píxel 1.0, sin error de por medio."""
        _, destino = mision
        assert _exportar(destino, monkeypatch, EXPORT_PRODUCTS="dsm",
                         EXPORT_EPSG=str(MAGNA)) == 0
        ds = gdal.Open(f"{destino}/dsm.tif")
        gt = ds.GetGeoTransform()
        ds = None
        assert abs(gt[0] - ORIGEN_9377[0]) < 500 and abs(gt[3] - ORIGEN_9377[1]) < 500, \
            f"el ráster quedó en ({gt[0]:.0f}, {gt[3]:.0f}), no en {ORIGEN_9377}"
        assert abs(abs(gt[1]) - 0.2) < 0.05, f"tamaño de píxel corrupto: {gt[1]}"

    def test_reproyecta_al_epsg_pedido(self, mision, monkeypatch):
        _, destino = mision
        _exportar(destino, monkeypatch, EXPORT_PRODUCTS="dsm", EXPORT_EPSG=str(MAGNA))
        ds = gdal.Open(f"{destino}/dsm.tif")
        code = ds.GetSpatialRef().GetAuthorityCode(None)
        ds = None
        assert code == str(MAGNA)

    def test_source_no_reproyecta(self, mision, monkeypatch):
        _, destino = mision
        _exportar(destino, monkeypatch, EXPORT_PRODUCTS="dsm", EXPORT_EPSG="source")
        ds = gdal.Open(f"{destino}/dsm.tif")
        code = ds.GetSpatialRef().GetAuthorityCode(None)
        gt = ds.GetGeoTransform()
        ds = None
        assert code == str(UTM18N)
        assert abs(gt[0] - GT_UTM[0]) < 1

    def test_el_cog_trae_overviews(self, mision, monkeypatch):
        _, destino = mision
        _exportar(destino, monkeypatch, EXPORT_PRODUCTS="dsm", EXPORT_EPSG=str(MAGNA))
        ds = gdal.Open(f"{destino}/dsm.tif")
        n = ds.GetRasterBand(1).GetOverviewCount()
        ds = None
        assert n > 0, "un COG sin overviews no es cloud-optimized"


class TestClases:
    def test_las_clases_no_se_promedian_al_reproyectar(self, mision, monkeypatch):
        """Vecino más cercano, mismo criterio que generate_tiles.py: promediar
        clases inventa categorías intermedias que no existen. Se exige además
        que estén TODAS — un ráster vacío también cumpliría "sin intermedios"."""
        _, destino = mision
        _exportar(destino, monkeypatch, EXPORT_PRODUCTS="classes", EXPORT_EPSG=str(MAGNA))
        ds = gdal.Open(f"{destino}/severidad_class.tif")
        vals = set(np.unique(ds.GetRasterBand(1).ReadAsArray()).tolist())
        ds = None
        assert vals <= {0, 1, 2, 3, 4}, f"aparecieron clases inventadas: {sorted(vals)}"
        assert vals >= {1, 2, 3, 4}, f"se perdieron clases: {sorted(vals)}"


class TestFormatos:
    @pytest.mark.parametrize("fmt,ext", [("geojson", ".geojson"), ("gpkg", ".gpkg"),
                                         ("shp", ".shp"), ("kml", ".kml")])
    def test_formatos_vectoriales(self, mision, monkeypatch, fmt, ext):
        _, destino = mision
        assert _exportar(destino, monkeypatch, EXPORT_PRODUCTS="area",
                         EXPORT_VECTOR_FORMAT=fmt, EXPORT_EPSG=str(MAGNA)) == 0
        salida = f"{destino}/area_afectada{ext}"
        assert os.path.isfile(salida)
        ds = ogr.Open(salida)
        lyr = ds.GetLayer()
        assert lyr.GetFeatureCount() == 1
        code = lyr.GetSpatialRef().GetAuthorityCode(None)
        ds = None
        # KML es WGS84 por especificación, cualquier otra CRS lo rompe en silencio
        assert code == ("4326" if fmt == "kml" else str(MAGNA))

    def test_gtiff_tambien_sale_georreferenciado(self, mision, monkeypatch):
        _, destino = mision
        _exportar(destino, monkeypatch, EXPORT_PRODUCTS="dsm",
                  EXPORT_RASTER_FORMAT="gtiff", EXPORT_EPSG=str(MAGNA))
        ds = gdal.Open(f"{destino}/dsm.tif")
        gt = ds.GetGeoTransform()
        ds = None
        assert abs(gt[0] - ORIGEN_9377[0]) < 500

    def test_los_atributos_sobreviven(self, mision, monkeypatch):
        _, destino = mision
        _exportar(destino, monkeypatch, EXPORT_PRODUCTS="area",
                  EXPORT_VECTOR_FORMAT="gpkg", EXPORT_EPSG=str(MAGNA))
        ds = ogr.Open(f"{destino}/area_afectada.gpkg")
        feat = ds.GetLayer().GetNextFeature()
        val = feat.GetField("area_m2")
        ds = None
        assert abs(val - 12345.0) < 1e-6


class TestComportamiento:
    def test_sin_export_dir_es_un_noop(self, mision, monkeypatch, capsys):
        monkeypatch.delenv("EXPORT_DIR", raising=False)
        assert export_products.main() == 0
        assert "no se exporta nada" in capsys.readouterr().out

    def test_omite_lo_que_la_mision_no_generó(self, mision, monkeypatch, capsys):
        _, destino = mision
        assert _exportar(destino, monkeypatch, EXPORT_PRODUCTS="all") == 0
        assert not os.path.exists(f"{destino}/thermal_orthomosaic.tif")
        assert "no generado en esta misión" in capsys.readouterr().out

    def test_epsg_invalido_falla_con_mensaje(self, mision, monkeypatch, capsys):
        _, destino = mision
        assert _exportar(destino, monkeypatch, EXPORT_PRODUCTS="dsm",
                         EXPORT_EPSG="999999") == 1
        assert "inválido" in capsys.readouterr().out

    @pytest.mark.parametrize("clave,valor", [
        ("EXPORT_PRODUCTS", "rgb,inventado"),
        ("EXPORT_RASTER_FORMAT", "jpeg2000"),
        ("EXPORT_VECTOR_FORMAT", "dxf"),
    ])
    def test_configuracion_invalida_falla(self, mision, monkeypatch, clave, valor):
        _, destino = mision
        assert _exportar(destino, monkeypatch, **{clave: valor}) == 1

    def test_escribe_el_manifiesto(self, mision, monkeypatch):
        _, destino = mision
        _exportar(destino, monkeypatch, EXPORT_PRODUCTS="dsm,area", EXPORT_EPSG=str(MAGNA))
        with open(f"{destino}/export_manifest.json") as f:
            m = json.load(f)
        assert m["crs"] == f"EPSG:{MAGNA}"
        assert len(m["archivos"]) == 2
        assert {a["producto"] for a in m["archivos"]} == {"dsm", "area"}
