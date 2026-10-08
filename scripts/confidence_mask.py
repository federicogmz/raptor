#!/usr/bin/env python3
"""Máscara de confianza combinada RGB+térmico.

Ambos productos cubren áreas distintas (RGB es el vuelo completo; térmico es
un subconjunto). La confianza no es "ambos tienen dato" (eso sería casi
siempre = footprint térmico) sino "ambos productos, en su resolución nativa,
tienen dato válido" — se exporta como máscara binaria en la grilla del
térmico (el factor limitante) para que cualquier análisis conjunto (fusión
RGB+térmico) sepa exactamente qué área tiene respaldo en LOS DOS productos.

El único recorte espacial de la misión es el casco convexo de
trim_low_overlap_edges.py — este script solo LEE la validez ya definitiva de
rgb_orthomosaic.tif/thermal_orthomosaic.tif (no los reescribe ni les aplica
ningún filtro propio) y cruza las dos.

Salida: outputs/confidence_mask.tif.
"""
import numpy as np
from osgeo import gdal

gdal.UseExceptions()

RGB_PATH = "outputs/rgb_orthomosaic.tif"
TH_PATH  = "outputs/thermal_orthomosaic.tif"
CONF_PATH = "outputs/confidence_mask.tif"


def _pct_of(num, den):
    return f"{100*num/den:.1f}%" if den else "n/a (0 px)"


def _leer_valido_rgb():
    ds = gdal.Open(RGB_PATH)
    gt = ds.GetGeoTransform()
    W, H = ds.RasterXSize, ds.RasterYSize
    valid = ds.GetRasterBand(4).ReadAsArray() == 255
    ds = None
    return valid, gt, W, H


def _leer_valido_thermal():
    ds = gdal.Open(TH_PATH)
    gt = ds.GetGeoTransform()
    W, H = ds.RasterXSize, ds.RasterYSize
    valid = np.isfinite(ds.GetRasterBand(1).ReadAsArray())
    ds = None
    return valid, gt, W, H


def build_confidence(rgb_valid, rgb_gt, rgb_w, rgb_h, th_valid, th_gt, th_w, th_h):
    """Resamplea la validez RGB a la grilla térmica (el factor limitante) y
    cruza ambas: 255 donde los DOS productos tienen dato válido."""
    rows = np.arange(th_h); cols = np.arange(th_w)
    CC, RR = np.meshgrid(cols, rows)
    UX = th_gt[0] + (CC + 0.5) * th_gt[1]
    UY = th_gt[3] + (RR + 0.5) * th_gt[5]
    RC = ((UX - rgb_gt[0]) / rgb_gt[1]).astype(int)
    RR2 = ((UY - rgb_gt[3]) / rgb_gt[5]).astype(int)
    ok = (RC >= 0) & (RC < rgb_w) & (RR2 >= 0) & (RR2 < rgb_h)
    rgb_on_th = np.zeros((th_h, th_w), bool)
    rgb_on_th[ok] = rgb_valid[RR2[ok], RC[ok]]

    confidence = rgb_on_th & th_valid
    print(f"Confianza combinada (RGB válido AND térmico válido, grilla térmica): "
          f"{int(confidence.sum()):,} px ({100*confidence.mean():.1f}% de la grilla térmica, "
          f"{_pct_of(int(confidence.sum()), int(th_valid.sum()))} del footprint térmico)")

    drv = gdal.GetDriverByName("GTiff")
    out_ds = drv.Create(CONF_PATH, th_w, th_h, 1, gdal.GDT_Byte,
                         ["COMPRESS=LZW", "TILED=YES"])
    out_ds.SetGeoTransform(th_gt)
    srs = gdal.Open(TH_PATH).GetProjection()
    out_ds.SetProjection(srs)
    out_ds.GetRasterBand(1).WriteArray((confidence * 255).astype(np.uint8))
    out_ds.GetRasterBand(1).SetNoDataValue(0)
    out_ds = None
    print(f"✅ {CONF_PATH}")


if __name__ == "__main__":
    rgb_valid, rgb_gt, rgb_w, rgb_h = _leer_valido_rgb()
    th_valid, th_gt, th_w, th_h = _leer_valido_thermal()
    build_confidence(rgb_valid, rgb_gt, rgb_w, rgb_h, th_valid, th_gt, th_w, th_h)
