#!/usr/bin/env python3
"""
Prepara la banda D (cámara RGB del DJI M3M) para ODM — un mosaico visible
rápido, independiente de las 4 bandas espectrales y del vuelo M3T/H20T (si
lo hay). Mismo sensor M3M que las bandas G/R/RE/NIR, así que trae el mismo
GPS EXIF nativo (RTK) — igual tratamiento que prepare_multispectral_odm.py,
pero UNA imagen por captura (no 4 que tengan que emparejar). El trabajo real
(enlazar a images/, armar geo.txt) lo hace scripts/odm_staging.py, compartido
entre los dos.

Produce:
  1. processing/dband_odm/images/*_D.JPG (hardlink a data/, ver odm_staging)
  2. processing/dband_odm/geo.txt (fallback para ODM)

Uso:
  python3 scripts/prepare_dband_odm.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hardware import safe_concurrency  # noqa: E402
from odm_staging import escribir_geo_txt, poblar_images  # noqa: E402

# Mismo criterio memory-aware que prepare_multispectral_odm.py (ver el
# comentario largo ahí sobre por qué no es cpu_count() a secas) — mismo
# sensor M3M, mismo perfil de 5 MP.
_override = os.environ.get("MAX_CONCURRENCY")
NPROCS = safe_concurrency(5, override=int(_override) if _override else None)

D_JPG_DIR  = "data/dband_mosaico"
OUTPUT_DIR = "processing/dband_odm"
IMAGES_DIR = os.path.join(OUTPUT_DIR, "images")
GEO_TXT    = os.path.join(OUTPUT_DIR, "geo.txt")


def main():
    if not os.path.isdir(D_JPG_DIR):
        print(f"❌ ERROR: Directorio fuente no encontrado: {D_JPG_DIR}")
        sys.exit(1)

    print("=" * 60)
    print("  Preparación de la banda D (RGB, M3M) para ODM")
    print("=" * 60)
    print()

    nombres = poblar_images(D_JPG_DIR, IMAGES_DIR, ("_D.JPG",), NPROCS,
                            etiqueta="imágenes")
    if not nombres:
        print(f"❌ No se encontraron imágenes *_D.JPG en {D_JPG_DIR}")
        sys.exit(1)
    print(f"  Imágenes encontradas: {len(nombres)}")

    rutas = [os.path.join(IMAGES_DIR, f) for f in nombres]
    con_gps, _ = escribir_geo_txt(rutas, GEO_TXT, NPROCS, etiqueta="imágenes")

    print(f"\n✅ {con_gps}/{len(nombres)} imágenes con GPS")
    print(f"   Directorio ODM: {OUTPUT_DIR}/")
    print(f"   geo.txt: {GEO_TXT}")
    print("\nSiguiente paso: ODM banda D (invocado por docker/entrypoint.sh)")


if __name__ == "__main__":
    main()
