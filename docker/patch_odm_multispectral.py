#!/usr/bin/env python3
"""Parche de build: corrige un bug real de OpenDroneMap/ODM (upstream, no de
este repo) en stages/run_opensfm.py — reportado en vivo en una misión real de
incendio (M3M/multiespectral): alignment_info solo se calculaba adentro del
guard de resumibilidad (`if not io.file_exists(added_shots_file) or
self.rerun()`), pero align_to_primary_band() (que la necesita) se registra
en undistort_pipeline de forma incondicional. En CUALQUIER pasada resumida
por esta etapa (added_shots_done.txt ya presente y self.rerun()==False) —
notablemente la SEGUNDA invocación del propio run_odm() de este repo
(--end-with opensfm liviano, después --rerun-from openmvs pesado, ver
docker/entrypoint.sh) — alignment_info quedaba None y explotaba con:
  AttributeError: 'NoneType' object has no attribute 'get'
100% reproducible, en TODA misión multiespectral, no solo en reintentos.

PR enviado upstream (OpenDroneMap/ODM) con este mismo fix; esto es el parche
local mientras no esté mergeado — se aplica en cada build de la imagen
porque vive en la capa base (opendronemap/odm:gpu), no en este repo.

Transformación LÍNEA A LÍNEA (no un bloque de texto exacto): el archivo
fuente tiene espacios al final de varias líneas en blanco dentro del bloque,
y un match de un literal completo es frágil ante ese tipo de diferencia
invisible. Se ubica el bloque por sus dos líneas ancla (cortas, sin
indentación variable) y se reconstruye entre ellas.

Se corre una sola vez durante `docker build` (ver Dockerfile). Falla el
build si las líneas ancla no aparecen (la base cambió el código de forma
inesperada) — mejor un build roto y visible que una imagen con multiespectral
silenciosamente sin arreglar.
"""

PATH = "/code/stages/run_opensfm.py"

ANCLA_INICIO = "            added_shots_file = octx.path('added_shots_done.txt')\n"
ANCLA_FIN = "            undistort_pipeline.append(align_to_primary_band)\n"

BLOQUE_NUEVO = """            added_shots_file = octx.path('added_shots_done.txt')

            # RAPTOR PATCH (upstream bug, PR pendiente en OpenDroneMap/ODM):
            # alignment_info se usa incondicionalmente más abajo vía
            # align_to_primary_band en undistort_pipeline, pero antes solo se
            # calculaba adentro del guard de resumibilidad. En cualquier
            # pasada resumida (added_shots_file ya presente, self.rerun()
            # False — incluida la segunda invocación de run_odm() de este
            # repo, --rerun-from openmvs) quedaba None y explotaba con
            # AttributeError: 'NoneType' object has no attribute 'get'.
            # Se calcula siempre; solo la duplicación de shots sigue
            # detrás del guard (repetirla sí duplicaría shots).
            s2p, p2s = multispectral.compute_band_maps(reconstruction.multi_camera, primary_band_name)

            if not args.skip_band_alignment:
                alignment_info = multispectral.compute_alignment_matrices(reconstruction.multi_camera, primary_band_name, tree.dataset_raw, s2p, p2s, max_concurrency=args.max_concurrency)
            else:
                log.ODM_WARNING("Skipping band alignment")
                alignment_info = {}

            if not io.file_exists(added_shots_file) or self.rerun():
                log.ODM_INFO("Adding shots to reconstruction")

                octx.backup_reconstruction()
                octx.add_shots_to_reconstruction(p2s)
                octx.touch(added_shots_file)

            undistort_pipeline.append(align_to_primary_band)
"""

MARKER = "RAPTOR PATCH (upstream bug, PR pendiente en OpenDroneMap/ODM)"


def main():
    with open(PATH) as f:
        content = f.read()

    if MARKER in content:
        print(f"  ✅ {PATH} ya tiene el parche (build repetido) — nada que hacer")
        return

    if ANCLA_INICIO not in content or ANCLA_FIN not in content:
        print(f"  ⚠ {PATH} no tiene las líneas ancla esperadas — la imagen base "
              f"cambió este archivo (¿ya lo arreglaron upstream?). Se sigue "
              f"SIN aplicar el parche local; revisar a mano si multiespectral "
              f"vuelve a fallar con 'NoneType' object has no attribute 'get' "
              f"en align_to_primary_band.")
        return

    ini = content.index(ANCLA_INICIO)
    fin = content.index(ANCLA_FIN, ini) + len(ANCLA_FIN)
    content = content[:ini] + BLOQUE_NUEVO + content[fin:]
    with open(PATH, "w") as f:
        f.write(content)
    print(f"  ✅ parche aplicado a {PATH}")


if __name__ == "__main__":
    main()
