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

Uso: <comando ODM> 2>&1 | python3 odm_progress_filter.py "<label>" <logfile> [resume_from]

resume_from (opcional): nombre de la etapa ODM (dataset, opensfm, ...) desde
la que esta invocación RETOMA de verdad — ver run_odm() en
docker/entrypoint.sh, que corre cada sensor en DOS invocaciones de run.py
para poder darle más hilos a TODO el SfM sin tocar la concurrencia de MVS
(fase 1 `--end-with opensfm`, fase 2 `--rerun-from openmvs`). ODM siempre
arranca su cadena de etapas desde "dataset" (stages/odm_app.py:
self.first_stage = dataset, sin importar --rerun-from) y cada
ODM_Stage.run() imprime "Running <etapa> stage" INCONDICIONALMENTE antes de
decidir puertas adentro si hace el trabajo de verdad o lo saltea
(stages/dataset.py comprueba si images.json ya existe) — así que la segunda
invocación reimprime "Cargando dataset" aunque ODM lo salte en <1s, dando la
falsa impresión de que está reprocesando imágenes. Con resume_from se
suprimen los headers/barras de las etapas anteriores a esa (ODM las corre
igual puertas adentro, pero son no-ops reales, no hay nada que mostrar).

Además de filtrar, este script CRONOMETRA las sub-etapas de OpenSfM
(features / matching / SfM incremental / undistort) y las deja en
outputs/logs/odm_<label>_substages.json. Sin eso, "ODM multiespectral: 8.5 h"
es un número sin accionar: hubo que reconstruir a mano, con timestamps de un
log de MB, que las 10.4 h de la banda D eran 108 min de features + 56 de
matching + 267 de SfM incremental + 107 de undistort — y que features y
undistort estaban corriendo con 2 hilos sobre 20 núcleos. Se mide con el
reloj de pared del propio filtro (las líneas llegan en streaming), no
parseando los timestamps del texto: no todas las líneas de ODM traen uno.

