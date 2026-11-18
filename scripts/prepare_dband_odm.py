#!/usr/bin/env python3
"""
Prepara la banda D (cámara RGB del DJI M3M) para ODM — un mosaico visible
rápido, independiente de las 4 bandas espectrales y del vuelo M3T/H20T (si
lo hay). Comparte con prepare_rgb_odm.py la copia limpia sin `MakerNote` DJI
vía ``scripts/odm_staging.copiar_y_limpiar_exif`` (tomas reales `*_D.JPG` del
M3M disparan `IndexError` en `exifread.decode_maker_note` durante la etapa
`dataset` de ODM si se enlazan en crudo), y genera ``geo.txt`` con soporte PPK.

Produce:
  1. processing/dband_odm/images/*_D.JPG (copia --reflink=auto limpia sin MakerNotes)
  2. processing/dband_odm/geo.txt (fallback RTK/PPK para ODM)

Uso:
  python3 scripts/prepare_dband_odm.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hardware import safe_concurrency  # noqa: E402
from odm_staging import (  # noqa: E402
    copiar_y_limpiar_exif,
    escribir_camera_seed,
    escribir_geo_txt,
    recolectar_exif,
)

# Mismo criterio memory-aware que prepare_multispectral_odm.py (ver el
# comentario largo ahí sobre por qué no es cpu_count() a secas) — mismo
# sensor M3M, aunque banda D ejecuta también exiftool en paralelo.
_override = os.environ.get("MAX_CONCURRENCY")
NPROCS = safe_concurrency(5, override=int(_override) if _override else None)

D_JPG_DIR = "data/dband_mosaico"
MRK_DIR = os.environ.get("RAPTOR_MRK_DBAND_DIR", "data/mrk/dband")
OUTPUT_DIR = "processing/dband_odm"
IMAGES_DIR = os.path.join(OUTPUT_DIR, "images")
GEO_TXT = os.path.join(OUTPUT_DIR, "geo.txt")


def main():
    if not os.path.isdir(D_JPG_DIR):
        print(f"❌ ERROR: Directorio fuente no encontrado: {D_JPG_DIR}")
        sys.exit(1)

    print("=" * 60)
    print("  Preparación de la banda D (RGB, M3M) para ODM")
    print("=" * 60)
    print()

    nombres = sorted(f for f in os.listdir(D_JPG_DIR) if f.upper().endswith("_D.JPG"))
    if not nombres:
        print(f"❌ No se encontraron imágenes *_D.JPG en {D_JPG_DIR}")
        sys.exit(1)
    print(f"  Imágenes encontradas: {len(nombres)}")

    copiar_y_limpiar_exif(
        D_JPG_DIR,
        IMAGES_DIR,
        nombres,
        NPROCS,
        etiqueta="imágenes banda D",
        sufijos_limpieza=("_D.JPG",),
    )

    rutas = [os.path.join(IMAGES_DIR, f) for f in nombres]
    con_gps, _ = escribir_geo_txt(rutas, GEO_TXT, NPROCS, etiqueta="imágenes", mrk_dir=MRK_DIR)
    registros, _ = recolectar_exif(rutas, GEO_TXT, NPROCS, etiqueta="imágenes", mrk_dir=MRK_DIR)
    escribir_camera_seed(registros, os.path.join(OUTPUT_DIR, ".raptor_camera_seed.json"), etiqueta="imágenes banda D")

    print(f"\n✅ {con_gps}/{len(nombres)} imágenes con GPS")
    print(f"   Directorio ODM: {OUTPUT_DIR}/")
    print(f"   geo.txt: {GEO_TXT}")
    print("\nSiguiente paso: ODM banda D (invocado por docker/entrypoint.sh)")


if __name__ == "__main__":
    main()
