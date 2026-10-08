#!/usr/bin/env python3
"""Clasifica los índices de vegetación (NDVI/GNDVI/NDRE/MSAVI2) en clases
discretas por cortes estándar de teledetección agrícola/forestal — no
inventados:

  NDVI  (USGS):  1=sin vegetación(<0.1) 2=escasa/estresada(0.1-0.6) 3=densa y sana(>=0.6)
  GNDVI:         1=estrés severo(<0.3) 2=moderada/estresada(0.3-0.5) 3=sana(>=0.5)
  NDRE:          1=deficiencia N(<0.2) 2=transición(0.2-0.3) 3=saludable(0.3-0.6) 4=óptimo/maduro(>=0.6)
  MSAVI2:        mismos cortes que NDVI (0.1/0.6) — MSAVI2 no tiene una
                 convención de cortes tan establecida en la literatura; se
                 reusan los de NDVI como punto de partida razonable (MSAVI2
                 se diseñó para leer en la misma escala 0-1 que NDVI, solo
                 corregido por brillo de suelo). Ver compute_vegetation_indices.py.

Sobre TODA la misión, sin recortar a ningún polígono — el sentido es ver el
estado de vegetación en general, no solo dentro de un área de interés.

Uso: python3 scripts/classify_vegetation_indices.py
Lee: outputs/indices/{ndvi,gndvi,ndre,msavi2}.tif
Escribe: outputs/indices/{ndvi,gndvi,ndre,msavi2}_class.tif
"""
import os
import sys

from osgeo import gdal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from raster_classify import classify, write_class_tif  # noqa: E402

gdal.UseExceptions()

INDICES_DIR = "outputs/indices"

# (breaks, nombres) por índice — límites de literatura, ver docstring
INDEX_SCHEMES = {
    "ndvi": ([0.1, 0.6], ["sin vegetación", "escasa/estresada", "densa y sana"]),
    "gndvi": ([0.3, 0.5], ["estrés severo", "moderada/estresada", "sana"]),
    "ndre": ([0.2, 0.3, 0.6], ["deficiencia N", "transición", "saludable", "óptimo/maduro"]),
    "msavi2": ([0.1, 0.6], ["sin vegetación", "escasa/estresada", "densa y sana"]),
}


def main():
    hecho = 0
    for name, (breaks, labels) in INDEX_SCHEMES.items():
        idx_path = os.path.join(INDICES_DIR, f"{name}.tif")
        if not os.path.isfile(idx_path):
            print(f"  ⚠ {idx_path} no existe, omitiendo clasificación de {name}")
            continue
        idx_ds = gdal.Open(idx_path)
        gt, proj = idx_ds.GetGeoTransform(), idx_ds.GetProjection()
        vals = idx_ds.GetRasterBand(1).ReadAsArray()
        idx_ds = None
        import numpy as np
        idx_valid = np.isfinite(vals)
        idx_cls = classify(vals, breaks, idx_valid)
        for i, lbl in enumerate(labels, 1):
            n = int((idx_cls == i).sum())
            print(f"  {name} {lbl}: {n} px")
        write_class_tif(os.path.join(INDICES_DIR, f"{name}_class.tif"), idx_cls, gt, proj)
        hecho += 1
    if hecho == 0:
        print("  (sin índices de vegetación generados — nada que clasificar)")


if __name__ == "__main__":
    sys.exit(main() or 0)
