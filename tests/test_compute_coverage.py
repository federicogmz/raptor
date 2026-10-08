"""scripts/compute_coverage.py — qué fracción del ÁREA VOLADA quedó cubierta.

La métrica que faltaba: run_summary mide la cobertura del RECUADRO del
ráster; acá la referencia es el convex hull de la ruta de vuelo, así una
reconstrucción que descartó fotos en silencio (planar en terreno con
relieve: terminaba "bien" con 23-45% de cobertura sin error visible) se
ve clara en outputs/coverage.json + alerta.
"""
import json
import os
import subprocess

import numpy as np
from osgeo import gdal, osr

gdal.UseExceptions()

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "compute_coverage.py")

LON_LAT_CORNERS = [(-75.001, 5.999), (-75.001, 6.001),
                   (-74.999, 6.001), (-74.999, 5.999)]


def _utm_bounds():
    wgs = osr.SpatialReference()
    wgs.ImportFromEPSG(4326)
    utm = osr.SpatialReference()
    utm.ImportFromEPSG(32618)
    wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    utm.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    t = osr.CoordinateTransformation(wgs, utm)
    xs, ys = [], []
    for lon, lat in LON_LAT_CORNERS:
        x, y, _ = t.TransformPoint(lon, lat)
        xs.append(x)
        ys.append(y)
    return min(xs), max(ys), max(xs) - min(xs), max(ys) - min(ys)


def _escribir_raster(path, arr, gt, proj):
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(str(path), arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform(gt)
    ds.SetProjection(proj)
    b = ds.GetRasterBand(1)
    b.WriteArray(arr)
    b.SetNoDataValue(0)
    ds = None


def _flight_path(path):
    """flight_path.geojson con los 4 puntos de esquina (capturas)."""
    feats = [{
        "type": "Feature",
        "properties": {"kind": "capture", "time": "2026-08-08T11:49:18"},
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
    } for lon, lat in LON_LAT_CORNERS]
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))


def _setup(tmp_path, mitad_datos=True):
    xmin, ymax, w_m, h_m = _utm_bounds()
    res = 1.0
    W, H = int(w_m), int(h_m)
    gt = (xmin, res, 0.0, ymax, 0.0, -res)
    arr = np.zeros((H, W), dtype=np.float32)
    if mitad_datos:
        arr[:, W // 2:] = 300.0  # solo la mitad derecha tiene dato
    else:
        arr[:, :] = 300.0
    outputs = tmp_path / "outputs"
    outputs.mkdir(exist_ok=True)
    _flight_path(tmp_path / "outputs" / "flight_path.geojson")
    _escribir_raster(outputs / "rgb_orthomosaic.tif", arr, gt,
                     'PROJCS["UTM zone 18N",GEOGCS["WGS 84",'
                     'DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
                     'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],'
                     'PROJECTION["Transverse_Mercator"],PARAMETER["latitude_of_origin",0],'
                     'PARAMETER["central_meridian",-75],PARAMETER["scale_factor",0.9996],'
                     'PARAMETER["false_easting",500000],PARAMETER["false_northing",0],'
                     'UNIT["metre",1]]')
    return outputs


class TestCoberturaVsVuelo:
    def test_mosaico_recortado_dispara_la_alerta(self, tmp_path):
        _setup(tmp_path, mitad_datos=True)
        r = subprocess.run(["python3", SCRIPT], cwd=str(tmp_path),
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr
        cov = json.loads((tmp_path / "outputs" / "coverage.json").read_text())
        assert cov["alerta"] is True
        assert "Cobertura baja" in cov["mensaje"]
        pct = cov["productos"]["rgb"]["cobertura_area_volada_pct"]
        assert 35 <= pct <= 65, pct  # la mitad derecha sobre el hull entero
        assert cov["area_volada_km2"] and cov["area_volada_km2"] > 0.04

    def test_cobertura_completa_no_alerta(self, tmp_path):
        _setup(tmp_path, mitad_datos=False)
        r = subprocess.run(["python3", SCRIPT], cwd=str(tmp_path),
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0
        cov = json.loads((tmp_path / "outputs" / "coverage.json").read_text())
        assert cov["alerta"] is False
        assert cov["productos"]["rgb"]["cobertura_area_volada_pct"] > 95

    def test_sin_ruta_de_vuelo_omite_sin_fallar(self, tmp_path):
        outputs = tmp_path / "outputs"
        outputs.mkdir()
        arr = np.full((10, 10), 300.0, dtype=np.float32)
        _escribir_raster(outputs / "rgb_orthomosaic.tif", arr,
                         (0, 1, 0, 10, 0, -1), 'EPSG:32618')
        r = subprocess.run(["python3", SCRIPT], cwd=str(tmp_path),
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0
        assert not (tmp_path / "outputs" / "coverage.json").exists()
        assert "sin ruta de vuelo" in r.stdout

    def test_ruta_con_linestring_y_puntos(self, tmp_path):
        """flight_path.geojson real contiene una LineString del track + Points de fotos."""
        outputs = _setup(tmp_path, mitad_datos=False)
        feats = [
            {
                "type": "Feature",
                "properties": {"kind": "flight_track"},
                "geometry": {"type": "LineString", "coordinates": LON_LAT_CORNERS},
            }
        ] + [
            {
                "type": "Feature",
                "properties": {"kind": "capture"},
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
            }
            for lon, lat in LON_LAT_CORNERS
        ]
        (outputs / "flight_path.geojson").write_text(
            json.dumps({"type": "FeatureCollection", "features": feats})
        )
        r = subprocess.run(["python3", SCRIPT], cwd=str(tmp_path),
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr
        cov = json.loads((outputs / "coverage.json").read_text())
        assert cov["alerta"] is False
        assert cov["productos"]["rgb"]["cobertura_area_volada_pct"] > 95

