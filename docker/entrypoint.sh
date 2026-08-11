#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
# Entrypoint del contenedor ÚNICO de producción (imagen raptor,
# construida FROM opendronemap/odm:gpu — ODM y el post-procesamiento
# propio viven en el mismo filesystem, sin docker-in-docker).
#
# Uso por DEFECTO (webapp interactiva — subir fotos, elegir parámetros,
# ver progreso en vivo y el geovisor al terminar, todo por navegador):
#   docker run --rm --gpus all -p 8080:8080 raptor:latest
#   → abrir http://localhost:8080
#
# Uso CLI/batch (scripts/automatización, sin interacción — agregar el
# argumento `run` al final es OBLIGATORIO desde que `webapp` es el default):
#   docker run --rm --gpus all \
#     -v /ruta/a/la/mision:/input \
#     -v /ruta/de/salida:/app/outputs \
#     -v /ruta/de/tiles:/app/geovisor/tiles \
#     -e MODE=rgb+thermal \
#     -p 8080:8080 \
#     raptor:latest run
#
# Otros modos: `serve` (solo levantar el geovisor sobre processing/outputs
# ya existentes), `shell` (debug).
#
# Multiespectral (DJI M3M u otro dron distinto, misma zona): montar un
# SEGUNDO volumen aparte de /input (es otro vuelo/sensor, no se mezcla) —
#   -v /ruta/vuelo-multiespectral:/input_ms
# Si /input_ms no está montado, este módulo ni se toca (cero impacto en
# misiones RGB+térmico existentes).
#
# Térmico: se procesa con el renderizador NATIVO de ODM (malla 3D +
# textura + ortofoto real), no un blending heurístico propio — ver
# scripts/prepare_thermal_native_odm.py.
#
# GPU: pasá `--gpus all` SIEMPRE que la máquina lo permita — es la única
# forma de que la etapa densa (cuando corre) sea rápida. Sin el runtime de
# NVIDIA montado, el binario de reconstrucción densa ni siquiera carga —
#   DensifyPointCloud: error while loading shared libraries: libcuda.so.1
# — y la corrida muere con "Child returned 127" en la etapa openmvs, o sea
# DESPUÉS de haber pagado todo el SfM. Comprobado en vivo.
#
# Que ODM "detecta nvidia-smi y cae a CPU" es cierto para elegir el ALGORITMO
# (usa la ruta CPU de OpenMVS), pero no evita el enlace dinámico contra
# libcuda del propio ejecutable. `--gpus all` en una máquina sin GPU es
# inofensivo si el runtime está instalado; si no lo está, el chequeo de más
# abajo avisa ANTES de empezar en vez de reventar horas después — PERO solo
# para los sensores que de verdad van a tocar la etapa densa: RGB en
# vistazo/rápido (--fast-orthophoto) y banda D (que siempre lo usa) no la
# necesitan, así que una corrida donde esos sean los únicos sensores activos
# no exige CUDA aunque falte.
#
# Variables de entorno:
#   MODE            rgb | rgb+thermal | none  (default: rgb+thermal)
#                    `none` = esta misión NO tiene vuelo RGB/térmico (solo
#                    multiespectral M3M) — requiere MS_SOURCE_DIR.
#   SOURCE_DIR       carpeta fuente de imágenes (default: /input)
#   MS_SOURCE_DIR    carpeta fuente multiespectral (default: /input_ms;
#                    el módulo corre SOLO si este directorio existe)
#   SKIP_ODM        1 = saltar SfM/MVS (reusar processing/ ya existente)
#   PORT            puerto del geovisor         (default: 8080)
#   SERVE           1 = levantar el geovisor al terminar (default: 1)
#   VERBOSE         1 = mostrar TODA la salida cruda (ODM + scripts), sin
#                       filtrar. Por defecto (0) solo se ve una barra de
#                       progreso por etapa; el log completo de cada una
#                       queda en outputs/logs/ y se vuelca entero si falla.
#   MAX_CONCURRENCY fuerza el nº de hilos de ODM (por defecto se calcula
#                    según la RAM disponible, ver safe_concurrency()). Bajalo
#                    si el proceso muere sin mensaje por falta de memoria.
#   PRESET          vistazo | rapido | estandar (default) | alta | maxima.
#                    Elige POR USO Y TIEMPO, no por un número abstracto:
#                      vistazo   ver algo utilizable en minutos, en emergencia
#                      rapido    respuesta operativa el mismo día
#                      estandar  la entrega normal de una misión
#                      alta      análisis fino y medición sobre el DSM
#                      maxima    archivo y peritaje
#                    Cada preset fija el detalle del modelo de superficie (qué
#                    tan bien se resuelven copas de árboles y bordes), el techo
#                    de resolución del ortomosaico/DSM (nunca más fino que el
#                    GSD real del vuelo), cuántos features se extraen por foto
#                    y qué algoritmo de SfM se usa. Tabla completa en
#                    scripts/hardware.py; `python3 scripts/hardware.py estimate
#                    --photos N` muestra los cinco con su tiempo estimado para
#                    ese número de fotos, sin arrancar nada.
#   QUALITY         (heredado) entero 0-100. Se mapea al preset equivalente
#                    para no romper corridas y scripts que ya lo pasan.
#
# Exportación de la entrega (opcional — sin EXPORT_DIR no se exporta nada):
#   EXPORT_DIR            carpeta destino de los productos finales
#   EXPORT_PRODUCTS       qué exportar, claves separadas por coma; "all" = todo
#                          (rgb, thermal, dsm, multispectral, indices, classes,
#                           confidence, area, flight_path, situation, pointclouds)
#   EXPORT_RASTER_FORMAT  cog (default) | gtiff
#   EXPORT_VECTOR_FORMAT  geojson (default) | gpkg | shp | kml
#   EXPORT_EPSG           EPSG destino (9377 = MAGNA-SIRGAS / Origen-Nacional,
#                          el sistema único nacional de Colombia) o "source"
#                          para entregar en la UTM que eligió ODM.
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail
cd /app

# Tiempos por etapa: registrados desde acá, impresos + escritos a JSON al
# final (ver el resumen antes de "Pipeline completo" más abajo) — sin esto,
# saber dónde se va el tiempo real de una corrida significaba reconstruirlo
# a mano con `ps`/mtimes de archivos, como hubo que hacer para encontrar el
# cuello de botella de esta misma sesión (35 min de preparación antes de que
# ODM arrancara siquiera).
T_MISSION_START=$(date +%s)

MODE="${MODE:-rgb+thermal}"
SOURCE_DIR="${SOURCE_DIR:-/input}"
MS_SOURCE_DIR="${MS_SOURCE_DIR:-/input_ms}"
# Banda D del M3M (cámara RGB, mosaico visible rápido) — opt-in explícito,
# no automático solo porque haya archivos *_D.JPG: es procesamiento extra
# (su propio ODM) que no todas las misiones quieren pagar.
DBAND="${DBAND:-0}"
SKIP_ODM="${SKIP_ODM:-0}"
PORT="${PORT:-8080}"
SERVE="${SERVE:-1}"
VERBOSE="${VERBOSE:-0}"
# PRESET es la forma actual de pedir calidad (vistazo / rapido / estandar /
# alta / maxima — ver PRESETS en scripts/hardware.py). QUALITY 0-100 era la
# anterior: un número que no le decía a nadie cuánto iba a tardar ni para qué
# servía, y donde 71 y 89 daban exactamente lo mismo. Se sigue aceptando
# porque hay corridas guardadas, scripts y CI que lo pasan; se mapea al
# preset equivalente.
PRESET="${PRESET:-${QUALITY:-estandar}}"
# TERRENO: "plano" (default) o "escarpado" — ver TERRENOS en
# scripts/hardware.py. "escarpado" fuerza sfm_algorithm=incremental incluso
# en vistazo/rápido (que por defecto usan planar): en terreno con relieve
# fuerte, la reconstrucción planar puede descartar la mayoría de las fotos
# en silencio — confirmado en vivo, ver el comentario de TERRENOS.
TERRENO="${TERRENO:-plano}"
export MODE VERBOSE

