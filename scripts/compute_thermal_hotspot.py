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
from raster_classify import classify, write_class_tif  # noqa: E402

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
    temp = ds.GetRasterBand(1).ReadAsArray()
    ds = None

    valid = np.isfinite(temp)
    n_valid = int(valid.sum())
    thc = classify(temp, TERM_BREAKS, valid)

    for i, lbl in enumerate(LABELS, 1):
        n = int((thc == i).sum())
        pct = 100 * n / n_valid if n_valid else 0
        print(f"  térmico {lbl}: {n:,} px ({pct:.1f}%)")

    write_class_tif(OUT_PATH, thc, gt, proj)
    print(f"✅ {OUT_PATH} (hotspot térmico sobre toda la cobertura)")


if __name__ == "__main__":
    main()
