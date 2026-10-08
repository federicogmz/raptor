#!/usr/bin/env python3
"""Utilidades compartidas para clasificar un ráster continuo en clases
discretas por cortes fijos, y escribirlas a GeoTIFF con limpieza de ruido.

Antes vivían en compute_severity_classes.py (que también calculaba
severidad recortada al polígono de área afectada); se separaron al sacar
esa lógica — compute_thermal_hotspot.py y classify_vegetation_indices.py
son los dos consumidores reales.
"""
import os

from osgeo import gdal

gdal.UseExceptions()

# Área mínima cartografiable (MMU, "minimum mapping unit"): un blob de 1-2
# píxeles en una clase alta puede ser ruido de reconstrucción, no una señal
# real — reportado en vivo, focos térmicos de 1-2 px disparando alertas que
# no eran representativas. En METROS CUADRADOS, no píxeles fijos: un número
# fijo de píxeles no significa lo mismo en dos mosaicos con distinto GSD —
# write_class_tif() lo convierte al umbral de píxeles con la resolución REAL
# de CADA ráster (su propio GeoTransform), no una constante calibrada contra
# una sola misión. 1 m² es conservador a escala de dron (GSD sub-métrico).
MMU_M2 = 1.0


def classify(values, breaks, valid):
    import numpy as np
    cls = np.zeros(values.shape, dtype=np.uint8)
    cls[valid & (values < breaks[0])] = 1
    for i in range(len(breaks) - 1):
        cls[valid & (values >= breaks[i]) & (values < breaks[i + 1])] = i + 2
    cls[valid & (values >= breaks[-1])] = len(breaks) + 1
    return cls


def _sieve_mmu(band, gt, label):
    """Funde (no borra) los blobs más chicos que MMU_M2 con el polígono
    vecino más grande — gdal.SieveFilter es el filtro estándar de
    teledetección para esto: a diferencia de poner esos píxeles en nodata,
    no deja huecos, reclasifica el ruido a lo que lo rodea de verdad (que
    puede ser otra clase, o el fondo 0 si el blob está aislado — ahí
    "reclasificar a 0" es exactamente borrar la falsa alerta).
    maskBand=None a propósito: sin máscara, gdal.SieveFilter trata TODOS los
    valores por igual, incluido 0 — con una máscara de nodata (lo default si
    se le pasara band.GetMaskBand()) los píxeles en 0 quedarían excluidos del
    algoritmo, y un blob de foco activo rodeado de fondo (el caso típico que
    se quiere limpiar acá) nunca se tocaría."""
    px_area = abs(gt[1] * gt[5])
    if px_area <= 0:
        return
    threshold_px = max(1, round(MMU_M2 / px_area))
    if threshold_px <= 1:
        return  # a esta resolución un solo píxel YA cubre la MMU real
    gdal.SieveFilter(band, None, band, threshold_px, 4)
    print(f"  MMU {label}: {MMU_M2:.2f} m² → {threshold_px} px a esta resolución "
          f"({px_area:.4f} m²/px)")


def write_class_tif(path, arr, gt, proj):
    drv = gdal.GetDriverByName("GTiff")
    out = drv.Create(path, arr.shape[1], arr.shape[0], 1, gdal.GDT_Byte, ["COMPRESS=LZW", "TILED=YES"])
    out.SetGeoTransform(gt)
    out.SetProjection(proj)
    band = out.GetRasterBand(1)
    band.WriteArray(arr)
    band.SetNoDataValue(0)
    _sieve_mmu(band, gt, os.path.basename(path))
    out = None
    print(f"✅ {path}")
