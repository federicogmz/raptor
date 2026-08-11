#!/usr/bin/env python3
"""
Convierte R-JPEG térmicos → GeoTIFF Float32 con valores de temperatura (°C).

Usa el binario dji_irp del DJI Thermal SDK v1.8 para extraer los datos
radiométricos en formato float32 crudo, y los envuelve en GeoTIFF.
"""

import os, sys, subprocess, glob, tempfile, time
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np
from osgeo import gdal

gdal.UseExceptions()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hardware import safe_concurrency  # noqa: E402

# Cada imagen es una llamada a dji_irp + exiftool sobre su propio archivo
# temporal (tempfile.NamedTemporaryFile ya genera un nombre único por
# llamada) y escribe a su propio .tif — no hay estado compartido entre
# conversiones, así que paralelizar es directo. dji_irp es liviano por
# imagen (0.33 MP el sensor térmico) comparado con el band alignment de ODM,
# así que safe_concurrency(0.33) en la práctica no acota mucho más que los
# núcleos reales — pero usa la MISMA cuenta memory-aware que el resto del
# pipeline (no cpu_count() a secas) para que MAX_CONCURRENCY sea una sola
# perilla consistente en todos lados, y para que docker/entrypoint.sh pueda
# repartir un presupuesto compartido cuando esto corre EN PARALELO con
# prepare-multispectral (ver la preparación en paralelo más abajo) sin que
# cada stream pida lo suyo sin saber del otro.
_override = os.environ.get("MAX_CONCURRENCY")
NPROCS = safe_concurrency(0.33, override=int(_override) if _override else None)

# ── Configuración ──────────────────────────────────────────────────
_BASE = os.path.dirname(os.path.abspath(__file__))
SDK_LIB_DIR = os.path.join(_BASE, "..", "dji_thermal_sdk/tsdk-core/lib/linux/release_x64")
DJI_IRP     = os.path.join(_BASE, "..", "dji_thermal_sdk/utility/bin/linux/release_x64/dji_irp")


def _jpeg_size(path):
    """(ancho, alto) del R-JPEG leyendo solo la cabecera."""
    ds = gdal.Open(path)
    try:
        return ds.RasterXSize, ds.RasterYSize
    finally:
        ds = None


def thermal_size(src_jpg, n_floats):
    """Resolución del arreglo radiométrico de un R-JPEG.

    En los R-JPEG de DJI el stream JPEG está a la misma resolución que el
    sensor térmico, así que las dimensiones de la imagen son las del arreglo.
    Se comprueba contra la cantidad real de floats que devolvió dji_irp antes
    de usarlas: si no cuadran, el reshape produciría un raster corrido o
    fallaría con un error incomprensible.

    Devuelve (w, h) o None si no se puede determinar con certeza.
    """
    w, h = _jpeg_size(src_jpg)
    if w * h == n_floats:
        return w, h
    return None


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
        try:
            result = subprocess.run(
                [DJI_IRP, "-s", src_jpg, "-a", "measure",
                 "-o", raw_path, "--measurefmt", "float32", "--distance", "25"],
                env=env,
                capture_output=True, text=True,
                timeout=30
            )
        except subprocess.TimeoutExpired:
            # Bug real, reportado en vivo: sin este catch, un solo dji_irp
            # colgado (SDK externo, no algo que este script controle)
            # tumbaba TODA la conversión — la excepción se escapaba sin
            # atajar hasta fut.result() en main(), que la relanza y aborta
            # el ThreadPoolExecutor entero en vez de contar esta imagen como
            # UNA falla más (que es lo que ya hace el resto de esta función
            # para cualquier otro tipo de error de dji_irp).
            print(f"  ❌ dji_irp no respondió en 30s para {os.path.basename(src_jpg)} — se salta esta imagen")
            return False
        if result.returncode != 0:
            print(f"  ❌ dji_irp error: {result.stderr[:200]}")
            return False

        if not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
            print(f"  ❌ dji_irp no produjo salida")
            return False

        # 2. Leer raw y envolver en GeoTIFF. La resolución se infiere de la
        # foto: el sensor térmico varía según el modelo (640x512 en H20T/M3T/
        # M30T, 1280x1024 en los más nuevos) y fijarla obligaría a tocar código
        # para cada cámara nueva.
        raw = np.fromfile(raw_path, dtype=np.float32)
        size = thermal_size(src_jpg, len(raw))
        if size is None:
            jw, jh = _jpeg_size(src_jpg)
            print(f"  ❌ {os.path.basename(src_jpg)}: dji_irp devolvió {len(raw):,} "
                  f"valores pero la imagen es {jw}x{jh} ({jw*jh:,} px) — no coinciden, "
                  f"no se puede saber cómo ordenar el arreglo")
            return False
        w, h = size
        arr = raw.reshape(h, w)

        drv = gdal.GetDriverByName("GTiff")
        ds = drv.Create(dst_tif, w, h, 1, gdal.GDT_Float32,
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

    jw, jh = _jpeg_size(jpgs[0])
    print(f"Convirtiendo {len(jpgs)} R-JPEG → GeoTIFF Float32 "
          f"(sensor térmico {jw}x{jh}, inferido de las fotos) "
          f"con {NPROCS} en paralelo...")
    t0 = time.time()
    ok = 0
    skip = 0
    fail = 0
    done = 0

    # Los ya convertidos (skip) se filtran ANTES de mandar nada al pool: no
    # tiene sentido ocupar un hilo en abrir con GDAL un .tif que ni se va a
    # tocar.
    pendientes = []
    for jpg in jpgs:
        stem = os.path.basename(jpg).replace(".JPG", "")
        dst = os.path.join(dst_dir, f"{stem}.tif")
        if os.path.exists(dst):
            ds = gdal.Open(dst)
            if ds is not None:
                skip += 1
                ds = None
                continue
            os.remove(dst)  # corrupto: se regenera
        pendientes.append((jpg, dst))

    with ThreadPoolExecutor(max_workers=NPROCS) as pool:
        futuros = {pool.submit(convert_one, jpg, dst): jpg for jpg, dst in pendientes}
        for fut in as_completed(futuros):
            done += 1
            if fut.result():
                ok += 1
            else:
                fail += 1
            if done % 100 == 0:
                elapsed = time.time() - t0
                print(f"  {done}/{len(pendientes)} ({ok} ok, {fail} fail) — {elapsed:.0f}s")

    elapsed = time.time() - t0
    print(f"\n✅ {ok} convertidos, {skip} existentes, {fail} fallidos")
    print(f"   Tiempo: {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"   Directorio: {os.path.abspath(dst_dir)}/")

    if fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
