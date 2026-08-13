#!/usr/bin/env python3
"""Cobertura del mosaico vs. el ÁREA REALMENTE VOLADA — la verificación que
faltaba. Escribe outputs/coverage.json y advierte en la salida si algún
producto cubre menos del umbral.

POR QUÉ EXISTE: el problema que más confunde en emergencias no es un error,
es una corrida que \"termina bien\" con un mosaico recortado muy por debajo
del área real volada. Confirmado en vivo (misión mision_2026-08-08, terreno
rocoso, preset vistazo con planar): solo 23.6% de las fotos RGB entraron a
la reconstrucción — sin ningún error visible, porque OpenSfM descarta en
silencio las fotos cuya homografía no encaja en relieve fuerte. Incluso con
la reconstrucción completa (incremental), el ortomosaico final puede cubrir
menos de la mitad del área volada (44.9% en esa misma misión) sin que nadie
lo señale.

La métrica de run_summary (\"cobertura_pct\") mide qué fracción del RECUADRO
del ráster tiene dato — no dice nada de cuánto del área que el dron recorrió
quedó cubierta. Acá la referencia es el convex hull de la ruta de vuelo
(outputs/flight_path.geojson, que export_flight_path.py escribe del GPS/EXIF
de las fotos ANTES de ODM): se rasteriza sobre la grilla de cada ortomosaico
y se mide qué fracción de esos píxeles tiene dato real.

Escribe: outputs/coverage.json
  { \"area_volada_km2\": 3.14, \"umbral_alerta_pct\": 60.0,
    \"alerta\": true, \"mensaje\": \"⚠ ...\",
    \"productos\": {\"rgb\": {\"cobertura_area_volada_pct\": 44.9}, ...} }

Uso: python3 scripts/compute_coverage.py  (corre desde /app, como el resto)
"""
import json
import os

import numpy as np
from osgeo import gdal, ogr, osr

gdal.UseExceptions()

FLIGHT_PATH = "outputs/flight_path.geojson"
OUT_PATH = "outputs/coverage.json"
# Un mosaico que cubre menos del 60% del área volada es señal de
# reconstrucción incompleta (fotos descartadas por planar en relieve, falla
# de matching, etc.) — vale la pena que el operador lo sepa ANTES de decidir
# sobre ese mapa.
UMBRAL_ALERTA_PCT = 60.0
PRODUCTOS = {
    "rgb": "outputs/rgb_orthomosaic.tif",
    "thermal": "outputs/thermal_orthomosaic.tif",
    "multispectral": "outputs/multispectral_orthomosaic.tif",
    "dband": "outputs/dband_orthomosaic.tif",
}


def _hull_vuelo():
    """Convex hull (EPSG:4326) de los puntos de la ruta de vuelo, o None."""
    if not os.path.isfile(FLIGHT_PATH):
        return None
    ds = ogr.Open(FLIGHT_PATH)
    try:
        lyr = ds.GetLayer()
        pts = []
        for feat in lyr:
            g = feat.GetGeometryRef()
            if g is not None:
                pts.append(g.Clone())
        if not pts:
            return None
        mp = ogr.Geometry(ogr.wkbMultiPoint)
        for p in pts:
            mp.AddGeometry(p)
        hull = mp.ConvexHull()
        return hull if hull and not hull.IsEmpty() else None
    finally:
        ds = None


def _valid_mask(ds):
    """Máscara de \"píxel con dato real\" de un ortomosaico: la última banda
    (el alpha, donde lo hay) con valor > 0 y finito. Mismo criterio que
    run_summary.py::_info_raster."""
    band = ds.GetRasterBand(ds.RasterCount)
    arr = band.ReadAsArray()
    if arr is None:
        return np.zeros((ds.RasterYSize, ds.RasterXSize), dtype=bool)
    return np.isfinite(arr) & (arr > 0)


def _rasterizar(hull, gt, proj, W, H):
    """Hull (en el CRS del raster) → máscara booleana sobre la grilla."""
    mem = ogr.GetDriverByName("MEM").CreateDataSource("mem")
    srs = osr.SpatialReference(wkt=proj)
    lay = mem.CreateLayer("hull", srs, ogr.wkbPolygon)
    feat = ogr.Feature(lay.GetLayerDefn())
    feat.SetGeometry(hull)
    lay.CreateFeature(feat)
    ds = gdal.GetDriverByName("MEM").Create("", W, H, 1, gdal.GDT_Byte)
    ds.SetGeoTransform(gt)
    ds.SetProjection(proj)
    band = ds.GetRasterBand(1)
    band.Fill(0)
    gdal.RasterizeLayer(ds, [1], lay, burn_values=[1])
    mask = band.ReadAsArray().astype(bool)
    return mask


def main():
    hull_4326 = _hull_vuelo()
    if hull_4326 is None:
        print("  ⚠ sin ruta de vuelo (flight_path.geojson) — omitiendo cobertura vs vuelo")
        return 0

    src_srs = osr.SpatialReference()
    src_srs.ImportFromEPSG(4326)
    src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    area_volada_m2 = None
    productos = {}
    for nombre, path in PRODUCTOS.items():
        if not os.path.isfile(path):
            continue
        ds = gdal.Open(path)
        try:
            gt, proj = ds.GetGeoTransform(), ds.GetProjection()
            srs = osr.SpatialReference(wkt=proj)
            if area_volada_m2 is None:
                to = osr.CoordinateTransformation(src_srs, srs)
                hull = hull_4326.Clone()
                hull.Transform(to)
                area_volada_m2 = hull.GetArea()
            else:
                to = osr.CoordinateTransformation(src_srs, srs)
                hull = hull_4326.Clone()
                hull.Transform(to)
            mask = _rasterizar(hull, gt, proj, ds.RasterXSize, ds.RasterYSize)
            en_vuelo = int(mask.sum())
            if en_vuelo == 0:
                continue
            valid = _valid_mask(ds)
            cubre = int((valid & mask).sum())
            productos[nombre] = {
                "cobertura_area_volada_pct": round(100.0 * cubre / en_vuelo, 1)}
        finally:
            ds = None

    if not productos:
        print("  ⚠ sin ortomosaicos recortados todavía — omitiendo cobertura vs vuelo")
        return 0

    alerta = any(p["cobertura_area_volada_pct"] < UMBRAL_ALERTA_PCT
                 for p in productos.values())
    mensaje = None
    if alerta:
        peor = min(productos, key=lambda k: productos[k]["cobertura_area_volada_pct"])
        mensaje = (
            f"⚠ Cobertura baja: «{peor}» cubre solo "
            f"{productos[peor]['cobertura_area_volada_pct']}% del área volada "
            f"({area_volada_m2 / 1e6:.2f} km² volados). Posible reconstrucción "
            "incompleta (planar en terreno con relieve descarta fotos en "
            "silencio). Verificá antes de decidir sobre este mapa.")
        print(mensaje)

    data = {
        "area_volada_km2": round(area_volada_m2 / 1e6, 3) if area_volada_m2 else None,
        "umbral_alerta_pct": UMBRAL_ALERTA_PCT,
        "alerta": alerta,
        "mensaje": mensaje,
        "productos": productos,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(OUT_PATH, 0o666)
    except OSError:
        pass
    print(f"✅ {OUT_PATH} (área volada {area_volada_m2 / 1e6:.2f} km², "
          f"{len(productos)} producto(s) con dato)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
