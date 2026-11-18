#!/usr/bin/env python3
"""
Prepara las imágenes RGB (visible) para ODM: copia desde data/ (fuente
inmutable, ya organizada por docker/setup-data.sh) a processing/rgb_odm/
images/ y limpia el EXIF/XMP propio del dron.

Copia REAL vía ``scripts/odm_staging.copiar_y_limpiar_exif`` (compartida con
prepare_dband_odm.py; a diferencia de ``poblar_images`` por hardlink usada solo
en los `.TIF` de prepare_multispectral_odm.py): acá se corre
``exiftool -overwrite_original`` sobre las copias ``cp --reflink=auto`` para no
modificar los originales en ``data/`` (ver docstring en ``scripts/odm_staging.py``).

EXIF: se limpia todo (-all=) salvo -exif:all y -xmp-drone-dji:all.
  - -xmp-drone-dji:all preserva RtkStd{Lon,Lat,Hgt} y RtkFlag como atributos XML
    (-api Compact=Shorthand) para opendm/photo.py.
  - -exif:all preserva Make/Model/FocalLength (crítico para no caer al
    focal_ratio genérico 0.85 y colapsar la escala de reconstrucción).

Además, gestiona opcionalmente `processing/rgb_odm/geo.txt` según
`RAPTOR_RGB_GEO_TXT` (`auto` [default] | `1` | `0`):
  - `auto`: solo genera `geo.txt` si un `*Timestamp.MRK` (`Q==50`) actualizó
    coordenadas, si alguna toma trae `RTK FLOAT` (`RtkFlag` 32/34) o si el
    bloque mezcla tomas FIX y no-FIX. En vuelos uniformemente 100 % RTK FIX o
    uniformemente sin RTK borra cualquier `geo.txt` residual y deja intacto el
    lector XMP nativo de ODM.

Uso:
  python3 scripts/prepare_rgb_odm.py [data_rgb] [images_dir]
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

_override = os.environ.get("MAX_CONCURRENCY")
NPROCS = safe_concurrency(2, override=int(_override) if _override else None)

DATA_RGB = sys.argv[1] if len(sys.argv) > 1 else "data/rgb_mosaico"
IMAGES_DIR = sys.argv[2] if len(sys.argv) > 2 else "processing/rgb_odm/images"
MRK_DIR = os.environ.get("RAPTOR_MRK_RGB_DIR", "data/mrk/rgb")
SUFIJOS = ("_V.JPG", "_W.JPG")
LOTE = 100


def _gestionar_geo_txt(images_dir, nombres):
    """Decide si escribir o limpiar `geo.txt` junto a `images_dir` y siembra cámara."""
    proj_dir = os.path.dirname(os.path.abspath(images_dir))
    geo_txt = os.path.join(proj_dir, "geo.txt")
    seed_path = os.path.join(proj_dir, ".raptor_camera_seed.json")
    modo = os.environ.get("RAPTOR_RGB_GEO_TXT", "auto").strip().lower() or "auto"
    seed_off = os.environ.get("RAPTOR_CAMERA_SEED", "1").strip().lower() in ("0", "false", "no", "off")
    rutas = [os.path.join(images_dir, f) for f in nombres]
    if modo == "0":
        if os.path.isfile(geo_txt):
            os.remove(geo_txt)
        if seed_off:
            if os.path.isfile(seed_path):
                os.unlink(seed_path)
        else:
            registros, _ = recolectar_exif(rutas, geo_txt, NPROCS, etiqueta="imágenes RGB", mrk_dir=MRK_DIR)
            escribir_camera_seed(registros, seed_path, etiqueta="imágenes RGB")
        return
    if modo == "1":
        escribir_geo_txt(rutas, geo_txt, NPROCS, etiqueta="imágenes RGB", mrk_dir=MRK_DIR)
        registros, _ = recolectar_exif(rutas, geo_txt, NPROCS, etiqueta="imágenes RGB", mrk_dir=MRK_DIR)
        escribir_camera_seed(registros, seed_path, etiqueta="imágenes RGB")
        return
    # Modo `auto`: solo escribe geo.txt si corrige vía MRK (Q=50) o en bloques
    # mixtos / RTK FLOAT (un vuelo uniforme sin RTK o 100 % FIX sigue sin geo.txt).
    registros, stats = recolectar_exif(rutas, geo_txt, NPROCS, etiqueta="imágenes RGB", mrk_dir=MRK_DIR)
    if stats["mrk_ppk"] or stats["mixto"] or stats["float"]:
        print(
            "🛰️  Generando geo.txt RGB por "
            + ("actualización MRK (Q=50)" if stats["mrk_ppk"] else "ponderación diferencial RtkFlag / bloque mixto")
            + " …"
        )
        escribir_geo_txt(rutas, geo_txt, NPROCS, etiqueta="imágenes RGB", mrk_dir=MRK_DIR)
    elif os.path.isfile(geo_txt):
        os.remove(geo_txt)
    escribir_camera_seed(registros, seed_path, etiqueta="imágenes RGB")


def main():
    if not os.path.isdir(DATA_RGB):
        print(f"❌ ERROR: Directorio fuente no encontrado: {DATA_RGB}")
        sys.exit(1)
    os.makedirs(IMAGES_DIR, exist_ok=True)

    nombres = sorted(f for f in os.listdir(DATA_RGB) if f.upper().endswith(SUFIJOS))
    if not nombres:
        nombres = sorted(
            f
            for f in os.listdir(DATA_RGB)
            if f.upper().endswith((".JPG", ".JPEG", ".PNG", ".TIF", ".TIFF"))
            and not f.upper().endswith(("_T.JPG", "_T.JPEG", "_D.JPG", "_D.JPEG"))
            and "_MS_" not in f.upper()
        )
    if not nombres:
        print(f"❌ No se encontraron imágenes RGB en {DATA_RGB}")
        sys.exit(1)

    copiar_y_limpiar_exif(
        DATA_RGB,
        IMAGES_DIR,
        nombres,
        NPROCS,
        lote=LOTE,
        etiqueta="imágenes RGB",
        sufijos_limpieza=(".JPG", ".JPEG", ".PNG", ".TIF", ".TIFF"),
    )
    _gestionar_geo_txt(IMAGES_DIR, nombres)


if __name__ == "__main__":
    main()
