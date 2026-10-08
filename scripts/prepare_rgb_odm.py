#!/usr/bin/env python3
"""
Prepara las imágenes RGB (visible) para ODM: copia desde data/ (fuente
inmutable, ya organizada por docker/setup-data.sh) a processing/rgb_odm/
images/ y limpia el EXIF/XMP propio del dron.

Copia REAL, no hardlink (a diferencia de odm_staging.poblar_images para
multiespectral/banda D): acá se corre exiftool -overwrite_original sobre las
copias, y con un hardlink eso modificaría también el archivo fuente en
data/ — ver la advertencia en scripts/odm_staging.py. Se usa
`cp --reflink=auto`: en un filesystem con copy-on-write (btrfs, XFS,
overlay2 sobre ellos) cuesta prácticamente cero; en el resto cae a una
copia normal sin decir nada.

EXIF: se limpia todo (-all=) salvo -exif:all y -xmp-drone-dji:all. Historia
real de por qué son justo esos dos grupos y ningún otro (venía de un
`prepare-rgb` en el Makefile, movido acá):
  - -xmp-drone-dji:all preserva RtkStd{Lon,Lat,Hgt}: ODM los auto-detecta del
    EXIF (opendm/photo.py) y ajusta el peso GPS del bundle adjustment según
    la precisión RTK real (~1-3cm) en vez de caer a un DOP genérico
    (~metros) — PERO solo si sobreviven en el archivo. -api Compact=Shorthand
    es necesario: sin eso exiftool los reserializa como elementos XML
    anidados en vez de atributos (formato original de DJI), y el parser XMP
    propio de ODM (busca '@drone-dji:RtkStdLon' con prefijo @ = atributo) no
    los reconoce.
  - -exif:all (no solo -gps:all): -gps:all da la posición pero NO
    Make/Model/FocalLength, así que ODM ve una cámara anónima y usa su focal
    por defecto (focal_ratio 0.85) en vez de la real del M3T (0.667) — 27% de
    error inicial. Con esa semilla el bundle adjustment diverge a focal_x=75
    y la reconstrucción COLAPSA de escala: un vuelo de 500×416 m midió una
    ortofoto de 34×23 m, con el recorte por solape (haciendo lo correcto
    sobre una geometría imposible) dejando el mosaico en 2% de cobertura.
    -exif:all es superconjunto de -gps:all, así que preserva la posición
    igual y además la identidad de la cámara. Verificado con
    opendm.photo.ODM_Photo sobre las 3 variantes: make/model/focal_ratio
    vuelven a los valores de la foto cruda y gps_xy_stddev sigue en 0.0235 m.

Se saltea los archivos que ya estén en el destino: son fotos ya capturadas
(no cambian entre corridas), así que si el destino ya existe es porque una
corrida anterior de esta misma misión ya lo copió y limpió — un retry no
debería volver a copiar + re-exiftoolear miles de imágenes que no cambiaron.

Uso:
  python3 scripts/prepare_rgb_odm.py [data_rgb] [images_dir]
"""
import os
import sys
import subprocess
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hardware import safe_concurrency  # noqa: E402

# Mismo perfil que usaba el Makefile por defecto (concurrency 2) — copiar +
# limpiar EXIF es liviano por archivo (E/S, no decodificar píxeles), así que
# no hace falta el perfil de 5 MP que sí protege al band alignment de MS.
_override = os.environ.get("MAX_CONCURRENCY")
NPROCS = safe_concurrency(2, override=int(_override) if _override else None)

DATA_RGB = sys.argv[1] if len(sys.argv) > 1 else "data/rgb_mosaico"
IMAGES_DIR = sys.argv[2] if len(sys.argv) > 2 else "processing/rgb_odm/images"
SUFIJOS = ("_V.JPG", "_W.JPG")
LOTE = 100  # mismo tamaño de lote que usaba `xargs -n 100` en el Makefile


def main():
    if not os.path.isdir(DATA_RGB):
        print(f"❌ ERROR: Directorio fuente no encontrado: {DATA_RGB}")
        sys.exit(1)
    os.makedirs(IMAGES_DIR, exist_ok=True)

    nombres = sorted(f for f in os.listdir(DATA_RGB) if f.upper().endswith(SUFIJOS))
    if not nombres:
        nombres = sorted(
            f for f in os.listdir(DATA_RGB)
            if f.upper().endswith((".JPG", ".JPEG", ".PNG", ".TIF", ".TIFF"))
            and not f.upper().endswith(("_T.JPG", "_T.JPEG", "_D.JPG", "_D.JPEG"))
            and "_MS_" not in f.upper()
        )
    if not nombres:
        print(f"❌ No se encontraron imágenes RGB en {DATA_RGB}")
        sys.exit(1)

    nombres_set = set(nombres)
    viejos = [f for f in os.listdir(IMAGES_DIR)
              if f.upper().endswith((".JPG", ".JPEG", ".PNG", ".TIF", ".TIFF")) and f not in nombres_set]
    if viejos:
        print(f"🧹 Limpiando {len(viejos)} de otra misión/corrida …")
        for f in viejos:
            os.remove(os.path.join(IMAGES_DIR, f))

    pendientes = [f for f in nombres if not os.path.exists(os.path.join(IMAGES_DIR, f))]
    ya = len(nombres) - len(pendientes)
    if not pendientes:
        print(f"✅ {ya} imágenes RGB ya estaban preparadas, nada que copiar")
        return

    print(f"📋 Copiando {len(pendientes)} imágenes RGB nuevas con {NPROCS} en paralelo"
          + (f" ({ya} ya existentes, reutilizadas) …" if ya else " …"))

    def _copiar(f):
        subprocess.run(
            ["cp", "--reflink=auto", os.path.join(DATA_RGB, f), os.path.join(IMAGES_DIR, f)],
            check=True,
        )

    with ThreadPoolExecutor(max_workers=NPROCS) as pool:
        list(pool.map(_copiar, pendientes))

    print(f"🏷️  Limpiando EXIF/XMP del dron en {len(pendientes)} imágenes nuevas …")
    dst_paths = [os.path.join(IMAGES_DIR, f) for f in pendientes]
    lotes = [dst_paths[i:i + LOTE] for i in range(0, len(dst_paths), LOTE)]

    def _limpiar_lote(lote):
        # -tagsfromfile @ con -all= antes: no copia de OTRO archivo, es el
        # truco de exiftool para "quedarme solo con estos grupos de tags de
        # MÍ MISMO" (descarta MakerNotes y demás ruido propio del dron).
        subprocess.run(
            ["exiftool", "-api", "Compact=Shorthand", "-all=",
             "-tagsfromfile", "@", "-exif:all", "-xmp-drone-dji:all",
             "-overwrite_original", "-q", "-@", "-"],
            input="\n".join(lote), capture_output=True, text=True,
        )

    with ThreadPoolExecutor(max_workers=NPROCS) as pool:
        list(pool.map(_limpiar_lote, lotes))

    print(f"✅ {len(pendientes)} imágenes RGB preparadas"
          + (f", {ya} reutilizadas" if ya else ""))


if __name__ == "__main__":
    main()
