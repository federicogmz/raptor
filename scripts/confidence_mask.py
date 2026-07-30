#!/usr/bin/env python3
"""Limpieza final de bordes + máscara de confianza combinada RGB+térmico.

Problema: aunque trim_low_overlap_edges.py ya descarta el borde de bajo
solape por criterios geométricos, en el borde mismo quedan artefactos
sub-geométricos: motas de nodata sueltas dentro del área válida (ruido de
JPEG/interpolación, <50px) y protrusiones/pelos de 1-2px en el contorno
(de morfología irregular de costuras). Ninguno de los dos es "borde real
de vuelo" — son ruido de muy baja escala.

Limpieza (igual para RGB y térmico, mismo criterio):
  1. Rellenar motas de NODATA pequeñas (<MIN_SPECK_PX) rodeadas de datos
     válidos — no son huecos reales, son ruido puntual.
  2. Quitar motas de DATO VÁLIDO pequeñas y sueltas (<MIN_SPECK_PX, fuera
     del cuerpo principal) — protrusiones aisladas sin respaldo.
  3. Suavizado morfológico leve (opening+closing, 1px) — quita pelos/dientes
     de 1px del contorno sin comerse borde real (que tiene escala >>1px).

Máscara de confianza: ambos productos cubren áreas distintas (RGB es el
vuelo completo; térmico es un subconjunto). La confianza no es "ambos
tienen dato" (eso sería casi siempre = footprint térmico) sino "ambos
productos, en su resolución nativa, pasaron su propio control de calidad" —
se exporta como máscara binaria en la grilla del térmico (el factor
limitante) para que cualquier análisis conjunto (fusión RGB+térmico) sepa
exactamente qué área tiene respaldo confiable en LOS DOS productos.

Salida: limpia outputs/rgb_orthomosaic.tif y outputs/thermal_orthomosaic.tif
in-place, y escribe outputs/confidence_mask.tif (grilla térmica, 0/255).
"""
import numpy as np
from osgeo import gdal
from scipy import ndimage

gdal.UseExceptions()


def _pct_change(before, after):
    return f"{100*(after-before)/before:+.2f}%" if before else "n/a (0 px antes)"


def _pct_of(num, den):
    return f"{100*num/den:.1f}%" if den else "n/a (0 px)"


RGB_PATH = "outputs/rgb_orthomosaic.tif"
TH_PATH  = "outputs/thermal_orthomosaic.tif"
CONF_PATH = "outputs/confidence_mask.tif"

MIN_SPECK_PX = 50   # motas (nodata interior o dato suelto) menores a esto = ruido
SMOOTH_ITERS = 1    # opening+closing leve: quita pelos de 1px sin comerse borde real


def clean_mask(valid):
    """Rellena motas de nodata chicas, quita motas de dato suelto chicas,
    suaviza el contorno levemente. No toca estructura de escala real."""
    # 1) motas de NODATA chicas rodeadas de válido -> rellenar (no son hueco real)
    invalid = ~valid
    lbl_i, n_i = ndimage.label(invalid)
    sizes_i = np.bincount(lbl_i.ravel())
    small_i = (sizes_i < MIN_SPECK_PX)
    small_i[0] = False
    fill_holes = small_i[lbl_i]
    out = valid | fill_holes

    # 2) motas de DATO válido chicas y sueltas (no pegadas al cuerpo principal)
    lbl_v, n_v = ndimage.label(out)
    if n_v > 1:
        sizes_v = np.bincount(lbl_v.ravel())
        main_v = int(np.argmax(sizes_v[1:])) + 1
        small_v = (sizes_v < MIN_SPECK_PX) & (np.arange(len(sizes_v)) != main_v)
        small_v[0] = False
        drop = small_v[lbl_v]
        out = out & ~drop

    # 3) suavizado morfológico leve del contorno (pelos/dientes de 1px)
    out = ndimage.binary_opening(out, iterations=SMOOTH_ITERS)
    out = ndimage.binary_closing(out, iterations=SMOOTH_ITERS)
    return out


