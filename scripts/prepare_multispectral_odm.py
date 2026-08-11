#!/usr/bin/env python3
"""
Prepara las bandas multiespectrales (DJI M3M) para ODM.

A diferencia del térmico, las 4 bandas MS ya traen GPS EXIF nativo (RTK) —
no hace falta inyectar nada, solo dejarlas en images/ y generar geo.txt como
fallback. El trabajo real lo hace scripts/odm_staging.py, compartido con
prepare_dband_odm.py (mismo sensor, mismo tratamiento).

Produce:
  1. processing/multispectral_odm/images/*_MS_{G,R,RE,NIR}.TIF
     (hardlink a data/, ver odm_staging — no una copia: son ~23 GB en una
     misión mediana y ODM solo los lee)
  2. processing/multispectral_odm/geo.txt (fallback para ODM)

Uso:
  python3 scripts/prepare_multispectral_odm.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hardware import safe_concurrency  # noqa: E402
from odm_staging import escribir_geo_txt, poblar_images  # noqa: E402

# Cada enlace/lectura de exiftool es independiente (sin estado compartido) y
# confirmado en vivo como el cuello de botella real de esta etapa: un vuelo
# M3M mediano son miles de TIFF (2340 en la corrida donde se encontró esto)
# uno por uno, con CPU/RAM/GPU del resto de la máquina ociosos mientras
# tanto. PERO: la primera versión de este fix usaba cpu_count() a secas
# (todos los núcleos) y coincidió con un CRASH POR MEMORIA de toda la PC del
# usuario en la corrida donde se probó en vivo — no hay certeza de que haya
# sido la causa única, pero es exactamente el mismo patrón de riesgo que
# entrypoint.sh ya documenta para el band alignment de ODM sobre este MISMO
# sensor (M3M, bandas de 5 MP: "~2.5 GB por hilo... con el kernel matando el
# proceso sin dejar ninguna traza", ver safe_concurrency() en
# entrypoint.sh/hardware.py). Se reusa la misma función memory-aware en vez
# de cpu_count() a secas — más conservador de lo que enlazar/leer metadata
# necesitarían en teoría, pero es la cautela correcta después de lo que pasó.
# MAX_CONCURRENCY (mismo nombre que usa ODM) es la vía de escape manual.
_override = os.environ.get("MAX_CONCURRENCY")
NPROCS = safe_concurrency(5, override=int(_override) if _override else None)

MS_TIF_DIR  = "data/multiespectral_mosaico"
OUTPUT_DIR  = "processing/multispectral_odm"
IMAGES_DIR  = os.path.join(OUTPUT_DIR, "images")
GEO_TXT     = os.path.join(OUTPUT_DIR, "geo.txt")
BAND_SUFFIXES = ("_MS_G.TIF", "_MS_R.TIF", "_MS_RE.TIF", "_MS_NIR.TIF")


def main():
    if not os.path.isdir(MS_TIF_DIR):
        print(f"❌ ERROR: Directorio fuente no encontrado: {MS_TIF_DIR}")
        sys.exit(1)

    print("=" * 60)
    print("  Preparación de bandas multiespectrales para ODM")
    print("=" * 60)
    print()

    nombres = poblar_images(MS_TIF_DIR, IMAGES_DIR, BAND_SUFFIXES, NPROCS,
                            etiqueta="bandas")
    if not nombres:
        print(f"❌ No se encontraron bandas *_MS_*.TIF en {MS_TIF_DIR}")
        sys.exit(1)
    print(f"  Bandas encontradas: {len(nombres)}")

    # geo.txt necesita una fila POR ARCHIVO, no por captura: van las 4 bandas.
    rutas = [os.path.join(IMAGES_DIR, f) for f in nombres]
    con_gps, _ = escribir_geo_txt(rutas, GEO_TXT, NPROCS, etiqueta="bandas")

    print(f"\n✅ {con_gps}/{len(nombres)} bandas con GPS")
    print(f"   Directorio ODM: {OUTPUT_DIR}/")
    print(f"   geo.txt: {GEO_TXT}")
    print("\nSiguiente paso: ODM multiespectral (invocado por docker/entrypoint.sh)")


if __name__ == "__main__":
    main()