# ── Calidad (0-100) ─────────────────────────────────────────────────
# Controla el DETALLE DEL MODELO DE SUPERFICIE (pc-quality/feature-quality) —
# lo que decide si una copa de árbol se resuelve en la malla o la superficie
# sale lisa y el árbol se desplaza al proyectarlo (la causa real de "no
# parece true-ortho") — y también el TECHO de resolución que se le pide al
# ortomosaico y al DSM. Ninguno de los dos puede superar el GSD real del
# vuelo: ODM lo mide de la reconstrucción y recorta cualquier pedido más fino
# (opendm/gsd.py::cap_resolution) — pedir 1cm a un vuelo cuyo GSD real es 9cm
# no da más detalle, solo píxeles más chicos. SÍ puede pedirse un techo más
# GRUESO que el GSD real a propósito: reduce el total de píxeles del ráster
# final, y eso acelera de verdad el renderizado, el recorte de bordes, los
# tiles y la exportación COG — todos proporcionales al tamaño del ráster.
#
# La tabla vive en un solo lugar: scripts/hardware.py (fuente única — la
# webapp y el CLI leen la misma, así el mensaje que se le muestra al usuario
# ANTES de arrancar corresponde exactamente a lo que corre después).
# Todo el perfil en UNA sola llamada (`--shell` devuelve una línea por campo,
# en orden fijo). Antes eran seis invocaciones de python3 seguidas, una por
# campo, solo para poder arrancar.
#
# MATCHER_NEIGHBORS: 0 = grafo completo (cada foto contra TODAS las demás) —
# estuvo hardcodeado sin importar la calidad elegida, así que hasta el preset
# más rápido pagaba el matching más caro posible en vuelos de cientos de fotos.
# MIN_FEATURES: cuántos features por foto. También estuvo hardcodeado (12000,
# 8000 en térmico) pese a ser una de las dos sub-etapas más caras del SfM.
# SFM_ALGORITHM: `planar` en los presets rápidos — asume vuelo nadir a altura
# fija y ataca la reconstrucción incremental, que es secuencial por diseño y
# se llevó 4 h 27 min de las 10 h de la banda D de barbosa-picodegallo.
# HYBRID_BA: bundle adjustment local por foto agregada, global completo cada
# 100. Sin esto el costo de cada foto crece con la reconstrucción ya armada.
# FAST_ORTHOPHOTO_RGB: en vistazo/rápido, el RGB salta DensifyPointCloud (MVS)
# igual que banda D — confirmado en vivo, esa sola etapa se llevó ~11 de
# ~15h en una reconstrucción de 1199 fotos con GPU de laptop. Ver _odm_args.
if ! _PRESET_TXT=$(python3 scripts/hardware.py preset "$PRESET" --terreno "$TERRENO" --shell 2>&1); then
  echo "$_PRESET_TXT"; exit 1
fi
{
  read -r PRESET_NOMBRE
  read -r PRESET_TITULO
  read -r PC_QUALITY
  read -r FEAT_QUALITY
  read -r ODM_RES_CM
  read -r MIN_FEATURES
  read -r MATCHER_NEIGHBORS
  read -r SFM_ALGORITHM
  read -r HYBRID_BA
  read -r FAST_ORTHOPHOTO_RGB
} <<< "$_PRESET_TXT"
HYBRID_BA_FLAG=()
[[ "$HYBRID_BA" == "1" ]] && HYBRID_BA_FLAG=(--use-hybrid-bundle-adjustment)

# Resumen JSON también cuando la corrida FALLA: para CI, "en qué etapa murió y
# qué alcanzó a producir" vale tanto como el código de salida. Solo se arma en
# modo `run` (los demás modos ni siquiera llegan hasta acá).
resumen_al_salir() {
  local codigo=$?
  if [[ "${RAPTOR_EN_RUN:-0}" -eq 1 && $codigo -ne 0 ]]; then
    python3 scripts/run_summary.py "$codigo" >/dev/null 2>&1 || true
  fi
  return $codigo
}
trap resumen_al_salir EXIT

if [[ "$MODE" != "rgb" && "$MODE" != "rgb+thermal" && "$MODE" != "thermal" && "$MODE" != "none" ]]; then
  echo "❌ ERROR: MODE debe ser 'rgb', 'rgb+thermal', 'thermal' o 'none' (recibido: $MODE)"; exit 1
fi

RUN_MULTISPECTRAL=0
[[ -d "$MS_SOURCE_DIR" ]] && RUN_MULTISPECTRAL=1

# MODE=none: misión sin vuelo RGB/térmico (el M3M es otro dron, puede volarse
# solo). Sin él tampoco habría nada que procesar, así que se exige el MS.
# MODE=thermal: el vuelo RGB/térmico SÍ está (SOURCE_DIR sigue montado y hace
# falta para organizar las fotos térmicas), pero no se reconstruye el RGB —
# simétrico a MODE=rgb (que sí reconstruye RGB y salta el térmico). RGB y
# térmico son proyectos ODM independientes (processing/rgb_odm vs.
# processing/thermal_native_odm), así que saltarse uno no le hace falta al
# otro para nada — confirmado al agregar el módulo de banda D, que hace lo
# mismo con el M3M.
RUN_RGB=1
[[ "$MODE" == "none" || "$MODE" == "thermal" ]] && RUN_RGB=0
# Corre el vuelo RGB/térmico completo en rgb+thermal Y en thermal — en
# thermal no se reconstruye el RGB, pero SOURCE_DIR sigue siendo la misma
# carpeta del vuelo y hay que organizarla igual para llegar a las fotos
# térmicas.
RUN_THERMAL=0
[[ "$MODE" == "rgb+thermal" || "$MODE" == "thermal" ]] && RUN_THERMAL=1
if [[ "$RUN_RGB" -eq 0 && "$RUN_THERMAL" -eq 0 && "$RUN_MULTISPECTRAL" -eq 0 ]]; then
  echo "❌ ERROR: MODE=none (sin vuelo RGB/térmico) requiere un vuelo multiespectral"
  echo "   en MS_SOURCE_DIR ('$MS_SOURCE_DIR'), pero ese directorio no existe."
  exit 1
fi

case "${1:-run}" in
  serve)
    # Ver una misión ya procesada. Es la misma webapp: sirve el geovisor en
    # /geovisor/ y además trae el HUD de progreso, el muestreo por punto y la
    # edición del polígono de área afectada. Había un servidor aparte para
    # esto (geovisor/serve.py) que reimplementaba una parte de lo mismo.
    export RAPTOR_RUNS_ROOT="${RAPTOR_RUNS_ROOT:-/app/runs}"
    exec python3 -m webapp.main "$PORT"
    ;;
  shell)
    exec bash
    ;;
  webapp)
    # Modo DEFAULT de la imagen (ver Dockerfile CMD): webapp interactiva —
    # subir fotos crudas desde el navegador, elegir modo/parámetros, ver
    # progreso en vivo y el geovisor al terminar, todo en un solo puerto.
    # Orquestador en core/runner.py (activate_mission + PipelineRun +
    # PROGRESS_FILE) — ver webapp/main.py. No requiere SOURCE_DIR montado
    # de antemano: las fotos se suben por HTTP a /app/runs/<misión>/raw/.
    export RAPTOR_RUNS_ROOT="${RAPTOR_RUNS_ROOT:-/app/runs}"
    exec python3 -m webapp.main "$PORT"
    ;;
  run)
    RAPTOR_EN_RUN=1
    ;;
  quality-estimate)
    # Mensaje de "a qué resolución y en cuánto tiempo" SIN arrancar el
    # pipeline — cuenta las fotos montadas con los mismos patrones que
    # docker/setup-data.sh, pero sin copiarlas a data/ (no hace falta para
    # solo contar, y así no interfiere si después se corre `run` de verdad
    # sobre el mismo montaje). Lo usa ./raptor run para mostrar el mensaje
    # antes de lanzar la corrida real.
    N_RGB=0; N_TH=0; N_MS=0
    [[ -d "$SOURCE_DIR" ]] && N_RGB=$(find -L "$SOURCE_DIR" -type f \( -iname "*_V.JPG" -o -iname "*_W.JPG" \) 2>/dev/null | wc -l)
    [[ -d "$SOURCE_DIR" ]] && N_TH=$(find -L "$SOURCE_DIR" -type f -iname "*_T.JPG" 2>/dev/null | wc -l)
    [[ -d "$MS_SOURCE_DIR" ]] && N_MS=$(find -L "$MS_SOURCE_DIR" -type f -iname "*_MS_NIR.TIF" 2>/dev/null | wc -l)
    python3 -c "
import sys
sys.path.insert(0, 'scripts')
from hardware import estimate_message
m = estimate_message('$PRESET', $N_RGB + $N_TH + $N_MS * 4, terreno='$TERRENO')
hw = m['hardware']
print('🖥️  Hardware detectado: {} núcleos, {} — {}'.format(
    hw['cores'],
    f\"{hw['mem_available_mb']/1024:.0f} GB RAM libres\" if hw['mem_available_mb'] else 'RAM no detectada',
    (hw['gpu_name'] or 'GPU disponible') if hw['gpu'] else 'sin GPU (corre en CPU)'))
print('🎚️  ' + m['modelo_texto'])
print('📐 ' + m['resolucion_texto'])
print('⏱️  ' + m['tiempo_texto'])
"
    exit 0
    ;;
  *)
    exec "$@"
    ;;
esac

