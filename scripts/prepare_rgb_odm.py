#!/usr/bin/env python3
"""
Prepara las imágenes RGB (visible) para ODM: copia desde data/ (fuente
inmutable, ya organizada por docker/setup-data.sh) a processing/rgb_odm/
images/ y limpia el EXIF/XMP propio del dron.

Copia REAL, no hardlink (a diferencia de odm_staging.poblar_images para
multiespectral/banda D): acá se corre exiftool -overwrite_original sobre las
copias, y con un hardlink eso modificaría también el archivo fuente en
data/ — ver la advertencia en scripts/odm_staging.py. Se usa
`cp --reflink=auto`.

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
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hardware import safe_concurrency  # noqa: E402
from odm_staging import escribir_geo_txt, recolectar_exif  # noqa: E402

_override = os.environ.get("MAX_CONCURRENCY")
NPROCS = safe_concurrency(2, override=int(_override) if _override else None)

DATA_RGB = sys.argv[1] if len(sys.argv) > 1 else "data/rgb_mosaico"
IMAGES_DIR = sys.argv[2] if len(sys.argv) > 2 else "processing/rgb_odm/images"
MRK_DIR = os.environ.get("RAPTOR_MRK_RGB_DIR", "data/mrk/rgb")
SUFIJOS = ("_V.JPG", "_W.JPG")
LOTE = 100


def _gestionar_geo_txt(images_dir, nombres):
    """Decide si escribir o limpiar `geo.txt` junto a `images_dir`."""
    geo_txt = os.path.join(os.path.dirname(os.path.abspath(images_dir)), "geo.txt")
    modo = os.environ.get("RAPTOR_RGB_GEO_TXT", "auto").strip().lower() or "auto"
    if modo == "0":
        if os.path.isfile(geo_txt):
            os.remove(geo_txt)
        return
    rutas = [os.path.join(images_dir, f) for f in nombres]
    if modo == "1":
        escribir_geo_txt(rutas, geo_txt, NPROCS, etiqueta="imágenes RGB", mrk_dir=MRK_DIR)
        return
    # Modo `auto`: solo escribe geo.txt si corrige vía MRK (Q=50) o en bloques
    # mixtos / RTK FLOAT (un vuelo uniforme sin RTK o 100 % FIX sigue sin geo.txt).
    _, stats = recolectar_exif(rutas, geo_txt, NPROCS, etiqueta="imágenes RGB", mrk_dir=MRK_DIR)
    if stats["mrk_ppk"] or stats["mixto"] or stats["float"]:
        print(
            "🛰️  Generando geo.txt RGB por "
            + ("actualización MRK (Q=50)" if stats["mrk_ppk"] else "ponderación diferencial RtkFlag / bloque mixto")
            + " …"
        )
        escribir_geo_txt(rutas, geo_txt, NPROCS, etiqueta="imágenes RGB", mrk_dir=MRK_DIR)
    elif os.path.isfile(geo_txt):
        os.remove(geo_txt)


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

    nombres_set = set(nombres)
    viejos = [
        f
        for f in os.listdir(IMAGES_DIR)
        if f.upper().endswith((".JPG", ".JPEG", ".PNG", ".TIF", ".TIFF")) and f not in nombres_set
    ]
    if viejos:
        print(f"🧹 Limpiando {len(viejos)} de otra misión/corrida …")
        for f in viejos:
            os.remove(os.path.join(IMAGES_DIR, f))

    pendientes = [f for f in nombres if not os.path.exists(os.path.join(IMAGES_DIR, f))]
    ya = len(nombres) - len(pendientes)
    if not pendientes:
        print(f"✅ {ya} imágenes RGB ya estaban preparadas, nada que copiar")
        _gestionar_geo_txt(IMAGES_DIR, nombres)
        return

    print(
        f"📋 Copiando {len(pendientes)} imágenes RGB nuevas con {NPROCS} en paralelo"
        + (f" ({ya} ya existentes, reutilizadas) …" if ya else " …")
    )

    def _tmp_path(f):
        return os.path.join(IMAGES_DIR, f".tmp_{f}")

    def _copiar(f):
        subprocess.run(
            ["cp", "--reflink=auto", os.path.join(DATA_RGB, f), _tmp_path(f)],
            check=True,
        )

    def _limpiar_y_promover(lote_nombres):
        lote_tmp = [_tmp_path(f) for f in lote_nombres]
        r = subprocess.run(
            [
                "exiftool",
                "-api",
                "Compact=Shorthand",
                "-all=",
                "-tagsfromfile",
                "@",
                "-exif:all",
                "-xmp-drone-dji:all",
                "-overwrite_original",
                "-q",
                "-@",
                "-",
            ],
            input="\n".join(lote_tmp),
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            for p in lote_tmp:
                if os.path.exists(p):
                    os.remove(p)
            raise RuntimeError(f"exiftool falló limpiando lote RGB: {r.stderr[:300]}")
        for tmp_p, f in zip(lote_tmp, lote_nombres):
            os.replace(tmp_p, os.path.join(IMAGES_DIR, f))

    try:
        with ThreadPoolExecutor(max_workers=NPROCS) as pool:
            list(pool.map(_copiar, pendientes))

        print(f"🏷️  Limpiando EXIF/XMP del dron en {len(pendientes)} imágenes nuevas …")
        lotes = [pendientes[i : i + LOTE] for i in range(0, len(pendientes), LOTE)]
        with ThreadPoolExecutor(max_workers=NPROCS) as pool:
            list(pool.map(_limpiar_y_promover, lotes))
    finally:
        for f in pendientes:
            tmp_p = _tmp_path(f)
            if os.path.exists(tmp_p):
                os.remove(tmp_p)

    print(f"✅ {len(pendientes)} imágenes RGB preparadas" + (f", {ya} reutilizadas" if ya else ""))
    _gestionar_geo_txt(IMAGES_DIR, nombres)


if __name__ == "__main__":
    main()
