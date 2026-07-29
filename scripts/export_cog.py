#!/usr/bin/env python3
"""Convierte los productos raster finales a COG (Cloud Optimized GeoTIFF):
mismo GeoTIFF de siempre pero con overviews embebidos y una organización
interna que permite lectura parcial por HTTP range-requests (QGIS, Potree,
servidores de tiles pueden leerlos directo sin bajar el archivo entero).

Sobrescribe en el mismo path que ya usan generate_tiles.py y el geovisor —
un COG es un GeoTIFF válido normal (GDAL lo lee exactamente igual), así que
nada corriente abajo se rompe con el cambio.
"""
import os, sys, glob, subprocess, tempfile
from osgeo import gdal

gdal.UseExceptions()

OUTPUTS = "outputs"
PRODUCTS = [
    os.path.join(OUTPUTS, "rgb_orthomosaic.tif"),
    os.path.join(OUTPUTS, "thermal_orthomosaic.tif"),
    os.path.join(OUTPUTS, "dsm.tif"),
    os.path.join(OUTPUTS, "confidence_mask.tif"),
    os.path.join(OUTPUTS, "multispectral_orthomosaic.tif"),
] + sorted(glob.glob(os.path.join(OUTPUTS, "indices", "*.tif")))


def _predictor_for(path):
    """PREDICTOR de compresión según el tipo de dato real (no asumido):
    3 = floating point (DSM, térmico, multiespectral, índices), 2 = entero
    (RGB byte, máscara de confianza), 1 = sin predictor (resto)."""
    ds = gdal.Open(path)
    dt = ds.GetRasterBand(1).DataType
    ds = None
    name = gdal.GetDataTypeName(dt)
    if name in ("Float32", "Float64"):
        return "3"
    if name in ("Byte", "Int16", "UInt16", "Int32", "UInt32"):
        return "2"
    return "1"


def to_cog(path):
    predictor = _predictor_for(path)
    before = os.path.getsize(path)
    fd, tmp = tempfile.mkstemp(suffix=".tif", dir=os.path.dirname(path) or ".")
    os.close(fd)
    try:
        result = subprocess.run(
            ["gdal_translate", "-of", "COG",
             "-co", "COMPRESS=DEFLATE", "-co", f"PREDICTOR={predictor}",
             "-co", "OVERVIEW_RESAMPLING=AVERAGE", "-co", "BIGTIFF=IF_SAFER",
             path, tmp],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"  ❌ {path}: {result.stderr[:300]}")
            return False
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    after = os.path.getsize(path)
    print(f"  ✅ {path}: {before/1e6:.0f}MB → {after/1e6:.0f}MB (COG, predictor={predictor})")
    return True


def main():
    done = 0
    for p in PRODUCTS:
        if not os.path.isfile(p):
            continue
        if to_cog(p):
            done += 1
        else:
            sys.exit(1)
    if done == 0:
        print("  ⚠ No se encontró ningún raster final en outputs/ para convertir")
    print(f"\n✅ {done} raster(es) convertido(s) a COG")


if __name__ == "__main__":
    main()
