#!/usr/bin/env python3
"""Filtrado único de subdirectorios no cartográficos para escaneo de fuentes.

Evita mezclar vuelos cartográficos (RTK FIX) con subcarpetas auxiliares
(`audiovisual`, `panoramicas`, `rgb_360`, `videos`, `calib`) que suelen traer
tomas oblicuas/SINGLE sin RTK y desplazan la reconstrucción si ODM las junta en
el mismo bloque (caso real Barbosa: +4.53 m de offset vertical).

Regla clave para `webapp/main.py::_link_one_folder()`: la raíz elegida y los
symlinks de profundidad 1 dentro de `raw/<kind>/` NUNCA se podan por nombre (un
usuario puede importar una carpeta cuyo nombre contenga esos términos); solo se
podan subcarpetas internas debajo de cada fuente real. Con
`RAPTOR_INCLUDE_ALL_DIRS=1` se desactiva toda poda.
"""

import os
import sys

EXCLUDED_EXACT = {"videos", "video", "calib", "360", "rgb_360"}
RGB_PREFERRED_SUFFIXES = ("_V.JPG", "_W.JPG")
RGB_FALLBACK_SUFFIXES = (".JPG", ".JPEG", ".PNG", ".TIF", ".TIFF")
MS_BAND_SUFFIXES = ("_MS_G.TIF", "_MS_R.TIF", "_MS_RE.TIF", "_MS_NIR.TIF")


def es_subdir_excluido(dirname):
    """True si el nombre de subcarpeta corresponde a material no cartográfico."""
    if os.environ.get("RAPTOR_INCLUDE_ALL_DIRS") == "1":
        return False
    low = dirname.strip().lower()
    if not low:
        return False
    if "audiovisual" in low or "panoramica" in low or "panorama" in low:
        return True
    return low in EXCLUDED_EXACT or low.endswith("_360")


def iter_source_files(src_root):
    """Devuelve rutas de archivos válidos bajo `src_root`, siguiendo symlinks
    sin bucles y podando subdirectorios auxiliares no cartográficos."""
    src_str = str(src_root)
    if not os.path.isdir(src_str):
        return []
    resultados = []
    visitados = set()
    root_real = os.path.realpath(src_str)
    for curr_root, dirs, files in os.walk(src_str, followlinks=True):
        try:
            st = os.stat(curr_root)
            clave = (st.st_dev, st.st_ino)
            if clave in visitados:
                dirs[:] = []
                continue
            visitados.add(clave)
        except OSError:
            dirs[:] = []
            continue

        en_raiz = os.path.realpath(curr_root) == root_real
        filtrados = []
        for d in sorted(dirs):
            ruta_d = os.path.join(curr_root, d)
            # Los symlinks de primer nivel son raíces elegidas por el usuario vía
            # webapp import-local() (p. ej. raw/rgb_thermal/panoramicas_vuelo).
            if en_raiz and os.path.islink(ruta_d):
                filtrados.append(d)
                continue
            if not es_subdir_excluido(d):
                filtrados.append(d)
        dirs[:] = filtrados

        for f in sorted(files):
            resultados.append(os.path.join(curr_root, f))
    return resultados


def find_sensor_files(src_root, mode):
    """Lista rutas ordenadas según el modo de setup (`rgb`, `thermal`, `ms`,
    `ms_nir`, `dband`, `mrk`)."""
    all_files = iter_source_files(src_root)
    if mode == "rgb":
        pref = [p for p in all_files if os.path.basename(p).upper().endswith(RGB_PREFERRED_SUFFIXES)]
        if pref:
            return pref
        return [
            p
            for p in all_files
            if os.path.basename(p).upper().endswith(RGB_FALLBACK_SUFFIXES)
            and not os.path.basename(p).upper().rsplit(".", 1)[0].endswith(("_T", "_D"))
            and "_MS_" not in os.path.basename(p).upper()
        ]
    if mode == "thermal":
        return [p for p in all_files if os.path.basename(p).upper().endswith("_T.JPG")]
    if mode == "ms":
        return [p for p in all_files if os.path.basename(p).upper().endswith(MS_BAND_SUFFIXES)]
    if mode == "ms_nir":
        return [p for p in all_files if os.path.basename(p).upper().endswith("_MS_NIR.TIF")]
    if mode == "dband":
        return [p for p in all_files if os.path.basename(p).upper().endswith("_D.JPG")]
    if mode == "mrk":
        return [p for p in all_files if os.path.basename(p).upper().endswith("TIMESTAMP.MRK")]
    raise ValueError(f"Modo desconocido: {mode}")


def main():
    if len(sys.argv) < 3:
        print("uso: python3 scripts/source_filter.py <src_root> <rgb|thermal|ms|ms_nir|dband|mrk>", file=sys.stderr)
        return 1
    for path in find_sensor_files(sys.argv[1], sys.argv[2]):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
