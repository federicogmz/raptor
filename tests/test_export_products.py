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

pytest.importorskip("osgeo")
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
    _raster(
        "outputs/dsm.tif", rng.uniform(1900, 2100, (n, n)).astype(np.float32), gdal.GDT_Float32, nodata=float("nan")
    )
    _raster("outputs/termico_hotspot_class.tif", rng.integers(0, 5, (n, n)).astype(np.uint8), gdal.GDT_Byte, nodata=0)
    _raster(
        "outputs/rgb_orthomosaic.tif",
        np.concatenate([rng.integers(0, 255, (3, n, n), dtype=np.uint8), np.full((1, n, n), 255, np.uint8)]),
        gdal.GDT_Byte,
        alpha=True,
    )

    wgs = osr.SpatialReference()
    wgs.ImportFromEPSG(4326)
    wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ds = ogr.GetDriverByName("GeoJSON").CreateDataSource("outputs/flight_path.geojson")
    lyr = ds.CreateLayer("flight_path", wgs, ogr.wkbLineString)
    lyr.CreateField(ogr.FieldDefn("velocidad_kmh", ogr.OFTReal))
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetGeometry(ogr.CreateGeometryFromWkt("LINESTRING(-75.5 6.4,-75.499 6.4,-75.499 6.401)"))
    f.SetField("velocidad_kmh", 12345.0)
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
        assert _exportar(destino, monkeypatch, EXPORT_PRODUCTS="dsm", EXPORT_EPSG=str(MAGNA)) == 0
        ds = gdal.Open(f"{destino}/dsm.tif")
        gt = ds.GetGeoTransform()
        ds = None
        assert abs(gt[0] - ORIGEN_9377[0]) < 500 and abs(gt[3] - ORIGEN_9377[1]) < 500, (
            f"el ráster quedó en ({gt[0]:.0f}, {gt[3]:.0f}), no en {ORIGEN_9377}"
        )
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
        ds = gdal.Open(f"{destino}/termico_hotspot_class.tif")
        vals = set(np.unique(ds.GetRasterBand(1).ReadAsArray()).tolist())
        ds = None
        assert vals <= {0, 1, 2, 3, 4}, f"aparecieron clases inventadas: {sorted(vals)}"
        assert vals >= {1, 2, 3, 4}, f"se perdieron clases: {sorted(vals)}"


class TestTermico:
    def test_reproyectar_no_altera_las_temperaturas(self, mision, monkeypatch):
        """El térmico es una medición por píxel y los focos se definen por
        temperatura absoluta. Con remuestreo cúbico la exportación a
        EPSG:9377 aplanaba los picos y su sobreoscilación inventaba
        temperaturas que ningún píxel midió (-11.5 °C en Envigado)."""
        _, destino = mision
        rng = np.random.default_rng(7)
        n = 400
        temp = rng.integers(20, 31, (n, n)).astype(np.float32)
        temp[200:205, 200:205] = 121.4
        _raster("outputs/thermal_orthomosaic.tif", temp, gdal.GDT_Float32, nodata=float("nan"))
        _exportar(destino, monkeypatch, EXPORT_PRODUCTS="thermal", EXPORT_EPSG=str(MAGNA))
        ds = gdal.Open(f"{destino}/thermal_orthomosaic.tif")
        out = ds.GetRasterBand(1).ReadAsArray()
        ds = None
        vals = out[np.isfinite(out)]
        assert np.isclose(vals.max(), 121.4), f"el pico quedó en {vals.max():.1f} °C"
        assert vals.min() >= 20.0, f"apareció una temperatura inventada: {vals.min():.1f} °C"
        medidas = np.unique(temp)
        assert np.isin(np.unique(vals), medidas).all(), (
            "la exportación produjo temperaturas que no estaban en el original"
        )

    @pytest.mark.parametrize(
        "archivo,discreto,esperado",
        [
            ("outputs/thermal_orthomosaic.tif", False, "near"),
            ("outputs/termico_hotspot_class.tif", True, "near"),
            ("outputs/dsm.tif", False, "bilinear"),
            ("outputs/indices/ndvi.tif", False, "bilinear"),
            ("outputs/multispectral_orthomosaic.tif", False, "bilinear"),
            ("outputs/rgb_orthomosaic.tif", False, "cubic"),
        ],
    )
    def test_remuestreo_por_tipo_de_raster(self, archivo, discreto, esperado):
        assert export_products._resample_alg(archivo, discreto) == esperado


