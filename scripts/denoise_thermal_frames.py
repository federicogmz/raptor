#!/usr/bin/env python3
"""Reduce el ruido del sensor amplificado por la corrección de distancia.

distance=25 (vs el default=5 mal calibrado) recupera contraste real, pero
amplifica proporcionalmente el ruido de fondo del sensor (~1.39x medido
empíricamente en zona uniforme). Un filtro de mediana lo reduciría pero
también recorta picos reales (probado: -17.5°C en el píxel más caliente
del incendio). Se usa un filtro BILATERAL simple (3x3, ponderado por
similitud de intensidad) que sí preserva extremos reales:
  - en zonas planas (ruido) los vecinos son similares → promedia → reduce ruido.
  - en un pico real aislado (ej. incendio) los vecinos son muy distintos →
    peso≈0 → el valor casi no cambia (-0.1°C medido en el frame del incendio
    vs -17.5°C de la mediana).
"""
import glob
import subprocess
import time
import os

import numpy as np
from osgeo import gdal

gdal.UseExceptions()

THERMAL_DIR = "preprocessing/thermal_dji_sdk"
THERMAL_DENOISE_DIR = "preprocessing/thermal_dji_sdk_denoised"
SIGMA_R = 1.5  # °C, ancho de la ventana de similitud de intensidad


def bilateral_3x3(img: np.ndarray, sigma_r: float = SIGMA_R) -> np.ndarray:
    H, W = img.shape
    pad = np.pad(img, 1, mode="edge")
    acc = img.copy()
    wacc = np.ones_like(img)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            nb = pad[1 + dy:1 + dy + H, 1 + dx:1 + dx + W]
            diff = nb - img
            w = np.exp(-(diff * diff) / (2 * sigma_r * sigma_r))
            acc += nb * w
            wacc += w
    return acc / wacc


def main():
    os.makedirs(THERMAL_DENOISE_DIR, exist_ok=True)
    all_files = sorted(glob.glob(f"{THERMAL_DIR}/*.tif"))

    # Salteo de ya procesados: un retry sobre una misión ya denoised no debería
    # volver a correr el filtro bilateral (pixel a pixel, Python puro) sobre
    # cientos/miles de frames que ya están listos. Se valida con gdal.Open
    # (no solo os.path.exists) para no confiar en un archivo truncado por una
    # corrida cortada a mitad de escritura.
    files = []
    ya = 0
    for f in all_files:
        out_f = os.path.join(THERMAL_DENOISE_DIR, os.path.basename(f))
        if os.path.exists(out_f):
            ds = gdal.Open(out_f)
            if ds is not None:
                ds = None
                ya += 1
                continue
            os.remove(out_f)  # corrupto: se regenera
        files.append(f)

    print(f"Denoising {len(files)} frames nuevos (bilateral 3x3, sigma_r={SIGMA_R})"
          + (f" — {ya} ya existentes, reutilizados" if ya else "") + "…")
    t0 = time.time()
    out_files = []
    for i, f in enumerate(files):
        ds = gdal.Open(f, gdal.GA_ReadOnly)
        band = ds.GetRasterBand(1)
        gt = ds.GetGeoTransform()
        proj = ds.GetProjection()
        a = band.ReadAsArray().astype(np.float32)
        ds = None
        
        valid = np.isfinite(a)
        if not valid.all():
            a = np.where(valid, a, np.nanmedian(a[valid]) if valid.any() else 0.0)
        den = bilateral_3x3(a, SIGMA_R)
        
        # Escribir a directorio de salida
        out_f = os.path.join(THERMAL_DENOISE_DIR, os.path.basename(f))
        drv = gdal.GetDriverByName("GTiff")
        out_ds = drv.Create(out_f, den.shape[1], den.shape[0], 1, gdal.GDT_Float32, 
                            ["COMPRESS=LZW", "TILED=YES"])
        out_ds.SetGeoTransform(gt)
        out_ds.SetProjection(proj)
        out_band = out_ds.GetRasterBand(1)
        out_band.WriteArray(den.astype(np.float32))
        out_band.SetNoDataValue(np.nan)
        out_ds = None
        out_files.append(out_f)

        if (i + 1) % 200 == 0:
            print(f"  {i+1}/{len(files)} ({time.time()-t0:.0f}s)")
    print(f"✅ {len(files)} frames procesados en {time.time()-t0:.0f}s")

    # GDAL no copia EXIF/GPS al crear el raster desde cero — se copia acá
    # desde la fuente (mismo patrón que convert_thermal_tiff.py), para que
    # prepare_thermal_native_odm.py pueda usar esta carpeta directamente
    # sin perder el geotag.
    if out_files:
        src_pattern = os.path.join(THERMAL_DIR, "%f.tif")
        result = subprocess.run(
            ["exiftool", "-overwrite_original", "-tagsfromfile", src_pattern,
             "-gps:all", "-GimbalYawDegree", "-GimbalPitchDegree", "-GimbalRollDegree",
             "-FlightYawDegree", "-FlightPitchDegree", "-FlightRollDegree"]
            + out_files,
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"  ⚠ No se pudo copiar GPS/gimbal a los frames denoised: {result.stderr[:300]}")


if __name__ == "__main__":
    main()