# ── Chequeo del runtime de CUDA, ANTES de tocar nada ────────────────
# El binario de reconstrucción densa de la imagen base está enlazado contra
# libcuda.so.1. Si el `docker run` externo no pasó `--gpus all` (o el runtime
# de NVIDIA no está instalado en el host), ese enlace no resuelve y la corrida
# muere con "Child returned 127" en la etapa openmvs — DESPUÉS de haber
# pagado la preparación y todo el SfM. Comprobado en vivo: en una misión real
# eso son horas tiradas para terminar en un error que no dice qué pasó.
#
# Se chequea acá, en el primer segundo, y solo cuando esta misión de verdad va
# a correr la etapa densa: banda D usa --fast-orthophoto (no la toca) y con
# SKIP_ODM=1 no hay reconstrucción ninguna. RGB en vistazo/rápido (ver
# FAST_ORTHOPHOTO_RGB más abajo) tampoco la toca — mismo caso que banda D,
# así que sale del disparador igual que ella.
_DENSIFY=/code/SuperBuild/install/bin/DensifyPointCloud
if [[ "$SKIP_ODM" -eq 0 \
      && ( ( "$RUN_RGB" -eq 1 && "$FAST_ORTHOPHOTO_RGB" != "1" ) || "$RUN_THERMAL" -eq 1 || "$RUN_MULTISPECTRAL" -eq 1 ) \
      && -x "$_DENSIFY" ]] && ldd "$_DENSIFY" 2>/dev/null | grep -q "libcuda.so.1 => not found"; then
  echo "❌ ERROR: falta el runtime de CUDA dentro del contenedor."
  echo "   La reconstrucción densa (DensifyPointCloud) está enlazada contra"
  echo "   libcuda.so.1 y no puede ni cargar sin él — la corrida moriría en la"
  echo "   etapa openmvs, después de horas de SfM."
  echo ""
  echo "   Agregá --gpus all al docker run:"
  echo "     docker run --gpus all -v /ruta/mision:/input ... raptor run"
  echo "   (./raptor run ya lo hace salvo que le pases --no-gpu)."
  echo ""
  echo "   Si la máquina NO tiene GPU, hace falta igual el NVIDIA Container"
  echo "   Toolkit instalado en el host; sin eso esta imagen no puede correr"
  echo "   la etapa densa. Alternativa sin GPU: PRESET=vistazo o rapido con RGB"
  echo "   (usa --fast-orthophoto y no toca DensifyPointCloud), o una misión de"
  echo "   banda D."
  exit 1
fi

if [[ ( "$RUN_RGB" -eq 1 || "$RUN_THERMAL" -eq 1 ) && ! -d "$SOURCE_DIR" ]]; then
  echo "❌ ERROR: SOURCE_DIR ('$SOURCE_DIR') no existe. Montá la carpeta de la misión, p.ej.:"
  echo "   docker run -v /ruta/mision:/input ..."
  exit 1
fi

source scripts/progress.sh

echo "═══════════════════════════════════════════════════"
echo "  Pipeline Mosaico — MODE=${MODE}"
[[ "$RUN_RGB" -eq 1 || "$RUN_THERMAL" -eq 1 ]] && echo "  Fuente: ${SOURCE_DIR}"
[[ "$RUN_MULTISPECTRAL" -eq 1 ]] && echo "  Fuente multiespectral: ${MS_SOURCE_DIR}"
echo "═══════════════════════════════════════════════════"

# Organizar RGB+térmico y multiespectral EN PARALELO: leen de carpetas
# fuente distintas (SOURCE_DIR vs MS_SOURCE_DIR) y escriben a carpetas
# destino distintas (data/{rgb,termica}_mosaico vs data/multiespectral_
# mosaico) — no comparten nada, así que no hay razón para que uno espere al
# otro. Antes corrían uno detrás del otro sin necesidad real.
ORG_PIDS=()
if [[ "$RUN_RGB" -eq 1 || "$RUN_THERMAL" -eq 1 ]]; then
  ( echo "--- [0/5] Organizando imágenes desde: ${SOURCE_DIR} ---"
    ./docker/setup-data.sh "$SOURCE_DIR" ) &
  ORG_PIDS+=($!)
fi
if [[ "$RUN_MULTISPECTRAL" -eq 1 ]]; then
  ( echo "--- [0b/5] Organizando bandas multiespectrales desde: ${MS_SOURCE_DIR} ---"
    ./docker/setup-data-multispectral.sh "$MS_SOURCE_DIR" ) &
  ORG_PIDS+=($!)
fi
ORG_FAILED=0
for pid in "${ORG_PIDS[@]}"; do wait "$pid" || ORG_FAILED=1; done
if [[ "$ORG_FAILED" -eq 1 ]]; then
  echo "❌ ERROR: falló la organización de imágenes (RGB/térmico o multiespectral)."
  echo "   Revisá los mensajes de arriba para saber cuál."
  exit 1
fi