def clean_rgb():
    ds = gdal.Open(RGB_PATH)
    gt, proj = ds.GetGeoTransform(), ds.GetProjection()
    W, H = ds.RasterXSize, ds.RasterYSize
    full = ds.ReadAsArray()
    # CERRAR antes de re-crear el MISMO archivo: Create() lo trunca, y dejar el
    # dataset de lectura abierto sobre un fichero que ya no existe es pedirle a
    # la caché de bloques de GDAL que sirva datos de un inode borrado.
    ds = None
    valid = full[3] == 255
    before = int(valid.sum())
    cleaned = clean_mask(valid)
    after = int(cleaned.sum())
    print(f"RGB: limpieza de motas/contorno: {before:,} -> {after:,} px "
          f"({_pct_change(before, after)})")

    drv = gdal.GetDriverByName("GTiff")
    out_ds = drv.Create(RGB_PATH, W, H, 4, gdal.GDT_Byte,
                         ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_NEEDED"])
    out_ds.SetGeoTransform(gt)
    out_ds.SetProjection(proj)
    # Antes de cualquier WriteArray: libtiff congela los tags baseline
    # (ExtraSamples) al escribir la primera tile — mismo motivo documentado en
    # trim_low_overlap_edges.trim_rgb(). Acá venía después y funcionaba por la
    # heurística RGBA implícita de GDAL para un GeoTIFF Byte de 4 bandas; no
    # conviene depender de esa casualidad.
    out_ds.GetRasterBand(4).SetColorInterpretation(gdal.GCI_AlphaBand)
    for b in range(3):
        out_ds.GetRasterBand(b + 1).WriteArray(full[b])
    out_ds.GetRasterBand(4).WriteArray((cleaned * 255).astype(np.uint8))
    out_ds = None
    return cleaned, gt, W, H


def clean_thermal():
    ds = gdal.Open(TH_PATH)
    gt, proj = ds.GetGeoTransform(), ds.GetProjection()
    W, H = ds.RasterXSize, ds.RasterYSize
    a = ds.GetRasterBand(1).ReadAsArray()
    ds = None                      # ver clean_rgb(): Create() trunca este mismo archivo
    valid = np.isfinite(a)
    before = int(valid.sum())
    cleaned = clean_mask(valid)
    after = int(cleaned.sum())
    print(f"Térmico: limpieza de motas/contorno: {before:,} -> {after:,} px "
          f"({_pct_change(before, after)})")

    out = np.where(cleaned, a, np.nan).astype(np.float32)
    drv = gdal.GetDriverByName("GTiff")
    out_ds = drv.Create(TH_PATH, W, H, 1, gdal.GDT_Float32,
                         ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_NEEDED"])
    out_ds.SetGeoTransform(gt)
    out_ds.SetProjection(proj)
    out_ds.GetRasterBand(1).WriteArray(out)
    out_ds.GetRasterBand(1).SetNoDataValue(float("nan"))
    out_ds = None
    return cleaned, gt, W, H


def build_confidence(rgb_valid, rgb_gt, rgb_w, rgb_h, th_valid, th_gt, th_w, th_h):
    """Resamplea la validez RGB a la grilla térmica (el factor limitante) y
    cruza ambas: 255 donde los DOS productos tienen dato confiable."""
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
    rgb_valid, rgb_gt, rgb_w, rgb_h = clean_rgb()
    th_valid, th_gt, th_w, th_h = clean_thermal()
    build_confidence(rgb_valid, rgb_gt, rgb_w, rgb_h, th_valid, th_gt, th_w, th_h)
    print("✅ outputs/rgb_orthomosaic.tif, outputs/thermal_orthomosaic.tif (limpios) "
          "+ outputs/confidence_mask.tif")
