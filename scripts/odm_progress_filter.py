#!/usr/bin/env python3
"""Filtro de progreso para la salida nativa de ODM (python3 /code/run.py).

ODM es MUY verboso (miles de líneas: detección de features por imagen,
undistort por imagen, salida cruda de PoissonRecon/ReconstructMesh, etc.).
Por defecto (VERBOSE != "1") este filtro oculta todo eso y muestra solo una
barra de progreso por etapa, con el mismo estilo que el resto del pipeline
(scripts/progress.py) — ODM ya nombra sus etapas de forma consistente
("Running <etapa> stage" / "Finished <etapa> stage", ver stages/odm_app.py)
y durante la reconstrucción densa (openmvs) imprime además un % con ETA
("Estimated depth-maps N (XX.XX%, ETA Xm)...") que se usa para una barra
en vivo en vez de solo un header estático.

El log CRUDO completo siempre se escribe en el archivo pasado como 2do
argumento — si ODM falla, quien invoca este filtro (docker/entrypoint.sh)
vuelca ese archivo entero, así que ningún traceback se pierde.

Uso: <comando ODM> 2>&1 | python3 odm_progress_filter.py "<label>" <logfile>
El exit code de ODM (no el de este filtro) hay que propagarlo con
PIPESTATUS en el bash que invoca esto.
"""
import sys, os, re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from progress import print_stage_header, print_progress, print_done

# Nombres amigables + orden real de stages/odm_app.py (split/merge casi
# siempre son no-op salvo datasets enormes con --split).
STAGE_NAMES = {
    "dataset": "Cargando dataset",
    "split": "División en sub-modelos",
    "merge": "Fusión de sub-modelos",
    "opensfm": "SfM (features + matching + bundle adjustment)",
    "openmvs": "Nube de puntos densa (MVS)",
    "odm_filterpoints": "Filtrado de la nube de puntos",
    "odm_meshing": "Generación de malla 3D",
    "mvs_texturing": "Texturizado de la malla",
    "odm_georeferencing": "Georreferenciación",
    "odm_dem": "Generación del DSM",
    "odm_orthophoto": "Renderizado de la ortofoto",
    "odm_report": "Reporte",
    "odm_postprocess": "Post-procesamiento",
}
STAGE_ORDER = list(STAGE_NAMES.keys())
TOTAL_STAGES = len(STAGE_ORDER)

RUNNING_RE = re.compile(r"Running (\w+) stage")
FINISHED_RE = re.compile(r"Finished (\w+) stage")
DEPTHMAP_RE = re.compile(r"Estimated depth-maps \d+ \(([\d.]+)%.*?ETA\s+([^)]*)\)")


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "ODM"
    logfile = sys.argv[2] if len(sys.argv) > 2 else "/tmp/odm_progress.log"
    verbose = os.environ.get("VERBOSE", "0") == "1"
    os.makedirs(os.path.dirname(logfile) or ".", exist_ok=True)

    current_stage = None

    with open(logfile, "a") as log:
        for line in sys.stdin:
            log.write(line)
            log.flush()

            if verbose:
                sys.stdout.write(line)
                sys.stdout.flush()
                continue

            m = RUNNING_RE.search(line)
            if m:
                name = m.group(1)
                current_stage = name
                stage_n = STAGE_ORDER.index(name) + 1 if name in STAGE_ORDER else 1
                disp = STAGE_NAMES.get(name, name)
                print_stage_header(f"{label}: {disp}", stage_n, TOTAL_STAGES)
                continue

            m = DEPTHMAP_RE.search(line)
            if m:
                pct = float(m.group(1))
                eta = m.group(2).strip()
                print_progress(int(pct), 100, label=f"ETA {eta}",
                                stage_info=f"{label}: {STAGE_NAMES.get(current_stage, current_stage or '')}")
                continue

            m = FINISHED_RE.search(line)
            if m:
                name = m.group(1)
                print_done(stage_info=f"{label}: {STAGE_NAMES.get(name, name)}")
                continue


if __name__ == "__main__":
    main()
