#!/usr/bin/env python3
"""
Convierte R-JPEG térmicos → GeoTIFF Float32 con valores de temperatura (°C).

Usa el binario dji_irp del DJI Thermal SDK v1.8 para extraer los datos
radiométricos en formato float32 crudo, y los envuelve en GeoTIFF.
"""

import os, sys, subprocess, glob, tempfile, time
import numpy as np
from osgeo import gdal

gdal.UseExceptions()

# ── Configuración ──────────────────────────────────────────────────
_BASE = os.path.dirname(os.path.abspath(__file__))
SDK_LIB_DIR = os.path.join(_BASE, "..", "dji_thermal_sdk/tsdk-core/lib/linux/release_x64")
DJI_IRP     = os.path.join(_BASE, "..", "dji_thermal_sdk/utility/bin/linux/release_x64/dji_irp")
IMG_W, IMG_H = 640, 512  # DJI H20T thermal sensor


def convert_one(src_jpg: str, dst_tif: str) -> bool:
    """Convierte un R-JPEG a GeoTIFF Float32 usando dji_irp."""
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = SDK_LIB_DIR + ":" + env.get("LD_LIBRARY_PATH", "")

    with tempfile.NamedTemporaryFile(suffix=".raw", delete=False) as tmp:
        raw_path = tmp.name

    try:
        # 1. Extraer raw float32 con dji_irp
        # distance=25 (máximo permitido por el SDK para M3T): el R-JPEG trae
        # distance=5m de fábrica (nunca configurado para vuelo a ~500m AGL),
        # lo que comprime el rango dinámico (subestima extremos calientes/fríos).
        # 25m sigue siendo la cota dura del sensor, pero es lo mismo que se usó
        # para la entrega a Agisoft → comparación justa.
        result = subprocess.run(
            [DJI_IRP, "-s", src_jpg, "-a", "measure",
             "-o", raw_path, "--measurefmt", "float32", "--distance", "25"],
            env=env,
            capture_output=True, text=True,
            timeout=30
        )
        if result.returncode != 0:
            print(f"  ❌ dji_irp error: {result.stderr[:200]}")
            return False

        if not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
            print(f"  ❌ dji_irp no produjo salida")
            return False

        # 2. Leer raw y envolver en GeoTIFF
        raw = np.fromfile(raw_path, dtype=np.float32)
        expected = IMG_W * IMG_H
        if len(raw) != expected:
            print(f"  ❌ Tamaño inesperado: {len(raw)} floats (esperado {expected})")
            return False

        arr = raw.reshape(IMG_H, IMG_W)

        drv = gdal.GetDriverByName("GTiff")
        ds = drv.Create(dst_tif, IMG_W, IMG_H, 1, gdal.GDT_Float32,
                        ["COMPRESS=LZW", "TILED=YES"])
        ds.GetRasterBand(1).WriteArray(arr)
        ds.GetRasterBand(1).SetNoDataValue(np.nan)
        ds.FlushCache()
        ds = None

        # 3. Geolocalizar: copiar el GPS/gimbal EXIF del R-JPEG fuente (el
        # drone ya lo embebió ahí) al TIFF de temperatura. dji_irp por sí
        # solo NO propaga nada de esto — solo decodifica el arreglo crudo.
        # Doble uso: (a) prepare_thermal_native_odm.py lo necesita para
        # armar geo.txt del proyecto ODM térmico nativo (y lo vuelve a copiar
        # a los TIFF re-encodeados en Kelvin×100 que arma para ODM); (b)
        # exportar/entregar estos TIFF Float32 °C a software externo
        # (Agisoft, Pix4D) que arma su propia alineación a partir del
        # GPS/gimbal de cada foto individual.
        # Best-effort: si la fuente no tiene GPS o exiftool falla, se deja
        # el TIFF sin geotags en vez de fallar toda la conversión (el dato
        # radiométrico ya escrito arriba sigue siendo válido igual).
        try:
            subprocess.run(
                ["exiftool", "-overwrite_original", "-tagsfromfile", src_jpg,
                 "-gps:all", "-GimbalYawDegree", "-GimbalPitchDegree", "-GimbalRollDegree",
                 "-FlightYawDegree", "-FlightPitchDegree", "-FlightRollDegree",
                 "-RelativeAltitude",
                 dst_tif],
                capture_output=True, text=True, timeout=15
            )
        except Exception:
            pass

        return True

    finally:
        if os.path.exists(raw_path):
            os.unlink(raw_path)


def main():
    src_dir = sys.argv[1] if len(sys.argv) > 1 else "data/termica_mosaico"
    dst_dir = sys.argv[2] if len(sys.argv) > 2 else "preprocessing/thermal_dji_sdk"

    os.makedirs(dst_dir, exist_ok=True)

    # Pre-flight: check dji_irp exists
    if not os.path.isfile(DJI_IRP):
        print(f"❌ dji_irp no encontrado en {DJI_IRP}")
        print("   Extrae el SDK: unzip dji_thermal_sdk_v1.8_20250829.zip -d dji_thermal_sdk/")
        sys.exit(1)

    jpgs = sorted(glob.glob(os.path.join(src_dir, "*_T.JPG")))
    if not jpgs:
        print(f"❌ No se encontraron imágenes *_T.JPG en {src_dir}")
        sys.exit(1)

    print(f"Convirtiendo {len(jpgs)} R-JPEG → GeoTIFF Float32...")
    t0 = time.time()
    ok = 0
    skip = 0
    fail = 0

    for i, jpg in enumerate(jpgs):
        stem = os.path.basename(jpg).replace(".JPG", "")
        dst = os.path.join(dst_dir, f"{stem}.tif")

        # Skip if already valid
        if os.path.exists(dst):
            ds = gdal.Open(dst)
            if ds is not None:
                skip += 1
                ds = None
                continue
            # Corrupt file — regenerate
            os.remove(dst)

        if convert_one(jpg, dst):
            ok += 1
        else:
            fail += 1

        if (i + 1) % 100 == 0:
            elapsed = time.time() - t0
            print(f"  {i+1}/{len(jpgs)} ({ok} ok, {skip} skip, {fail} fail) — {elapsed:.0f}s")

    elapsed = time.time() - t0
    print(f"\n✅ {ok} convertidos, {skip} existentes, {fail} fallidos")
    print(f"   Tiempo: {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"   Directorio: {os.path.abspath(dst_dir)}/")

    if fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