El exit code de ODM (no el de este filtro) hay que propagarlo con
PIPESTATUS en el bash que invoca esto.
"""
import json, sys, os, re, time

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

# Sub-etapas de OpenSfM que consumen el grueso del SfM, con el texto exacto
# que cada una imprime POR IMAGEN. Se mide el lapso entre la primera y la
# última línea de cada una.
#
# LÍMITE IMPORTANTE, y por eso además se cronometran las ETAPAS de ODM
# (abajo): esto solo sirve para lo que imprime una línea terminada en \n por
# ítem. Las etapas que reportan progreso reescribiendo la misma línea con \r
# —OpenMVS es la principal— llegan acá como UNA sola línea gigante al final,
# así que su lapso medido colapsa a ~0. En la corrida de verificación eso se
# vio claro: las sub-etapas sumaban 6:40 de los 34:11 que tardó ODM.
SUBSTAGES = (
    ("features",  "Extracting ROOT_"),      # ROOT_DSPSIFT / ROOT_SIFT según config
    ("matching",  "Matching "),
    ("sfm",       "Adding "),               # "Adding <img> to the reconstruction"
    ("undistort", "Undistorting image"),
)
# Cada cuántas líneas se vacía el buffer del log al disco. Antes era CADA
# línea: sobre los millones de líneas de DEBUG que escupe OpenSfM en una
# corrida de horas eso es un write(2) por línea sin ninguna necesidad — si
# ODM muere, el filtro igual recibe EOF y cierra el archivo (que lo vacía),
# así que no se pierde el traceback que entrypoint.sh vuelca después.
FLUSH_CADA = 200


def _escribir_substages(logfile, label, marcas, etapas=None):
    """Deja los tiempos junto al log, para print_timings.py.

    `etapas` son las etapas de ODM cronometradas de "Running X stage" a
    "Finished X stage": a diferencia de las sub-etapas, cubren el 100% del
    tiempo de la reconstrucción y no dependen de que la etapa imprima una
    línea por ítem. Son la medida principal; las sub-etapas son el detalle
    de adentro del SfM.
    """
    if not marcas and not etapas:
        return
    destino = os.path.join(os.path.dirname(logfile) or ".",
                           f"odm_{label}_substages.json")
    datos = {}
    if os.path.exists(destino):
        # Las DOS fases de run_odm() escriben acá (SfM en la primera, MVS en
        # la segunda): se fusiona en vez de pisar, o el resumen final se
        # quedaría solo con lo que midió la última invocación.
        try:
            with open(destino) as f:
                datos = json.load(f)
        except (OSError, ValueError):
            datos = {}

    if etapas:
        acum = datos.setdefault("etapas", {})
        for nombre, segundos in etapas.items():
            acum[nombre] = round(acum.get(nombre, 0.0) + segundos, 1)

    for nombre, (t0, t1, n) in marcas.items():
        previo = datos.get(nombre)
        if previo:
            datos[nombre] = {"segundos": round(previo["segundos"] + (t1 - t0), 1),
                             "n": previo["n"] + n}
        else:
            datos[nombre] = {"segundos": round(t1 - t0, 1), "n": n}
    try:
        with open(destino, "w") as f:
            json.dump(datos, f, indent=2)
    except OSError:
        pass


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "ODM"
    logfile = sys.argv[2] if len(sys.argv) > 2 else "/tmp/odm_progress.log"
    resume_from = sys.argv[3] if len(sys.argv) > 3 else None
    resume_idx = STAGE_ORDER.index(resume_from) if resume_from in STAGE_ORDER else 0
    verbose = os.environ.get("VERBOSE", "0") == "1"
    os.makedirs(os.path.dirname(logfile) or ".", exist_ok=True)

    current_stage = None
    marcas = {}          # sub-etapa -> [t_primera, t_última, nº de líneas]
    etapas = {}          # etapa de ODM -> segundos acumulados
    etapa_abierta = None  # (nombre, t_inicio)
    desde_flush = 0

    # `label` viene como "ODM RGB"/"ODM MULTISPECTRAL"; para el nombre del
    # JSON se usa el mismo sufijo que el logfile, que ya es el id corto.
    label_corto = os.path.basename(logfile).removeprefix("odm_").removesuffix(".log")

    with open(logfile, "a") as log:
        for line in sys.stdin:
            log.write(line)
            desde_flush += 1
            if desde_flush >= FLUSH_CADA:
                log.flush()
                desde_flush = 0

            for nombre, patron in SUBSTAGES:
                if patron in line:
                    ahora = time.time()
                    marca = marcas.get(nombre)
                    if marca is None:
                        marcas[nombre] = [ahora, ahora, 1]
                    else:
                        marca[1] = ahora
                        marca[2] += 1
                    break

            # Cronómetro por ETAPA de ODM. Va acá arriba, ANTES del `continue`
            # de VERBOSE, para que medir no dependa de cómo se esté mostrando
            # la salida. Estas son las que cubren el 100% del tiempo de la
            # reconstrucción: las sub-etapas de arriba solo ven lo que imprime
            # una línea por ítem.
            _m = RUNNING_RE.search(line)
            if _m:
                etapa_abierta = (_m.group(1), time.time())
            else:
                _m = FINISHED_RE.search(line)
                if _m and etapa_abierta and etapa_abierta[0] == _m.group(1):
                    nombre_etapa, t0 = etapa_abierta
                    etapas[nombre_etapa] = etapas.get(nombre_etapa, 0.0) + (time.time() - t0)
                    etapa_abierta = None

            if verbose:
                sys.stdout.write(line)
                sys.stdout.flush()
                continue

            m = RUNNING_RE.search(line)
            if m:
                name = m.group(1)
                current_stage = name
                if name in STAGE_ORDER and STAGE_ORDER.index(name) < resume_idx:
                    continue  # no-op real (ver el porqué en el docstring)
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
                if name in STAGE_ORDER and STAGE_ORDER.index(name) < resume_idx:
                    continue
                print_done(stage_info=f"{label}: {STAGE_NAMES.get(name, name)}")
                continue

    _escribir_substages(logfile, label_corto,
                        {k: tuple(v) for k, v in marcas.items()},
                        etapas=etapas)


if __name__ == "__main__":
    main()