class TestFormatos:
    @pytest.mark.parametrize("fmt,ext", [("geojson", ".geojson"), ("gpkg", ".gpkg"), ("shp", ".shp"), ("kml", ".kml")])
    def test_formatos_vectoriales(self, mision, monkeypatch, fmt, ext):
        _, destino = mision
        assert (
            _exportar(
                destino, monkeypatch, EXPORT_PRODUCTS="flight_path", EXPORT_VECTOR_FORMAT=fmt, EXPORT_EPSG=str(MAGNA)
            )
            == 0
        )
        salida = f"{destino}/flight_path{ext}"
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
        _exportar(destino, monkeypatch, EXPORT_PRODUCTS="dsm", EXPORT_RASTER_FORMAT="gtiff", EXPORT_EPSG=str(MAGNA))
        ds = gdal.Open(f"{destino}/dsm.tif")
        gt = ds.GetGeoTransform()
        ds = None
        assert abs(gt[0] - ORIGEN_9377[0]) < 500

    def test_los_atributos_sobreviven(self, mision, monkeypatch):
        _, destino = mision
        _exportar(
            destino, monkeypatch, EXPORT_PRODUCTS="flight_path", EXPORT_VECTOR_FORMAT="gpkg", EXPORT_EPSG=str(MAGNA)
        )
        ds = ogr.Open(f"{destino}/flight_path.gpkg")
        feat = ds.GetLayer().GetNextFeature()
        val = feat.GetField("velocidad_kmh")
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
        assert _exportar(destino, monkeypatch, EXPORT_PRODUCTS="dsm", EXPORT_EPSG="999999") == 1
        assert "inválido" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "clave,valor",
        [
            ("EXPORT_PRODUCTS", "rgb,inventado"),
            ("EXPORT_RASTER_FORMAT", "jpeg2000"),
            ("EXPORT_VECTOR_FORMAT", "dxf"),
        ],
    )
    def test_configuracion_invalida_falla(self, mision, monkeypatch, clave, valor):
        _, destino = mision
        assert _exportar(destino, monkeypatch, **{clave: valor}) == 1

    def test_escribe_el_manifiesto(self, mision, monkeypatch):
        _, destino = mision
        _exportar(destino, monkeypatch, EXPORT_PRODUCTS="dsm,flight_path", EXPORT_EPSG=str(MAGNA))
        with open(f"{destino}/export_manifest.json") as f:
            m = json.load(f)
        assert m["crs"] == f"EPSG:{MAGNA}"
        assert len(m["archivos"]) == 2
        assert {a["producto"] for a in m["archivos"]} == {"dsm", "flight_path"}


