#!/usr/bin/env python3
"""Submuestreo de fotos de un proyecto ODM para respuesta de emergencia.

Reduce a 1 de cada N las fotos que ODM va a procesar, moviendo las demás a
`images_full/` (a un lado, sin borrar nada) y filtrando `geo.txt` a las que
quedaron. Es la palanca más grande para acortar una corrida en modo
`vistazo`/`rapido` con terreno escarpado: la reconstrucción incremental es
secuencial (~12 s/foto de bundle adjustment, no paralelizable entre fotos),
así que procesar la tercera parte de las fotos recorta ese costo casi a la
tercera parte, y el matching/undistort —también proporcionales al número de
fotos— bajan igual. El ortomosaico a 15 cm sigue cubriendo la zona; pierde
detalle de solape, que es exactamente el trade-off que un "vistazo" de
emergencia ya acepta.

POR QUÉ ES SEGURO TOCAR `images/` DEL PROYECTO:
  - El conjunto completo queda intacto en `data/<sensor>_mosaico/` (la
    organización) y en `runs/<misión>/raw/` (la webapp). `images_full/` es
    solo una copia a un lado para que nada se pierda aunque la preparación
    no se repita.
  - En la SIGUIENTE corrida, `_odm_prep` vuelve a poblar `images/` desde
    `data/` (la preparación siempre regenera el conjunto completo), así que
    submuestrear de nuevo simplemente re-aplica el mismo recorte — no hay
    estado que limpiar. Este script borra cualquier `images_full/` previo
    (los originales viven en data/, no ahí).
  - MS/banda D/térmico usan `geo.txt`: se filtra a las imágenes que quedan
    y el original se guarda en `images_full/geo.txt`.

Uso (desde /app, como el resto del pipeline):
  python3 scripts/subsample_photos.py processing/rgb_odm 3
"""
import os
import sys

USO = "uso: python3 scripts/subsample_photos.py <proyecto_odm> <n>" \
      "  (n>=2; n=1 o menor no hace nada)"

# Mismo set que BAND_SUFFIXES en prepare_multispectral_odm.py — las 4 bandas
# de UNA captura M3M tienen que submuestrear SIEMPRE juntas (ver _capture_key).
MS_BAND_SUFFIXES = ("_MS_G.TIF", "_MS_R.TIF", "_MS_RE.TIF", "_MS_NIR.TIF")


def _capture_key(fname):
    """Clave de agrupación para el submuestreo: el nombre completo, salvo
    para las 4 bandas multiespectrales, a las que se les saca el sufijo de
    banda — así las 4 bandas de una misma captura quedan en el mismo grupo.

    Bug real que esto arregla: `files[::n]` sobre la lista plana (los 4
    archivos de una captura MS quedan alfabéticamente intercalados como
    ..._G, ..._NIR, ..._R, ..._RE, seguidos de la próxima captura) toma 1 de
    cada N ARCHIVOS, no 1 de cada N CAPTURAS — el patrón de selección se
    desfasa entre bandas y termina dejando conteos distintos por banda
    (visto en una corrida real: banda Red con 151 imágenes, NIR con 151,
    de 152 esperadas). ODM arma pares de banda por CAPTURA, no por archivo
    suelto, y con conteos desparejados revienta con "Cannot match bands by
    filename..." antes de reconstruir nada. Agrupando por captura, 1 de
    cada N cae siempre sobre las 4 bandas juntas."""
    for suf in MS_BAND_SUFFIXES:
        if fname.upper().endswith(suf.upper()):
            return fname[:-len(suf)]
    return fname