if [[ "$RUN_RGB" -eq 1 || "$RUN_THERMAL" -eq 1 ]]; then
  # RGB siempre se exige acá aunque MODE=thermal no lo reconstruya: es la
  # misma carpeta del vuelo M3T/H20T, y su ausencia señala "carpeta
  # equivocada" tanto como en cualquier otro modo (mismo criterio que
  # webapp/main.py::_validate() y el chequeo del lado del navegador).
  RGB_COUNT=$(ls data/rgb_mosaico/*_V.JPG data/rgb_mosaico/*_W.JPG 2>/dev/null | wc -l || true)
  [[ "$RGB_COUNT" -eq 0 ]] && { echo "❌ ERROR: No hay imágenes RGB (*_V.JPG / *_W.JPG) en ${SOURCE_DIR}"; exit 1; }
  echo "   RGB: ${RGB_COUNT} imágenes"

  if [[ "$RUN_THERMAL" -eq 1 ]]; then
    TH_COUNT=$(ls data/termica_mosaico/*_T.JPG 2>/dev/null | wc -l || true)
    if [[ "$TH_COUNT" -eq 0 ]]; then
      echo "❌ ERROR: MODE=${MODE} pero no hay imágenes térmicas (*_T.JPG) en ${SOURCE_DIR}."
      echo "   Agregá las fotos térmicas del vuelo, o corré con MODE=rgb para procesar solo RGB."
      exit 1
    fi
    echo "   Térmico: ${TH_COUNT} imágenes"
  fi
fi

if [[ "$RUN_MULTISPECTRAL" -eq 1 ]]; then
  MS_COUNT=$(ls data/multiespectral_mosaico/*_MS_NIR.TIF 2>/dev/null | wc -l || true)
  [[ "$MS_COUNT" -eq 0 ]] && { echo "❌ ERROR: No hay bandas multiespectrales en data/multiespectral_mosaico/"; exit 1; }
  echo "   Multiespectral: ${MS_COUNT} capturas (4 bandas c/u)"
fi
# Banda D (RGB del M3M) — opcional y opt-in (DBAND=1): un mosaico visible
# rápido, independiente de si hay vuelo M3T/H20T o no. Sin archivos *_D.JPG
# no es un error, DO_DBAND simplemente queda en 0 — la mayoría de las
# misiones no la usan.
D_COUNT=$(ls data/dband_mosaico/*_D.JPG 2>/dev/null | wc -l || true)
DO_DBAND=0
if [[ "$DBAND" -eq 1 && "$D_COUNT" -gt 0 ]]; then
  DO_DBAND=1
  echo "   Banda D: ${D_COUNT} imágenes"
elif [[ "$DBAND" -eq 1 ]]; then
  echo "  ⚠ Se pidió el mosaico de banda D pero no hay archivos *_D.JPG — se omite."
fi

# Ruta de vuelo ACÁ, apenas las fotos están organizadas — no más abajo,
# después de sdk-convert/denoise-thermal/prepare-thermal-native. Esos tres
# pasos leen R-JPEG térmico y son lentos de verdad (sdk-convert invoca un
# binario del SDK de DJI por foto: ~20-40 min reales para unas 200 fotos),
# pero export_flight_path.py NO los necesita — lee GPS/tiempo directo del
# EXIF de data/{rgb,termica,multiespectral}_mosaico (ver su docstring), que
# ya están completos acá arriba. Antes esto corría después de esos tres
# pasos: el geovisor se abría temprano tal como está pensado, pero se
# quedaba sin nada real que mostrar durante toda esa demora — el propio
# comentario de más abajo decía "da algo real que mirar... durante la hora
# que tarda ODM" sin contar que ya faltaba una demora previa de la que la
# ruta de vuelo es independiente.
make flight-path

# Hardware detectado + qué implica el preset elegido, ANTES de arrancar ODM.
# Mismo texto que puede pedirse sin lanzar nada:
#   python3 scripts/hardware.py estimate --preset "$PRESET" --photos N
# Total de fotos = todas las que ODM va a procesar de verdad (RGB + térmico +
# multiespectral cuentan cada una su propio SfM), no una mezcla rara.
_TOTAL_FOTOS=$(( ${RGB_COUNT:-0} + ${TH_COUNT:-0} + ${MS_COUNT:-0} * 4 ))
echo ""
python3 -c "
import json, sys
sys.path.insert(0, 'scripts')
from hardware import estimate_message
m = estimate_message('$PRESET', $_TOTAL_FOTOS)
hw = m['hardware']
print('🖥️  Hardware detectado: {} núcleos, {} — {}'.format(
    hw['cores'],
    f\"{hw['mem_available_mb']/1024:.0f} GB RAM libres\" if hw['mem_available_mb'] else 'RAM no detectada',
    (hw['gpu_name'] or 'GPU disponible') if hw['gpu'] else 'sin GPU (corre en CPU)'))
print('🎚️  ' + m['modelo_texto'])
print('📐 ' + m['resolucion_texto'])
print('⏱️  ' + m['tiempo_texto'])
"
echo ""

# ── Concurrencia acotada por MEMORIA (detección automática de hardware) ──
# ODM documenta su propio consumo: "Peak memory requirement is ~1GB per thread
# and 2 megapixel image resolution" (--max-concurrency), y por defecto usa TODOS
# los núcleos. Escala con el tamaño de imagen, así que para un sensor de N MP el
# pico por hilo es ~N/2 GB.
#
# Esto protegía originalmente solo al band alignment del multiespectral
# (opendm/multispectral.py::compute_alignment_matrices, que carga DOS imágenes
# completas por hilo — con las bandas del M3M a 5 MP, ~2.5 GB por hilo, y en
# una máquina de 20 núcleos eso pedía ~50 GB con el kernel matando el proceso
# sin dejar ninguna traza). Pero CUALQUIER etapa de ODM usa todos los núcleos
# si no se le dice lo contrario — el mismo riesgo existe en RGB (fotos de
# ~12 MP) y en térmico, así que ahora se acota a las tres. También se usa
# más abajo para la preparación en paralelo (RGB/multiespectral/térmico),
# antes de que ODM siquiera arranque.
#
# La cuenta vive en scripts/hardware.py (única fuente — bash no puede hacer
# esta aritmética con megapíxeles fraccionarios como los del sensor térmico,
# 0.33 MP, sin arrastrar errores de redondeo). Delegar además la deja
# reusable desde la webapp (Python) para el mismo mensaje de estimación de
# tiempo que ve el usuario antes de arrancar.
safe_concurrency() {
  python3 scripts/hardware.py concurrency "${1:-2}"
}

echo "--- [1/5] Preparación + reconstrucción, por sensor ---"
pipeline_header
mkdir -p outputs/logs

# Presupuesto de concurrencia COMPARTIDO para la PREPARACIÓN: multiespectral,
# térmico y banda D paralelizan CADA UNO puertas adentro (copia+exiftool /
# dji_irp por imagen, ver safe_concurrency() en prepare_multispectral_odm.py/
# convert_thermal_tiff.py/prepare_dband_odm.py). Si cada uno pidiera su
# propia cuenta memory-aware sin saber de los demás, correrlos todos A LA VEZ
# podría sumar más hilos de los que la RAM disponible banca — el mismo tipo
# de riesgo que ya causó un cuelgue real de la PC esta sesión. Se calcula UN
# presupuesto con el perfil más pesado (5 MP) y se reparte entre los streams
# que de verdad van a correr Y paralelizan puertas adentro. prepare-rgb no
# entra en la cuenta: es una sola llamada a exiftool, sin hilos propios.
PREP_BUDGET=$(safe_concurrency 5)
# El térmico (sdk-convert) NO entra en este reparto — mide ~9MB RSS por
# proceso (dji_irp, confirmado en vivo), nada que ver con el perfil de 5 MP
# de multiespectral que calibra PREP_BUDGET. Usa su propio perfil liviano
# (safe_concurrency(0.33) adentro del script).
N_PARALLEL_STREAMS=0
[[ "$RUN_MULTISPECTRAL" -eq 1 ]] && N_PARALLEL_STREAMS=$((N_PARALLEL_STREAMS + 1))
[[ "$DO_DBAND" -eq 1 ]] && N_PARALLEL_STREAMS=$((N_PARALLEL_STREAMS + 1))
[[ "$N_PARALLEL_STREAMS" -eq 0 ]] && N_PARALLEL_STREAMS=1
PREP_SPLIT=$(( (PREP_BUDGET + N_PARALLEL_STREAMS - 1) / N_PARALLEL_STREAMS ))
[[ "$PREP_SPLIT" -lt 1 ]] && PREP_SPLIT=1

# Publica en el geovisor lo que YA esté listo, sin anunciar una etapa nueva
# (el usuario está mirando la fase real en curso; una etapa "Tiles" acá lo
# haría saltar de fase y volver). generate_tiles.py saltea lo que no cambió
# y usa la ortofoto cruda de ODM como vista previa si el producto recortado
# todavía no existe.
publish_partial() {
  python3 scripts/generate_tiles.py >> outputs/logs/tiles_parciales.log 2>&1 || true
  chmod -R a+rwX geovisor/tiles 2>/dev/null || true
}

# ── ODM: invocado directamente (mismo binario del contenedor base,
# sin docker-in-docker). nvidia-smi es detectado por ODM en runtime —
# si el `docker run` externo no pasó --gpus, ODM cae a CPU solo.
# La salida de ODM es MUY verbosa (miles de líneas) — por defecto se
# filtra a una barra de progreso por etapa (scripts/odm_progress_filter.py);
# el log crudo completo siempre queda en outputs/logs/odm_<label>.log y se
# vuelca entero si el proceso falla, para no perder nunca un traceback.

# Concurrencia liviana para detect_features/match_features/create_tracks/
# undistort: ODM las corre sobre fotos reducidas a feature_process_size
# (~2048px de lado mayor, high quality — no importa la resolución nativa del
# sensor, siempre se downsamplea a eso), un footprint de memoria muy por
# debajo del ~1GB/hilo que sí hace falta cuidar en MVS/DensifyPointCloud más
# abajo. Antes esas etapas usaban la MISMA concurrencia acotada que el resto
# de la corrida (2 hilos en una máquina de 20 núcleos) sin necesidad real —
# bump manual en vivo a 10 confirmó ~700%+ CPU sin subir la memoria de forma
# apreciable. 2 MP de "peso" por hilo (no 12.3 como el RGB completo) es
# conservador para lo que en la práctica pesa mucho menos, así que sigue
# acotado por RAM en máquinas chicas pero escala con los núcleos reales en
# cualquier otra — nunca un número fijo.
LIGHT_CONCURRENCY=$(safe_concurrency 2)

# Una invocación de ODM (run.py) con su filtro de progreso. Devuelve el exit
# code de ODM, NO el del filtro (de ahí PIPESTATUS): el filtro consume el
# pipe y termina bien aunque ODM haya reventado.
#
# $1 proyecto ODM, $2 concurrencia, $3 logfile, $4 label, $5 resume_from
# (vacío = mostrar todas las etapas); el resto son los args de ODM.
_odm_invocar() {
  local name="$1" conc="$2" logfile="$3" label="$4" resume="$5"; shift 5
  local filtro=(python3 /app/scripts/odm_progress_filter.py "ODM ${label^^}" "$logfile")
  [[ -n "$resume" ]] && filtro+=("$resume")
  (cd /code && python3 run.py --project-path /app/processing "$name" "$@" \
      --max-concurrency "$conc") 2>&1 | "${filtro[@]}"
  return "${PIPESTATUS[0]}"
}

run_odm() {
  # $3/$4: concurrencia liviana (SfM completo) / "segura" (MVS) — separadas
  # porque ODM solo permite UN --max-concurrency por invocación de run.py,
  # así que para que cada etapa use la que le corresponde hace falta partir
  # la corrida en dos invocaciones.
  #
  # DÓNDE SE PARTE, Y POR QUÉ AHÍ (esto estuvo mal y costó horas por misión):
  # el único lugar que escribe opensfm/config.yaml —con la línea
  # `processes: %s % args.max_concurrency`, que es la concurrencia que usan
  # DE VERDAD detect_features/match_features/reconstruct/undistort— es
  # OSFMContext.setup() en opendm/osfm.py, y a esa función la llama la etapa
  # **opensfm** (stages/run_opensfm.py:32), NO la etapa dataset. Además solo
  # (re)escribe el config si image_list.txt todavía no existe.
  #
  # Con el corte anterior (`--end-with dataset` / `--rerun-from opensfm`) la
  # fase 1 no llegaba nunca a crear image_list.txt, así que era la FASE 2 la
  # que escribía el config — con la concurrencia PESADA. O sea: el split
  # hacía exactamente lo contrario de lo que buscaba. Medido en vivo sobre
  # una máquina de 20 núcleos: `processes: 2` en RGB y `processes: 6` en
  # multiespectral, con las dos sub-etapas perfectamente paralelizables
  # (features y undistort) tardando 215 min en banda D y 285 min en
  # multiespectral.
  #
  # Corte correcto: fase 1 termina EN opensfm (ahí se escribe el config, con
  # la concurrencia liviana, y corre todo el SfM), fase 2 retoma en openmvs.
  # La fase 2 vuelve a visitar la etapa opensfm pero con rerun=False y con
  # image_list.txt ya existente → no reescribe el config, así que el ajuste
  # de la fase 1 sobrevive. OpenMVS sí lee args.max_concurrency directo de
  # ESTA invocación (stages/openmvs.py), no de config.yaml.
  local label="$1" name="$2" light_conc="$3" heavy_conc="$4"; shift 4
  local logfile="outputs/logs/odm_${label}.log"
  # Log FRESCO por corrida, truncado acá y no en el filtro: las dos fases
  # escriben al mismo archivo en modo append, así que truncar del lado del
  # filtro borraría la fase 1 al arrancar la fase 2. Sin esto los logs se
  # acumulaban entre corridas — odm_thermal.log de barbosa-picodegallo
  # terminó con DOS reconstrucciones completas superpuestas, lo que hace
  # imposible leer de ahí cuánto tardó cada etapa.
  : > "$logfile"
  # Con --fast-orthophoto ODM ni siquiera conecta la etapa openmvs a la
  # cadena (stages/odm_app.py: opensfm.connect(filterpoints) directo), así
  # que no hay ninguna etapa pesada que proteger con una concurrencia más
  # baja — partir la corrida ahí sería puro costo de arranque. Una sola
  # invocación, toda con la concurrencia liviana.
  local fast_ortho=0 a
  for a in "$@"; do [[ "$a" == "--fast-orthophoto" ]] && fast_ortho=1; done
  set +e
  local status
  if [[ "$fast_ortho" -eq 1 ]]; then
    _odm_invocar "$name" "$light_conc" "$logfile" "$label" "" "$@"
    status=$?
  else
    _odm_invocar "$name" "$light_conc" "$logfile" "$label" "" "$@" --end-with opensfm
    status=$?
    if [[ $status -eq 0 ]]; then
      _odm_invocar "$name" "$heavy_conc" "$logfile" "$label" openmvs "$@" --rerun-from openmvs
      status=$?
    fi
  fi
  set -e
  if [[ $status -ne 0 ]]; then
    echo ""
    echo "❌ ODM (${label}) falló (exit $status). Log completo: ${logfile}"
    # 137 = 128+9 (SIGKILL) y 143 = 128+15 (SIGTERM): al proceso lo MATÓ el
    # sistema, no falló por sí solo. Sin este mensaje el usuario ve un log que
    # termina en seco a mitad de una etapa y no tiene forma de saber por qué
    # (fue justo el caso del bug de band alignment, ver safe_concurrency).
    if [[ $status -eq 137 || $status -eq 143 ]]; then
      echo ""
      echo "   ⚠ El sistema MATÓ el proceso (señal $((status - 128))) — ODM no falló solo."
      echo "     Casi siempre es falta de memoria. RAM disponible ahora:"
      awk '/^MemAvailable:/ {printf "       %.1f GB de %s\n", $2/1048576, "RAM"}' /proc/meminfo 2>/dev/null || true
      echo "     Probá bajando la concurrencia (menos hilos = menos memoria pico):"
      echo "       docker run ... -e MAX_CONCURRENCY=4 ... raptor run"
      echo "     Si corriste con Docker Desktop/WSL, revisá también el límite de"
      echo "     memoria asignado a la VM (.wslconfig / Settings > Resources)."
    fi
    echo "─── últimas 150 líneas ───"
    tail -150 "$logfile"
    exit $status
  fi
}

# ── Etapas: banderas + contador ────────────────────────────────────
# Qué etapas corren en ESTA misión se decide una sola vez, y esa misma
# decisión se usa dos veces: para el total de la barra de progreso y para
# ejecutar (o no) cada bloque.
DO_THERMAL=0; [[ "$RUN_THERMAL" -eq 1 ]] && DO_THERMAL=1
DO_MS=0;      [[ "$RUN_MULTISPECTRAL" -eq 1 ]] && DO_MS=1
# Área afectada + severidad necesita AMBAS señales: el brillo multiespectral y
# la anomalía térmica son las dos que separan quemado de suelo desnudo o vías.
DO_AREA=0;    [[ "$DO_MS" -eq 1 && "$DO_THERMAL" -eq 1 ]] && DO_AREA=1
DO_ENTREGA=0; [[ -n "${EXPORT_DIR:-}" ]] && DO_ENTREGA=1
# confidence-mask es "RGB ∩ térmico" — confidence_mask.py abre
# outputs/rgb_orthomosaic.tif sin chequear que exista, y en MODE=thermal
# (RUN_RGB=0) nunca existe. Sin este gate, toda misión solo-térmico
# terminaba en un traceback acá mismo, después de horas de ODM.
DO_CONFIANZA=0; [[ "$RUN_RGB" -eq 1 && "$DO_THERMAL" -eq 1 ]] && DO_CONFIANZA=1
# Reconstrucción + recorte + exportación: UNA sola etapa en el contador
# aunque adentro corran varios sensores en simultáneo (mismo criterio que ya
# usaba el recorte) — el detalle real se ve en los sub-encabezados que cada
# make target ya imprime por su cuenta.
DO_TRIM=0; [[ "$RUN_RGB" -eq 1 || "$DO_THERMAL" -eq 1 || "$DO_MS" -eq 1 || "$DO_DBAND" -eq 1 ]] && DO_TRIM=1

STAGE_FLAGS=("$DO_TRIM" "$DO_CONFIANZA" "$DO_AREA" 1 1 "$DO_ENTREGA")
TOTAL_STAGES=0
for _f in "${STAGE_FLAGS[@]}"; do TOTAL_STAGES=$((TOTAL_STAGES + _f)); done

# Avanza el contador solo: ningún bloque tiene que acordarse de incrementarlo.
STAGE=0
stage_begin() {
  STAGE=$((STAGE + 1))
  pipeline_progress_start "$1" "$STAGE" "$TOTAL_STAGES"
}

# T_POST_START se setea más abajo, DESPUÉS de este bloque (ver ahí por qué):
# si se seteara acá, la etapa "post" del resumen de tiempos incluiría toda la
# reconstrucción (ver también scripts/print_timings.py).

[[ "$DO_TRIM" -eq 1 ]] && stage_begin "Preparación + reconstrucción + recorte, por sensor"

# Megapíxeles del sensor de cada proyecto — lo que decide su footprint de
# memoria por hilo en la fase pesada (MVS / band alignment), y por lo tanto
# cuántos proyectos entran a la vez en odm_slots() (scripts/hardware.py).
_odm_mp() {
  case "$1" in
    rgb)
      # 12.3 (4056x3040, H20T/M3T wide) es el costo de DensifyPointCloud:
      # mapas de profundidad a resolución nativa, simultáneos por hilo — es
      # la etapa que de verdad justifica reservar la máquina entera para RGB
      # solo (ver el comentario de odm_slots()). Con FAST_ORTHOPHOTO_RGB=1
      # (vistazo/rápido) esa etapa NUNCA corre — confirmado en run_odm(): sin
      # --fast-orthophoto en los args no hay fase "pesada" separada, todo
      # corre con la concurrencia liviana en una sola invocación. Sin este
      # caso, odm_slots() seguía negando el solapamiento con otro sensor
      # (p.ej. térmico) basado en un costo que esta corrida no paga —
      # confirmado en vivo: 10% CPU y >15 GB libres durante la reconstrucción
      # de RGB en modo escarpado, con térmico ya terminado y ocioso.
      # La mitad del perfil nativo es una estimación razonada (lo más caro
      # que queda sin MVS es texturizar sobre las imágenes "undistorted", que
      # SÍ están cerca de la resolución nativa — no tan liviano como
      # LIGHT_MP), no una medición — PENDIENTE de calibrar con una corrida
      # real que solape RGB con otro sensor bajo este modo.
      if [[ "$FAST_ORTHOPHOTO_RGB" == "1" ]]; then echo 6.15; else echo 12.3; fi ;;
    thermal)       echo 0.33 ;;   # 640x512
    multispectral) echo 5 ;;      # bandas del M3M
    dband)         echo 5 ;;      # cámara RGB del M3M
  esac
}
_odm_proyecto() {
  case "$1" in
    rgb)           echo rgb_odm ;;
    thermal)       echo thermal_native_odm ;;
    multispectral) echo multispectral_odm ;;
    dband)         echo dband_odm ;;
  esac
}
# Preparación de cada sensor — YA paralela entre sí (presupuesto arriba),
# ahora además INDEPENDIENTE de cuándo terminan las de los demás: cada una
# se llama desde la cadena de SU propio sensor (ver el bucle de despacho más
# abajo), no desde una barrera común. RGB/multiespectral/banda D preparan en
# segundos u minutos; el térmico invoca dji_irp foto por foto (20-40 min
# cada 200) — antes las 4 esperaban a que terminaran las 4 antes de que
# CUALQUIERA pudiera empezar a reconstruir. Confirmado en vivo (misión
# mision_2026-08-08): eso solo no explica una demora de horas, pero sí deja
# núcleos ociosos gratis mientras dura.
_odm_prep() {
  case "$1" in
    rgb)           make prepare-rgb ;;
    thermal)       make sdk-convert && make denoise-thermal && make prepare-thermal-native ;;
    multispectral) MAX_CONCURRENCY=$PREP_SPLIT make prepare-multispectral ;;
    dband)         MAX_CONCURRENCY=$PREP_SPLIT make prepare-dband ;;
  esac
}
# Argumentos propios de cada sensor. Se expanden SIN comillas donde se
# usan: ninguno contiene espacios (son banderas y números), y así se
# pueden devolver desde una función sin arrastrar arrays anidados, que
# bash no tiene.
_odm_args() {
  case "$1" in
    rgb)
      # --dsm: el modelo de superficie de la misión sale de acá cuando hay
      # vuelo RGB.
      # --fast-orthophoto (solo vistazo/rápido, ver FAST_ORTHOPHOTO_RGB más
      # arriba): salta DensifyPointCloud (MVS) — el DSM sale entonces de la
      # nube DISPERSA de SfM en vez de la densa. Mismo trade-off que banda D
      # ya asume siempre; acá queda atado al preset porque RGB SÍ tiene
      # sentido en calidad completa (es la fuente del DSM de la misión).
      echo "--feature-quality $FEAT_QUALITY --orthophoto-resolution $ODM_RES_CM"\
           "--dsm --dem-resolution $ODM_RES_CM --crop 0 --dem-gapfill-steps 3"\
           "--min-num-features $MIN_FEATURES --matcher-neighbors $MATCHER_NEIGHBORS"\
           "--pc-quality $PC_QUALITY --sfm-algorithm $SFM_ALGORITHM --skip-report"\
           "$([[ "$FAST_ORTHOPHOTO_RGB" == "1" ]] && echo --fast-orthophoto)" ;;
    thermal)
      # --radiometric-calibration camera: dispara la conversión Kelvin×100→°C
      # nativa de ODM para DJI H20T (opendm/thermal.py) sobre los TIFF que
      # prepare_thermal_native_odm.py ya re-encodeó + etiquetó (Make=DJI,
      # Model=ZH20T, XMP Camera:BandName=LWIR). Sin --skip-orthophoto: acá es
      # donde ODM renderiza la malla 3D con la textura calibrada — el
      # ortomosaico térmico REAL sale de esta etapa, no de un script propio.
      # El sensor es de 640x512, así que su GSD es ~10x más grueso que el del
      # RGB: pedir 1 cm no lo mejora (ODM recorta al GSD real igual) pero
      # evita fijar un número que quede corto en un vuelo más bajo.
      echo "--feature-quality $FEAT_QUALITY --radiometric-calibration camera"\
           "--orthophoto-resolution $ODM_RES_CM --crop 0"\
           "--min-num-features $MIN_FEATURES --matcher-neighbors $MATCHER_NEIGHBORS"\
           "--pc-quality $PC_QUALITY --sfm-algorithm $SFM_ALGORITHM --skip-report" ;;
    multispectral)
      # --radiometric-calibration camera+sun: usa el sensor de sol embebido en
      # cada banda (DJI M3M) para calibrar a reflectancia sin panel físico.
      # ODM agrupa las 4 bandas por captura vía el tag XMP Camera:BandName —
      # nada que armar de nuestro lado, ya viene correcto en los TIFF del M3M.
      echo "--feature-quality $FEAT_QUALITY --radiometric-calibration camera+sun"\
           "--dsm --dem-resolution $ODM_RES_CM --crop 0 --dem-gapfill-steps 3"\
           "--min-num-features $MIN_FEATURES --matcher-neighbors $MATCHER_NEIGHBORS"\
           "--pc-quality $PC_QUALITY --sfm-algorithm $SFM_ALGORITHM --skip-report" ;;
    dband)
      # Sin --dsm: el objetivo es un mosaico visible RÁPIDO para análisis
      # preliminar, no un modelo de superficie — si la misión también tiene
      # RGB/multiespectral con --dsm, ya hay uno.
      #
      # --fast-orthophoto: sin esto, "rápida" era mentira — ODM corre
      # DensifyPointCloud (MVS) igual aunque no se le pida --dsm (la nube
      # densa alimenta el mesh/textura de la ortofoto de todos modos), y esa
      # etapa es la más cara de TODA la corrida, casi tanto como un vuelo RGB
      # completo. Confirmado en vivo: 39 capturas tardaron ~5h50m en un vuelo
      # que en el resto de la misión tardó menos. --fast-orthophoto salta
      # directo de SfM a filterpoints (stages/odm_app.py) y arma la ortofoto
      # desde la nube DISPERSA — exactamente lo que banda D necesita.
      # pc-quality deja de aplicar (no hay MVS que lo use).
      echo "--feature-quality $FEAT_QUALITY --orthophoto-resolution $ODM_RES_CM"\
           "--crop 0 --min-num-features $MIN_FEATURES"\
           "--matcher-neighbors $MATCHER_NEIGHBORS --fast-orthophoto"\
           "--sfm-algorithm $SFM_ALGORITHM --skip-report" ;;
  esac
}
# Recorte + índices de CADA sensor — corre síncrono dentro de la cadena de
# ESE sensor (que ya es su propio job de fondo, ver el despacho más abajo),
# así que "en paralelo con la reconstrucción del sensor siguiente" sigue
# siendo cierto: este paso corre DESPUÉS de liberar el cupo de reconstrucción
# de este sensor (ver _liberar_cupo en el despacho), momento en el que el
# siguiente sensor en cola ya puede entrar a reconstruir.
_odm_post() {
  case "$1" in
    rgb)     make clean-dsm && make trim-edges-dsm && make trim-edges-rgb ;;
    thermal) make trim-edges-thermal ;;
    multispectral)
      if [[ "$RUN_RGB" -eq 0 ]]; then
        # Sin vuelo RGB el DSM sale del proyecto multiespectral (también
        # corre con --dsm). Mismos scripts que el caso RGB, solo apuntados
        # por env a processing/multispectral_odm — la limpieza y el recorte
        # por solape de cámaras no dependen de qué sensor produjo la
        # reconstrucción.
        DSM_SRC=processing/multispectral_odm/odm_dem/dsm.tif make clean-dsm
        ODM_RGB_DIR=processing/multispectral_odm make trim-edges-dsm
      fi
      make trim-edges-multispectral
      make compute-indices ;;
    dband)   make trim-edges-dband ;;
  esac
}
# Exportación cloud-optimized de LO QUE PRODUJO este sensor — COG para sus
# rásters, COPC para su nube de puntos georreferenciada — apenas termina SU
# post-procesamiento, no al final de toda la misión. El COG de cada archivo
# es idempotente (export_cog.py se saltea lo que ya sea COG en el pase de
# seguridad final), así que no hay costo por teselar/exportar temprano acá Y
# tener igual el pase final que cubre lo que cruza sensores (confidence_mask,
# severidad, etc. — ver el análisis cruzado más abajo).
# COG y COPC son productos independientes (un ráster, una nube de puntos) —
# si uno falla igual se intenta el otro, y se reporta fallo si CUALQUIERA de
# los dos falló. Con un `&&`/`set -e` ingenuo, un COG roto salteaba el COPC
# entero sin necesidad: son artefactos distintos con causas de falla distintas.
_odm_export() {
  local ok=0
  case "$1" in
    rgb)     python3 scripts/export_cog.py outputs/rgb_orthomosaic.tif outputs/dsm.tif || ok=1 ;;
    thermal) python3 scripts/export_cog.py outputs/thermal_orthomosaic.tif || ok=1 ;;
    multispectral)
      if [[ "$RUN_RGB" -eq 0 ]]; then
        python3 scripts/export_cog.py outputs/multispectral_orthomosaic.tif outputs/dsm.tif outputs/indices/*.tif || ok=1
      else
        python3 scripts/export_cog.py outputs/multispectral_orthomosaic.tif outputs/indices/*.tif || ok=1
      fi ;;
    dband)   python3 scripts/export_cog.py outputs/dband_orthomosaic.tif || ok=1 ;;
  esac
  python3 scripts/export_copc.py "$1" || ok=1
  return "$ok"
}

ODM_ORDEN=()
[[ "$RUN_RGB" -eq 1 ]] && ODM_ORDEN+=(rgb)
[[ "$RUN_THERMAL" -eq 1 ]] && ODM_ORDEN+=(thermal)
[[ "$RUN_MULTISPECTRAL" -eq 1 ]] && ODM_ORDEN+=(multispectral)
[[ "$DO_DBAND" -eq 1 ]] && ODM_ORDEN+=(dband)

if [[ "$SKIP_ODM" -eq 0 && "${#ODM_ORDEN[@]}" -gt 0 ]]; then
  # ── Reparto de la máquina entre las reconstrucciones que van a coincidir ──
  # Los cuatro proyectos son INDEPENDIENTES (distinta carpeta fuente, distinta
  # carpeta destino, ningún archivo compartido) — ver el comentario de
  # odm_slots() en scripts/hardware.py.
  _MPS=()
  for _l in "${ODM_ORDEN[@]}"; do _MPS+=("$(_odm_mp "$_l")"); done
  # RAPTOR_ODM_PARALELO=0/1 fuerza el modo secuencial de siempre.
  _MAXPAR_FLAG=()
  [[ -n "${RAPTOR_ODM_PARALELO:-}" ]] && _MAXPAR_FLAG=(--max-paralelo "$RAPTOR_ODM_PARALELO")
  # --shell devuelve las tres cosas en tres líneas: un solo arranque de
  # Python (~0.3 s) en vez de tres.
  {
    read -r ODM_SLOTS
    read -ra ODM_HILOS
    read -ra ODM_HILOS_LIGHT
  } < <(python3 scripts/hardware.py odm-slots "${_MPS[@]}" "${_MAXPAR_FLAG[@]}" --shell)

  echo "--- Reconstrucciones ODM: ${ODM_ORDEN[*]} ---"
  echo "    preset «${PRESET_TITULO}» → pc-quality=${PC_QUALITY}, feature-quality=${FEAT_QUALITY}, ${MIN_FEATURES} features/foto, sfm=${SFM_ALGORITHM}, hybrid-bundle-adjustment=$([[ "$HYBRID_BA" == "1" ]] && echo sí || echo no)"
  if [[ "$ODM_SLOTS" -gt 1 ]]; then
    echo "    hasta ${ODM_SLOTS} reconstrucciones a la vez (de $(nproc) núcleos y la RAM disponible; RAPTOR_ODM_PARALELO=1 para forzar de a una)"
  else
    echo "    de a una (el reparto de memoria no da para solapar en esta máquina)"
  fi
  echo "    cada sensor arranca a reconstruir apenas SU preparación termina — no espera a los demás"

  # ── Semáforo de reconstrucción: FIFO con ODM_SLOTS fichas ──────────
  # Cada sensor prepara de forma independiente (ya paralelo entre sí, ver
  # PREP_SPLIT arriba) y recién compite por una ficha al llegar a
  # RECONSTRUIR — así RGB/multiespectral/banda D (preparación de segundos a
  # minutos) no esperan a que el térmico termine la suya (20-40 min cada 200
  # fotos) para empezar a reconstruir, que es lo que de verdad pesa. La ficha
  # se libera apenas termina la reconstrucción de ese sensor (ANTES de su
  # recorte/exportación, que son livianos) para que el siguiente en cola
  # entre lo antes posible.
  _SEM_DIR=$(mktemp -d)
  mkfifo "$_SEM_DIR/tokens"
  exec {SEM_FD}<>"$_SEM_DIR/tokens"
  rm -rf "$_SEM_DIR"   # el fd ya quedó abierto; el archivo en sí no hace falta más
  for ((_s = 0; _s < ODM_SLOTS; _s++)); do printf '\n' >&"$SEM_FD"; done

  # Bandera de fallo compartida por archivo, no por variable: las cadenas de
  # sensor corren en subshells separados, que no pueden ver ni modificar una
  # variable del padre entre sí.
  FALLOFLAG="outputs/logs/.odm_fallo"
  rm -f "$FALLOFLAG" outputs/logs/.timing_*

  declare -A _PID_LABEL=()
  for _i in "${!ODM_ORDEN[@]}"; do
    _label="${ODM_ORDEN[$_i]}"
    _hilos_light="${ODM_HILOS_LIGHT[$_i]}"
    _hilos="${ODM_HILOS[$_i]}"
    (
      set -e
      _t_prep0=$(date +%s)
      _odm_prep "$_label"
      echo "⏱  Preparación de ${_label} completa en $(( $(date +%s) - _t_prep0 ))s"

      # Ficha del semáforo — bloquea ACÁ si los cupos de reconstrucción están
      # ocupados, no antes: la preparación de arriba ya corrió sin esperar a
      # nadie.
      read -u "$SEM_FD"
      _liberado=0
      _liberar_cupo() { [[ "$_liberado" -eq 1 ]] && return; _liberado=1; printf '\n' >&"$SEM_FD" 2>/dev/null || true; }
      trap _liberar_cupo EXIT

      if [[ -f "$FALLOFLAG" ]]; then
        echo "⏭  ${_label}: se salta — otra reconstrucción de esta misión ya falló"
        exit 1
      fi

      echo "--- ODM ${_label}: ${_hilos_light} hilos en SfM, ${_hilos} en la fase densa ---"
      _t0=$(date +%s)
      # run_odm EN UN SUBSHELL PROPIO — ver el cuerpo de run_odm(): en un
      # fallo hace `exit $status` (para imprimir el diagnóstico de SIGKILL/
      # volcar el log y cortar ahí mismo, ver más abajo). Sin este subshell
      # extra, ese `exit` mata DIRECTO la cadena de este sensor —incluido
      # este `if`— y el `touch "$FALLOFLAG"` de abajo nunca llega a
      # ejecutarse: la bandera de fallo queda sin marcar y un sensor
      # pendiente, todavía preparando, no se entera y arranca a reconstruir
      # igual. Comprobado en vivo con un test de este mismo cambio. Con el
      # subshell, el `exit` de run_odm solo termina ESE subshell interno; el
      # `if !` de acá SÍ ve su código de salida.
      if ! ( run_odm "$_label" "$(_odm_proyecto "$_label")" "$_hilos_light" "$_hilos" \
            $(_odm_args "$_label") "${HYBRID_BA_FLAG[@]}" ); then
        touch "$FALLOFLAG"
        exit 1
      fi
      _t1=$(date +%s)
      echo "⏱  ODM ${_label} completo en $((_t1 - _t0))s"
      _LABEL_MAY=$(printf '%s' "$_label" | tr a-z A-Z)
      printf 'T_ODM_%s_START=%s\nT_ODM_%s_END=%s\n' "$_LABEL_MAY" "$_t0" "$_LABEL_MAY" "$_t1" \
        > "outputs/logs/.timing_${_label}"

      # Se libera EXPLÍCITAMENTE acá (no solo en el trap de salida): el
      # siguiente sensor en cola tiene que poder entrar a reconstruir
      # mientras ESTE sensor todavía está en su recorte/exportación, que son
      # livianos (scipy/GDAL sobre un ráster) comparados con SfM/MVS.
      _liberar_cupo

      # Vista previa apenas ODM renderiza, sin esperar al recorte. El
      # térmico NO se publica crudo a propósito (ver THERMAL_IN en
      # generate_tiles.py): su calibración Kelvin→°C se aplica recién en el
      # recorte, y una escala de temperatura falsa en una herramienta de
      # incendios es peor que no mostrar nada todavía.
      [[ "$_label" != "thermal" ]] && publish_partial

      _odm_post "$_label"

      # Tiles + exportación cloud-optimized de ESTE sensor, apenas SU post
      # termina — no se espera a los otros sensores ni al análisis cruzado.
      publish_partial
      _odm_export "$_label"
    ) &
    _PID_LABEL[$!]="$_label"
  done

  _ODM_FALLO=0
  for pid in "${!_PID_LABEL[@]}"; do
    wait "$pid" || _ODM_FALLO=1
  done
  exec {SEM_FD}<&-

  # Tiempos por sensor: cada cadena los dejó en su propio archivo — un
  # subshell de fondo no puede exportar variables de vuelta al padre.
  for _label in "${ODM_ORDEN[@]}"; do
    _f="outputs/logs/.timing_${_label}"
    [[ -f "$_f" ]] && source "$_f"
  done
  rm -f outputs/logs/.timing_* "$FALLOFLAG"

  if [[ "$_ODM_FALLO" -eq 1 ]]; then
    echo ""
    echo "❌ ERROR: falló al menos una reconstrucción de ODM."
    echo "   El detalle está más arriba y en outputs/logs/odm_*.log."
    exit 1
  fi
elif [[ "$DO_TRIM" -eq 1 ]]; then
  # SKIP_ODM=1 (reusar lo ya reconstruido): sin reconstrucción que correr, el
  # recorte + exportación de los sensores presentes igual arrancan todos
  # juntos entre sí en vez de uno atrás de otro — mismo _odm_post/_odm_export
  # que usa el camino con ODM, así que el caso "sin RGB" de multiespectral
  # (limpieza de DSM propia) se resuelve exactamente igual en los dos casos.
  REUSE_PIDS=()
  for _label in rgb thermal multispectral dband; do
    case "$_label" in
      rgb)           _va=$RUN_RGB ;;
      thermal)       _va=$DO_THERMAL ;;
      multispectral) _va=$DO_MS ;;
      dband)         _va=$DO_DBAND ;;
    esac
    if [[ "$_va" -eq 1 ]]; then
      # set -e acá adentro es necesario, no cosmético: _odm_post() para
      # multispectral tiene un `if` con statements sueltos (no encadenados
      # con &&) — sin errexit, un `make clean-dsm` que falla ahí no corta la
      # cadena, sigue a trim-edges-dsm sobre un DSM roto y el exit code final
      # termina siendo el de `make compute-indices`, no el del paso que
      # realmente falló.
      ( set -e; _odm_post "$_label"; publish_partial; _odm_export "$_label" ) &
      REUSE_PIDS+=($!)
    fi
  done
  REUSE_FAILED=0
  for pid in "${REUSE_PIDS[@]}"; do wait "$pid" || REUSE_FAILED=1; done
  if [[ "$REUSE_FAILED" -eq 1 ]]; then
    echo "❌ ERROR: falló al menos un recorte/exportación de post-procesamiento."
    echo "   Revisá los mensajes de arriba para saber cuál — cada uno imprime su propio error."
    exit 1
  fi
fi

[[ "$DO_TRIM" -eq 1 ]] && pipeline_progress_done "Reconstrucción y recortes listos"

# Reloj de "post" recién ACÁ, después del bloque de reconstrucción de
# arriba: antes T_POST_START se seteaba ANTES de la reconstrucción y la
# etapa "post" del resumen de tiempos tragaba TODO el ODM — en una corrida
# completa con ODM (barbosa-chorrera: ~9.5 h de reconstrucción) habría
# reportado ~10.5 h de "post" y nada decía dónde estaba el tiempo real.
T_POST_START=$(date +%s)

# ── Análisis cruzado ─────────────────────────────────────────────────
# Máscara de confianza, calidad de vuelo y área afectada+severidad leen
# ortomosaicos/índices ya recortados, pero NO se leen ni se escriben entre
# sí — confirmado archivo por archivo:
#   confidence-mask   lee outputs/{rgb,thermal}_orthomosaic.tif
#   flight-quality    lee reconstruction.json + esos mismos dos ortomosaicos
#   área+severidad    lee outputs/{multispectral,thermal}_orthomosaic.tif
#                      + outputs/indices/*.tif
# Antes corrían estrictamente uno detrás del otro por costumbre, no por
# necesidad. Los stage_begin/done de los dos que tienen contador propio
# (confianza, área) se emiten desde acá —el padre— ANTES de lanzarlos de
# fondo: incrementar STAGE desde dentro de un subshell no se ve reflejado en
# el padre, así que la numeración "[n/total]" tiene que resolverse afuera.
[[ "$DO_CONFIANZA" -eq 1 ]] && stage_begin "Máscara de confianza"
[[ "$DO_AREA" -eq 1 ]] && stage_begin "Área afectada + clasificación de severidad"

CROSS_PIDS=()
if [[ "$DO_CONFIANZA" -eq 1 ]]; then
  ( make confidence-mask && python3 scripts/export_cog.py outputs/confidence_mask.tif ) &
  CROSS_PIDS+=($!)
fi
if [[ "$DO_THERMAL" -eq 1 ]]; then
  # Calidad del LEVANTAMIENTO (solape de cámaras, velocidad de vuelo, % de
  # imágenes reconstruidas) — no depende de multiespectral, así que corre
  # siempre que haya térmico, no solo en el modo solo-térmico. Liviano — no
  # tiene stage_begin propio.
  (
    make flight-quality
    if [[ "$DO_MS" -eq 0 ]]; then
      # El hotspot es puramente térmico (temperatura absoluta) y no depende
      # de NDVI ni del polígono de área afectada — pero antes SOLO se
      # generaba como subproducto de compute-severity, que exige
      # multiespectral (su señal primaria es NDVI). Una misión RGB+térmico
      # sin M3M se quedaba sin esta capa sin ninguna necesidad real. Con
      # multiespectral, la cadena de abajo (DO_AREA) ya genera un hotspot
      # recortado al área detectada, más específico — no se pisa acá.
      make compute-thermal-hotspot
      # situation.json en modo SOLO térmico: sin multiespectral no hay área
      # afectada ni severidad que resumir, pero la fecha de vuelo y los
      # focos activos del hotspot recién generado sí se pueden reportar.
      make situation-summary
    fi
  ) &
  CROSS_PIDS+=($!)
fi
if [[ "$DO_AREA" -eq 1 ]]; then
  ( make detect-area-afectada && make compute-severity && make situation-summary ) &
  CROSS_PIDS+=($!)
fi

CROSS_FAILED=0
for pid in "${CROSS_PIDS[@]}"; do wait "$pid" || CROSS_FAILED=1; done
if [[ "$CROSS_FAILED" -eq 1 ]]; then
  echo "❌ ERROR: falló el análisis cruzado (máscara de confianza, calidad de vuelo o área afectada)."
  echo "   Revisá los mensajes de arriba para saber cuál — cada uno imprime su propio error."
  exit 1
fi
[[ "$DO_CONFIANZA" -eq 1 ]] && pipeline_progress_done "Máscara de confianza lista"
[[ "$DO_AREA" -eq 1 ]] && pipeline_progress_done "Área afectada y severidad listas"
[[ "${#CROSS_PIDS[@]}" -gt 0 ]] && publish_partial

stage_begin "Generación de tiles XYZ"
make tiles
pipeline_progress_done "Tiles listos para el geovisor"

stage_begin "Exportación cloud-optimized (COG + COPC)"
# Pase de SEGURIDAD, no el primer pase: cada sensor ya exportó sus propios
# productos apenas terminó su post-procesamiento (ver _odm_export en el
# bloque de reconstrucción) y el análisis cruzado exportó confidence_mask.tif
# apenas terminó el suyo — esto corre sin args, así que ambos scripts se
# saltean lo que ya esté listo (export_cog.py chequea el layout COG real,
# export_copc.py compara mtimes) y solo convierten lo que faltaba. COG y COPC
# igual corren juntos entre sí: no comparten entrada ni salida.
make export-cog &
_PID_COG=$!
make export-copc &
_PID_COPC=$!
_EXPORT_FALLO=0
wait "$_PID_COG"  || _EXPORT_FALLO=1
wait "$_PID_COPC" || _EXPORT_FALLO=1
if [[ "$_EXPORT_FALLO" -eq 1 ]]; then
  echo "❌ ERROR: falló la exportación cloud-optimized (COG o COPC)."
  echo "   Revisá los mensajes de arriba — cada una imprime su propio error."
  exit 1
fi
pipeline_progress_done "Rasters COG y nubes COPC listos"

# Entrega opcional a la carpeta que eligió el usuario (formato + CRS propios).
if [[ "$DO_ENTREGA" -eq 1 ]]; then
  stage_begin "Exportación a carpeta de entrega"
  make export-products
  pipeline_progress_done "Entrega exportada a ${EXPORT_DIR}"
fi

# Red de seguridad: si alguien agrega un stage_begin y se olvida de su flag en
# STAGE_FLAGS (o al revés), la barra queda desfasada. Se avisa en vez de
# fallar: es cosmético y la misión ya está procesada.
if [[ "$STAGE" -ne "$TOTAL_STAGES" ]]; then
  echo "  ⚠ progreso desfasado: corrieron ${STAGE} etapas pero STAGE_FLAGS declara ${TOTAL_STAGES}."
  echo "    Revisá STAGE_FLAGS en docker/entrypoint.sh."
fi

T_POST_END=$(date +%s)
T_MISSION_END=$(date +%s)
export T_MISSION_START T_MISSION_END
export T_PREP_START="${T_PREP_START:-}" T_PREP_END="${T_PREP_END:-}"
export T_ODM_RGB_START="${T_ODM_RGB_START:-}" T_ODM_RGB_END="${T_ODM_RGB_END:-}"
export T_ODM_THERMAL_START="${T_ODM_THERMAL_START:-}" T_ODM_THERMAL_END="${T_ODM_THERMAL_END:-}"
export T_ODM_MS_START="${T_ODM_MS_START:-}" T_ODM_MS_END="${T_ODM_MS_END:-}"
# T_ODM_DBAND también se exporta (si no, print_timings.py — subproceso, solo
# ve variables exportadas — nunca mostraba la banda D en el resumen).
export T_ODM_DBAND_START="${T_ODM_DBAND_START:-}" T_ODM_DBAND_END="${T_ODM_DBAND_END:-}"
export T_POST_START T_POST_END
python3 scripts/print_timings.py || true

# Todo lo que escribe este contenedor queda dueño de root (corre como root
# adentro), lo que deja los productos ilegibles para el usuario del host —
# ArcGIS/QGIS abiertos desde Windows vía \\wsl$\ los reportan como "no
# válidos" en vez de un error de permisos claro. Se abre a todos los
# usuarios al final de cada corrida, no solo una vez a mano.
chmod -R a+rwX outputs preprocessing processing geovisor/tiles 2>/dev/null || true

# Resumen legible por máquina para automatización/CI (./raptor run --json).
# No debe tumbar la corrida si algo acá falla: los productos ya están escritos.
python3 scripts/run_summary.py 0 || echo "  ⚠ no se pudo generar run_summary.json"

echo ""
echo "═══════════════════════════════════════════════════"
echo "  ✅ Pipeline completo (MODE=${MODE})"
echo "═══════════════════════════════════════════════════"
echo "Productos en outputs/ (COG):"
ls -lh outputs/*.tif outputs/indices/*.tif 2>/dev/null | awk '{printf "  %-28s %s\n", $NF, $5}' || true
echo "Nubes de puntos en outputs/ (COPC):"
ls -lh outputs/*.copc.laz 2>/dev/null | awk '{printf "  %-28s %s\n", $NF, $5}' || echo "  (ninguna)"
echo "Tiles (geovisor/tiles/):"
TILE_LAYERS="hillshade"
[[ "$RUN_RGB" -eq 1 ]] && TILE_LAYERS="rgb $TILE_LAYERS"
[[ "$RUN_THERMAL" -eq 1 ]] && TILE_LAYERS="$TILE_LAYERS thermal"
[[ "$RUN_MULTISPECTRAL" -eq 1 ]] && TILE_LAYERS="$TILE_LAYERS ndvi gndvi ndre"
[[ "$DO_DBAND" -eq 1 ]] && TILE_LAYERS="$TILE_LAYERS dband"
for l in $TILE_LAYERS; do
  n=$(find "geovisor/tiles/$l" -name '*.png' 2>/dev/null | wc -l)
  echo "  $l: ${n} tiles"
done
echo "Logs de esta corrida: outputs/logs/ (VERBOSE=1 para ver todo en vivo la próxima)"

if [[ "$SERVE" -eq 1 ]]; then
  echo ""
  echo "🌐 Geovisor: http://localhost:${PORT}/geovisor/index.html"
  export RAPTOR_RUNS_ROOT="${RAPTOR_RUNS_ROOT:-/app/runs}"
  exec python3 -m webapp.main "$PORT"
fi