class TestVerticalDatum:
    def test_export_dsm_egm96_desplaza_altura_y_preserva_termica(self, mision, monkeypatch, tmp_path):
        import numpy as np
        from osgeo import gdal, osr

        import export_products

        _src, _ = mision
        dsm_src = export_products.CATALOG["dsm"][2][0]
        src_dir = os.path.dirname(dsm_src)
        th_src = os.path.join(src_dir, "thermal_orthomosaic.tif")

        # Crea una banda térmica con la misma grilla/CRS del DSM de prueba para verificar que NO se modifica en Z.
        dsm_ds = gdal.Open(dsm_src)
        w, h = dsm_ds.RasterXSize, dsm_ds.RasterYSize
        gt = dsm_ds.GetGeoTransform()
        src_srs = dsm_ds.GetSpatialRef()
        dsm_ds = None

        drv = gdal.GetDriverByName("GTiff")
        th_ds = drv.Create(th_src, w, h, 1, gdal.GDT_Float32)
        th_ds.SetGeoTransform(gt)
        th_ds.SetSpatialRef(src_srs)
        th_band = th_ds.GetRasterBand(1)
        th_band.SetNoDataValue(-9999.0)
        th_arr = np.full((h, w), 30.0, dtype=np.float32)
        th_arr[0, 0] = -9999.0
        th_band.WriteArray(th_arr)
        th_ds = None

        if not os.path.isfile("/usr/share/proj/egm96_15.gtx"):
            grid_path = str(tmp_path / "egm96_synth.tif")
            gds = drv.Create(grid_path, 4, 4, 1, gdal.GDT_Float32)
            gds.SetGeoTransform((-180.0, 90.0, 0.0, 90.0, 0.0, -45.0))
            s4326 = osr.SpatialReference()
            s4326.ImportFromEPSG(4326)
            gds.SetSpatialRef(s4326)
            gds.GetRasterBand(1).WriteArray(np.full((4, 4), 21.94, dtype=np.float32))
            gds = None
            monkeypatch.setenv("RAPTOR_EGM96_GRID", grid_path)
            grid_real = grid_path
        else:
            grid_real = "/usr/share/proj/egm96_15.gtx"

        dest_ellip = tmp_path / "dest_ellip"
        dest_egm96 = tmp_path / "dest_egm96"
        assert (
            _exportar(
                str(dest_ellip),
                monkeypatch,
                EXPORT_PRODUCTS="dsm,thermal",
                EXPORT_EPSG=str(MAGNA),
                EXPORT_VERTICAL_DATUM="ellipsoidal",
            )
            == 0
        )
        assert (
            _exportar(
                str(dest_egm96),
                monkeypatch,
                EXPORT_PRODUCTS="dsm,thermal",
                EXPORT_EPSG=str(MAGNA),
                EXPORT_VERTICAL_DATUM="egm96",
            )
            == 0
        )

        e_ds = gdal.Open(str(dest_ellip / "dsm.tif"))
        o_ds = gdal.Open(str(dest_egm96 / "dsm.tif"))
        e_band, o_band = e_ds.GetRasterBand(1), o_ds.GetRasterBand(1)
        e_nd, o_nd = e_band.GetNoDataValue(), o_band.GetNoDataValue()
        e_arr, o_arr = e_band.ReadAsArray(), o_band.ReadAsArray()
        o_srs = o_ds.GetSpatialRef()
        v_meta = o_ds.GetMetadataItem("VERTICAL_DATUM")
        gt_o = o_ds.GetGeoTransform()
        cx = gt_o[0] + gt_o[1] * o_ds.RasterXSize / 2.0
        cy = gt_o[3] + gt_o[5] * o_ds.RasterYSize / 2.0
        o_srs_epsg = o_srs.Clone()
        e_ds = o_ds = None

        import math

        n_esperado = export_products._sample_geoid_at(grid_real, cx, cy, export_products._srs_from_epsg(MAGNA))
        valid = np.isfinite(e_arr)
        if e_nd is not None and math.isfinite(e_nd):
            valid &= e_arr != np.float32(e_nd)
        assert valid.any()
        assert (~np.isfinite(o_arr[~valid]) | (o_arr[~valid] == o_nd)).all()
        diff = float(np.median(o_arr[valid] - e_arr[valid]))
        assert diff == pytest.approx(-n_esperado, abs=0.05)
        if os.path.isfile("/usr/share/proj/egm96_15.gtx"):
            assert -23.0 < diff < -20.5

        assert v_meta == "egm96"
        assert o_srs_epsg.GetAuthorityCode("PROJCS") == str(MAGNA)
        assert o_srs_epsg.GetAuthorityCode("VERT_CS") == "5773"

        t_ds = gdal.Open(str(dest_egm96 / "thermal_orthomosaic.tif"))
        t_arr = t_ds.GetRasterBand(1).ReadAsArray()
        t_nd = t_ds.GetRasterBand(1).GetNoDataValue()
        assert t_ds.GetMetadataItem("VERTICAL_DATUM") is None
        assert t_ds.GetSpatialRef().GetAuthorityCode(None) == str(MAGNA)
        t_ds = None
        t_valid = np.isfinite(t_arr) & (t_arr != np.float32(t_nd))
        assert float(np.median(t_arr[t_valid])) == pytest.approx(30.0)

        with open(dest_egm96 / "export_manifest.json", encoding="utf-8") as f:
            man = json.load(f)
        assert man["vertical_datum"] == "egm96"
        por_prod = {a["producto"]: a for a in man["archivos"]}
        assert por_prod["dsm"]["vertical_datum"] == "egm96"
        assert "vertical_datum" not in por_prod["thermal"]

    def test_preflight_falla_sin_parciales_si_falta_geoide_o_pdal(self, mision, monkeypatch, tmp_path):
        import export_products

        _src, _ = mision
        src_dir = os.path.dirname(export_products.CATALOG["dsm"][2][0])

        dest_geoid = tmp_path / "dest_sin_geoide"
        assert (
            _exportar(
                str(dest_geoid),
                monkeypatch,
                EXPORT_PRODUCTS="dsm",
                EXPORT_VERTICAL_DATUM="geoid:/no/existe.gtx",
            )
            == 1
        )
        assert not dest_geoid.exists()

        nube_src = os.path.join(src_dir, "point_cloud_rgb.copc.laz")
        with open(nube_src, "wb") as f:
            f.write(b"fake-copc")
        monkeypatch.setattr(export_products, "_find_pdal", lambda: None)

        dest_pdal = tmp_path / "dest_sin_pdal"
        assert (
            _exportar(
                str(dest_pdal),
                monkeypatch,
                EXPORT_PRODUCTS="dsm,pointclouds",
                EXPORT_VERTICAL_DATUM="offset:-20",
            )
            == 1
        )
        assert not dest_pdal.exists()

    def test_geoid_parcial_falla_sin_dejar_archivos(self, mision, monkeypatch, capsys, tmp_path):
        import numpy as np
        from osgeo import gdal

        import export_products

        _src, _ = mision
        dsm_src = export_products.CATALOG["dsm"][2][0]
        dsm_ds = gdal.Open(dsm_src)
        gt = dsm_ds.GetGeoTransform()
        w, h = dsm_ds.RasterXSize, dsm_ds.RasterYSize
        srs = dsm_ds.GetSpatialRef()
        dsm_ds = None

        # Grilla SIN NoData explícito que cubre solo 40 % del ancho del DSM.
        grid_parcial = str(tmp_path / "geoid_parcial.tif")
        pw = max(2, int(w * 0.4))
        ph = max(2, h)
        gds = gdal.GetDriverByName("GTiff").Create(grid_parcial, pw, ph, 1, gdal.GDT_Float32)
        gds.SetGeoTransform(gt)
        gds.SetSpatialRef(srs)
        gds.GetRasterBand(1).WriteArray(np.full((ph, pw), 20.0, dtype=np.float32))
        gds = None

        dest_parcial = tmp_path / "dest_parcial"
        assert (
            _exportar(
                str(dest_parcial),
                monkeypatch,
                EXPORT_PRODUCTS="dsm",
                EXPORT_EPSG="source",
                EXPORT_VERTICAL_DATUM=f"geoid:{grid_parcial}",
            )
            == 1
        )
        assert "no cubre el área completa" in capsys.readouterr().out
        assert not dest_parcial.exists() or os.listdir(dest_parcial) == []

    def test_webapp_validate_export_propaga_offset_y_geoid_a_export_products(self, mision, monkeypatch, tmp_path):
        import importlib
        import math
        import numpy as np
        from osgeo import gdal

        import export_products
        from core import scan

        monkeypatch.setenv("RAPTOR_RUNS_ROOT", str(tmp_path / "runs"))
        importlib.reload(scan)
        from webapp import main as webapp_main

        importlib.reload(webapp_main)

        _src, _ = mision
        dsm_src = export_products.CATALOG["dsm"][2][0]
        dsm_ds = gdal.Open(dsm_src)
        gt = dsm_ds.GetGeoTransform()
        w, h = dsm_ds.RasterXSize, dsm_ds.RasterYSize
        srs = dsm_ds.GetSpatialRef()
        dsm_band = dsm_ds.GetRasterBand(1)
        dsm_nd = dsm_band.GetNoDataValue()
        dsm_arr = dsm_band.ReadAsArray()
        dsm_ds = None

        mount_dir = tmp_path / "mount_export"
        mount_dir.mkdir()
        monkeypatch.setattr(webapp_main, "EXPORT_MOUNT", str(mount_dir))
        monkeypatch.setattr(webapp_main, "EXPORT_HOST_DIR", "/host/entregas")

        # Grilla geoidal válida cubriendo el DSM completo + margen.
        geocol = mount_dir / "geocol.tif"
        gds = gdal.GetDriverByName("GTiff").Create(str(geocol), w + 4, h + 4, 1, gdal.GDT_Float32)
        gds.SetGeoTransform((gt[0] - 2 * abs(gt[1]), gt[1], 0.0, gt[3] + 2 * abs(gt[5]), 0.0, gt[5]))
        gds.SetSpatialRef(srs)
        gds.GetRasterBand(1).WriteArray(np.full((h + 4, w + 4), 18.5, dtype=np.float32))
        gds = None

        mission_dir = tmp_path / "mission"
        mission_dir.mkdir()

        # 1) offset:-20 vía _validate_export -> export_products.main()
        err_off, env_off = webapp_main._validate_export(
            mission_dir,
            "/host/entregas/out_offset",
            ["dsm"],
            "cog",
            "geojson",
            "source",
            crear=True,
            vertical_datum="offset:-20",
        )
        assert err_off == []
        assert (
            _exportar(
                env_off["EXPORT_DIR"],
                monkeypatch,
                EXPORT_PRODUCTS=env_off["EXPORT_PRODUCTS"],
                EXPORT_RASTER_FORMAT=env_off["EXPORT_RASTER_FORMAT"],
                EXPORT_VECTOR_FORMAT=env_off["EXPORT_VECTOR_FORMAT"],
                EXPORT_EPSG=env_off["EXPORT_EPSG"],
                EXPORT_VERTICAL_DATUM=env_off["EXPORT_VERTICAL_DATUM"],
            )
            == 0
        )

        # 2) geoid:/host/entregas/geocol.tif vía _validate_export -> export_products.main()
        err_geo, env_geo = webapp_main._validate_export(
            mission_dir,
            "/host/entregas/out_geoid",
            ["dsm"],
            "cog",
            "geojson",
            "source",
            crear=True,
            vertical_datum="geoid:/host/entregas/geocol.tif",
        )
        assert err_geo == []
        assert (
            _exportar(
                env_geo["EXPORT_DIR"],
                monkeypatch,
                EXPORT_PRODUCTS=env_geo["EXPORT_PRODUCTS"],
                EXPORT_RASTER_FORMAT=env_geo["EXPORT_RASTER_FORMAT"],
                EXPORT_VECTOR_FORMAT=env_geo["EXPORT_VECTOR_FORMAT"],
                EXPORT_EPSG=env_geo["EXPORT_EPSG"],
                EXPORT_VERTICAL_DATUM=env_geo["EXPORT_VERTICAL_DATUM"],
            )
            == 0
        )

        valid = np.isfinite(dsm_arr)
        if dsm_nd is not None and math.isfinite(dsm_nd):
            valid &= dsm_arr != np.float32(dsm_nd)

        off_ds = gdal.Open(str(mount_dir / "out_offset" / "dsm.tif"))
        off_arr = off_ds.GetRasterBand(1).ReadAsArray()
        off_ds = None
        assert float(np.median(off_arr[valid] - dsm_arr[valid])) == pytest.approx(-20.0, abs=0.01)

        geo_ds = gdal.Open(str(mount_dir / "out_geoid" / "dsm.tif"))
        geo_arr = geo_ds.GetRasterBand(1).ReadAsArray()
        geo_ds = None
        assert float(np.median(geo_arr[valid] - dsm_arr[valid])) == pytest.approx(-18.5, abs=0.01)

        man_off = json.loads((mount_dir / "out_offset" / "export_manifest.json").read_text(encoding="utf-8"))
        man_geo = json.loads((mount_dir / "out_geoid" / "export_manifest.json").read_text(encoding="utf-8"))
        assert man_off["vertical_datum"] == "offset:-20.000m"
        assert man_geo["vertical_datum"] == "geoid:geocol.tif"