def main():
    if len(sys.argv) < 3:
        print(USO)
        return 1
    proj, n = sys.argv[1], int(sys.argv[2])
    if n < 2:
        print(f"  n={n} → sin submuestreo")
        return 0
    images = os.path.join(proj, "images")
    if not os.path.isdir(images):
        print(f"❌ ERROR: {images} no existe (¿ya corrió la preparación?)")
        return 1

    # Bug real, visto en una corrida real (M3M): ODM cachea el resultado de
    # escanear images/ en <proyecto>/images.json y, sin --rerun, lo REUSA
    # ciegamente si el archivo existe (stages/dataset.py: `if not
    # file_exists(images_database_file) or self.rerun()`) — sin comparar
    # contra el contenido actual de images/. Un reintento de esta misma
    # misión que cambia CUÁL captura submuestrea (p.ej. porque se corrigió
    # este mismo script) deja ese caché desincronizado: ODM sigue viendo el
    # set de la corrida anterior, con conteos de banda que no coinciden con
    # los archivos reales — "band Red has only 151 images (instead of 152)"
    # y revienta con "Cannot match bands by filename" antes de reconstruir
    # nada. Este script es el único lugar que CAMBIA qué queda en images/
    # (solo se invoca en modo urgencia, ver docker/entrypoint.sh), así que
    # es el lugar correcto para invalidar ese caché — se borra siempre que
    # corre, no solo cuando el N efectivamente recorta algo.
    images_json = os.path.join(proj, "images.json")
    if os.path.isfile(images_json):
        os.remove(images_json)

    full = os.path.join(proj, "images_full")
    os.makedirs(full, exist_ok=True)
    # Limpiar un images_full/ de un submuestreo anterior: los archivos ahí
    # son solo los descartados de la corrida previa — los originales están
    # en data/ y la preparación ya repobló images/ con el conjunto completo.
    for prev in os.listdir(full):
        os.remove(os.path.join(full, prev))

    # Orden alfabético = cronológico en los nombres DJI (DJI_<fecha>_<seq>),
    # así cada línea de vuelo queda espaciada uniformemente: se toma 1 de
    # cada N consecutivas en vez de un muestreo aleatorio que dejaría huecos.
    files = sorted(f for f in os.listdir(images)
                   if os.path.isfile(os.path.join(images, f)))
    if len(files) < n:
        print(f"  ⚠ solo {len(files)} fotos — menos que el factor {n}, sin submuestreo")
        return 0
    # Agrupar por captura (ver _capture_key): 1 de cada N se aplica a
    # CAPTURAS, no a archivos sueltos. Para todo lo que no sea multiespectral
    # (RGB/térmico/banda D: 1 archivo = 1 captura) esto es idéntico a antes.
    groups = {}
    for f in files:
        groups.setdefault(_capture_key(f), []).append(f)
    capturas = sorted(groups)
    if len(capturas) < n:
        print(f"  ⚠ solo {len(capturas)} capturas — menos que el factor {n}, sin submuestreo")
        return 0
    keep = {f for clave in capturas[::n] for f in groups[clave]}
    moved = 0
    for f in files:
        if f not in keep:
            os.replace(os.path.join(images, f), os.path.join(full, f))
            moved += 1

    # geo.txt (térmico nativo, multiespectral, banda D): primera columna =
    # nombre de imagen; se filtra a las que quedaron y el original se guarda.
    # El separador puede ser tab O espacio según quién lo haya escrito
    # (odm_staging.py usa tab; algunos preparadores, espacios) — split()
    # corta por cualquier whitespace y cubre los dos.
    geo = os.path.join(proj, "geo.txt")
    if os.path.isfile(geo):
        with open(geo, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        # La primera línea NO es una imagen — es el header de proyección que
        # escribe escribir_geo_txt() (odm_staging.py), típicamente "EPSG:4326".
        # Filtrarla junto con el resto (bug real: su primer token nunca
        # matchea un nombre de archivo, así que siempre caía) le borra el SRS
        # a geo.txt — ODM entonces lee la primera fila de datos como si fuera
        # el header y revienta con "Bad SRS supplied: DJI_..." en vez de
        # reconstruir. Se preserva sin condición; el filtro por `keep` corre
        # solo sobre las filas de imagen (línea 2 en adelante).
        header, filas = (lines[:1], lines[1:]) if lines else ([], [])
        kept_lines = header + [ln for ln in filas
                                if os.path.basename(ln.split()[0].strip()) in keep]
        os.replace(geo, os.path.join(full, "geo.txt"))
        with open(geo, "w", encoding="utf-8") as fh:
            fh.write("\n".join(kept_lines) + ("\n" if kept_lines else ""))

    with open(os.path.join(proj, ".subsampled_n"), "w", encoding="utf-8") as fh:
        fh.write(str(n))
    print(f"  Submuestreo x{n}: {len(keep)} imágenes en images/ "
          f"({moved} movidas a images_full/)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
