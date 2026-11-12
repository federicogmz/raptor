#!/usr/bin/env python3
"""Clasifica focos térmicos (hotspot) a partir del ortomosaico térmico —
temperatura ABSOLUTA, no anomalía relativa: un umbral relativo da falsos
positivos en suelo/cultivo calentado por el sol en días despejados.

La literatura operacional de detección de hotspots con drones en incendios
usa 190-250°F (~88-121°C) como umbral de "fuego activo bajo superficie"
(predetermined threshold para mop-up/residual heat monitoring). Se usa el
extremo bajo de ese rango (88°C) como corte de "foco activo".
  1=normal(<40°C) 2=elevado(40-60°C) 3=caliente(60-88°C) 4=foco activo(>=88°C)
Fuente: literatura de UAV thermal imaging para wildfire hotspot/mop-up
detection (predetermined threshold 190-250°F).

Se muestra SIN recortar a ningún polígono: cubre toda la extensión con dato
térmico válido. Es la única fuente de hotspot del pipeline — antes existía
una segunda versión recortada a un polígono de "área afectada" derivado de
NDVI, retirada por no dar resultados confiables.

Uso: python3 scripts/compute_thermal_hotspot.py
Lee: outputs/thermal_orthomosaic.tif
Escribe: outputs/termico_hotspot_class.tif
"""

import os
import sys

import numpy as np
from osgeo import gdal

gdal.UseExceptions()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from raster_classify import _sieve_mmu, classify  # noqa: E402

STRIP_ROWS = int(os.environ.get("RAPTOR_STRIP_ROWS", 1024))
TH_PATH = "outputs/thermal_orthomosaic.tif"
OUT_PATH = "outputs/termico_hotspot_class.tif"
LABELS = ["normal", "elevado", "caliente", "foco activo"]
TERM_BREAKS = [40.0, 60.0, 88.0]  # °C absolutos — ver docstring


def main():
    if not os.path.isfile(TH_PATH):
        print(f"❌ ERROR: {TH_PATH} no encontrado (¿corrió trim-edges-thermal?)")
        sys.exit(1)

    ds = gdal.Open(TH_PATH)
    gt, proj = ds.GetGeoTransform(), ds.GetProjection()
    W, H = ds.RasterXSize, ds.RasterYSize
    in_band = ds.GetRasterBand(1)
    nodata = in_band.GetNoDataValue()

    drv = gdal.GetDriverByName("GTiff")
    out = drv.Create(OUT_PATH, W, H, 1, gdal.GDT_Byte, ["COMPRESS=LZW", "TILED=YES"])
    out.SetGeoTransform(gt)
    out.SetProjection(proj)
    out_band = out.GetRasterBand(1)
    out_band.SetNoDataValue(0)

    class_counts = {i: 0 for i in range(1, len(LABELS) + 1)}
    total_valid = 0

    for yoff in range(0, H, STRIP_ROWS):
        num_rows = min(STRIP_ROWS, H - yoff)
        temp_strip = in_band.ReadAsArray(0, yoff, W, num_rows)
        valid_strip = np.isfinite(temp_strip)
        if nodata is not None:
            valid_strip &= ~np.isclose(temp_strip, nodata) & (temp_strip > -1e30) & (temp_strip < 1e30)
        else:
            valid_strip &= (temp_strip > -1e30) & (temp_strip < 1e30)

        thc_strip = classify(temp_strip, TERM_BREAKS, valid_strip)
        out_band.WriteArray(thc_strip, 0, yoff)

        total_valid += int(valid_strip.sum())
        for i in range(1, len(LABELS) + 1):
            class_counts[i] += int((thc_strip == i).sum())

    ds = None

    for i, lbl in enumerate(LABELS, 1):
        n = class_counts[i]
        pct = 100 * n / total_valid if total_valid else 0
        print(f"  térmico {lbl}: {n:,} px ({pct:.1f}%)")

    _sieve_mmu(out_band, gt, os.path.basename(OUT_PATH))
    out = None
    print(f"✅ {OUT_PATH} (hotspot térmico sobre toda la cobertura)")


if __name__ == "__main__":
    main()
