#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
# Entrypoint del contenedor único de producción (FROM opendronemap/odm:gpu:
# ODM y el post-procesamiento propio en el mismo filesystem, sin
# docker-in-docker).
#
#   raptor:latest              webapp interactiva (default) → :8080
#   raptor:latest run          pipeline batch, sin interacción
#   raptor:latest serve        solo geovisor sobre outputs ya existentes
#   raptor:latest shell        debug
#
# Batch necesita /input montado (y /input_ms para el vuelo multiespectral
# del M3M, que es otro dron: si no está montado, ese módulo ni se toca).
#
# GPU: pasá `--gpus all` SIEMPRE que la máquina lo permita. Sin NVIDIA el
# binario de reconstrucción densa ni carga (libcuda.so.1) y la corrida muere
# con "Child returned 127" DESPUÉS de pagar todo el SfM — por eso se chequea
# al arrancar, no cuando explota. Los presets con fast_orthophoto no la usan.
#
# Variables de entorno:
#   MODE            rgb | rgb+thermal | thermal | none   (default rgb+thermal)
#                    `none` = misión sin vuelo RGB/térmico (solo M3M).
#   SOURCE_DIR      fuente de imágenes            (default /input)
#   MS_SOURCE_DIR   fuente multiespectral         (default /input_ms)
#   DBAND           1 = además la banda D del M3M (opt-in)
#   SUB_SAMPLE      N>=2: procesar 1 de cada N fotos (modo urgencia)
#   SKIP_ODM        1 = reusar processing/ ya reconstruido
#   PRESET          vistazo | rapido | estandar | alta | maxima
#                    (QUALITY 0-100 se sigue aceptando y se mapea al preset)
#   TERRENO         plano | escarpado — escarpado fuerza SfM incremental
#   MAX_CONCURRENCY fuerza los hilos de ODM (default: según RAM disponible)
#   RAPTOR_ODM_PARALELO  cuántas reconstrucciones a la vez (default: auto)
#   PORT / SERVE / VERBOSE
#
# Entrega opcional (sin EXPORT_DIR no se exporta nada):
#   EXPORT_DIR, EXPORT_PRODUCTS ("all" o claves separadas por coma),
#   EXPORT_RASTER_FORMAT (cog|gtiff), EXPORT_VECTOR_FORMAT
#   (geojson|gpkg|shp|kml), EXPORT_EPSG (9377 nacional, o "source").
#
# La tabla de presets/terrenos vive en scripts/hardware.py — fuente única
# compartida con la webapp y el CLI.
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
# Submuestreo de emergencia: procesar 1 de cada N fotos (SUB_SAMPLE=N, N>=2).
# Ver scripts/subsample_photos.py — la palanca más grande para acortar una
# corrida en modo vistazo/rápido con terreno escarpado (la reconstrucción
# incremental es secuencial y proporcional al número de fotos).
SUB_SAMPLE="${SUB_SAMPLE:-0}"
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

# ── Perfil de calidad ───────────────────────────────────────────────
# La tabla completa (qué significa cada campo y por qué su valor) vive en
# scripts/hardware.py: fuente única que leen también la webapp y el CLI, así
# el tiempo que se le promete al usuario ANTES de arrancar es el que corre
# después. Acá solo se lee el perfil ya resuelto — `--shell` devuelve una
# línea por campo, en orden fijo, en una sola invocación de python3.
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
# Térmico: mismo criterio que RGB (vistazo/rápido → --fast-orthophoto).
# Confirmado en vivo (mision_2026-08-08, vistazo+escarpado): el térmico
# pagó ~3.3 h SOLO en DensifyPointCloud (openmvs) para un sensor de
# 640×512 (0.33 MP) cuyo producto final es el ortomosaico en °C — la nube
# densa no alimenta ningún producto (no pide --dsm). Con fast-orthophoto la
# malla del render nativo de ODM sale de la nube dispersa de SfM (mismo
# mecanismo que banda D), que a esa escala es suficiente.
FAST_ORTHOPHOTO_THERMAL="$FAST_ORTHOPHOTO_RGB"

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

if [[ "$MODE" != "rgb" && "$MODE" != "rgb+thermal" && "$MODE" != "thermal" && "$MODE" != "thermal-convert" && "$MODE" != "none" ]]; then
  echo "❌ ERROR: MODE debe ser 'rgb', 'rgb+thermal', 'thermal', 'thermal-convert' o 'none' (recibido: $MODE)"; exit 1
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
# MODE=thermal-convert: solo convierte R-JPEG a GeoTIFF Float32 calibrado
# en °C para procesar en fotogrametría externa (Agisoft Metashape, Pix4D, Terra).
RUN_RGB=1
[[ "$MODE" == "none" || "$MODE" == "thermal" || "$MODE" == "thermal-convert" ]] && RUN_RGB=0
# Corre el vuelo RGB/térmico completo en rgb+thermal Y en thermal — en
# thermal no se reconstruye el RGB, pero SOURCE_DIR sigue siendo la misma
# carpeta del vuelo y hay que organizarla igual para llegar a las fotos
# térmicas.
RUN_THERMAL=0
[[ "$MODE" == "rgb+thermal" || "$MODE" == "thermal" ]] && RUN_THERMAL=1
if [[ "$RUN_RGB" -eq 0 && "$RUN_THERMAL" -eq 0 && "$RUN_MULTISPECTRAL" -eq 0 && "$MODE" != "thermal-convert" ]]; then
  echo "❌ ERROR: MODE=none (sin vuelo RGB/térmico) requiere un vuelo multiespectral"
  echo "   en MS_SOURCE_DIR ('$MS_SOURCE_DIR'), pero ese directorio no existe."
  exit 1
fi

case "${1:-run}" in
  serve)
    # Ver una misión ya procesada. Es la misma webapp: sirve el geovisor en
    # /geovisor/ y además trae el HUD de progreso y el muestreo por punto.
    # Había un servidor aparte para esto (geovisor/serve.py) que
    # reimplementaba una parte de lo mismo.
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
    if [[ -d "$SOURCE_DIR" ]]; then
      N_RGB=$(find -L "$SOURCE_DIR" -type f \( -iname "*_V.JPG" -o -iname "*_W.JPG" \) 2>/dev/null | wc -l)
      if [[ "$N_RGB" -eq 0 ]]; then
        N_RGB=$(find -L "$SOURCE_DIR" -type f \( -iname "*.JPG" -o -iname "*.JPEG" -o -iname "*.PNG" \) ! -iname "*_T.*" ! -iname "*_D.*" ! -iname "*_MS_*" 2>/dev/null | wc -l)
      fi
    fi
    [[ -d "$SOURCE_DIR" ]] && N_TH=$(find -L "$SOURCE_DIR" -type f -iname "*_T.JPG" 2>/dev/null | wc -l)
    [[ -d "$MS_SOURCE_DIR" ]] && N_MS=$(find -L "$MS_SOURCE_DIR" -type f -iname "*_MS_NIR.TIF" 2>/dev/null | wc -l)
    # Modo urgencia (SUB_SAMPLE=N): ODM va a procesar 1 de cada N, así que la
    # estimación usa el conteo REAL de lo que correrá, no el de la tarjeta.
    # Multiespectral NO se submuestrea (ver el comentario grande junto a
    # `python3 scripts/subsample_photos.py` más abajo — se fragmenta con
    # menos solape) así que su conteo se deja completo acá también.
    if [[ "${SUB_SAMPLE:-0}" -gt 1 ]]; then
      N_RGB=$((N_RGB / SUB_SAMPLE)); N_TH=$((N_TH / SUB_SAMPLE))
    fi
    python3 -c "
import sys
sys.path.insert(0, 'scripts')
from hardware import estimate_message
m = estimate_message('$PRESET', $N_RGB + $N_TH + $N_MS * 4, terreno='$TERRENO',
                     por_sensor={'rgb': $N_RGB, 'thermal': $N_TH, 'multispectral': $N_MS * 4})
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
      && ( ( "$RUN_RGB" -eq 1 && "$FAST_ORTHOPHOTO_RGB" != "1" ) \
           || ( "$RUN_THERMAL" -eq 1 && "$FAST_ORTHOPHOTO_THERMAL" != "1" ) \
           || ( "$RUN_MULTISPECTRAL" -eq 1 && "$FAST_ORTHOPHOTO_RGB" != "1" ) ) \
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
  echo "   la etapa densa. Alternativa sin GPU: PRESET=vistazo o rapido"
  echo "   (RGB y térmico usan --fast-orthophoto y no tocan DensifyPointCloud),"
  echo "   o una misión de banda D."
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

if [[ "$MODE" == "thermal-convert" ]]; then
  TH_COUNT=$(ls data/termica_mosaico/*_T.JPG 2>/dev/null | wc -l || true)
  if [[ "$TH_COUNT" -eq 0 ]]; then
    echo "❌ ERROR: MODE=thermal-convert pero no hay imágenes térmicas (*_T.JPG) en ${SOURCE_DIR}."
    exit 1
  fi
  echo "   Térmico: ${TH_COUNT} imágenes (modo conversión directa)"
elif [[ "$RUN_RGB" -eq 1 || "$RUN_THERMAL" -eq 1 ]]; then
  # RGB siempre se exige acá aunque MODE=thermal no lo reconstruya: es la
  # misma carpeta del vuelo M3T/H20T, y su ausencia señala "carpeta
  # equivocada" tanto como en cualquier otro modo (mismo criterio que
  # webapp/main.py::_validate() y el chequeo del lado del navegador).
  RGB_COUNT=$(ls data/rgb_mosaico/*_V.JPG data/rgb_mosaico/*_W.JPG 2>/dev/null | wc -l || true)
  if [[ "$RGB_COUNT" -eq 0 ]]; then
    RGB_COUNT=$(find data/rgb_mosaico -maxdepth 1 -type f \( -iname "*.JPG" -o -iname "*.JPEG" -o -iname "*.PNG" -o -iname "*.TIF" -o -iname "*.TIFF" \) 2>/dev/null | wc -l || true)
  fi
  [[ "$RGB_COUNT" -eq 0 ]] && { echo "❌ ERROR: No hay imágenes RGB en ${SOURCE_DIR}"; exit 1; }
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

# Ruta de vuelo EN SEGUNDO PLANO, apenas las fotos están organizadas.
# export_flight_path.py lee GPS/tiempo del EXIF de
# data/{rgb,termica,multiespectral}_mosaico (ya completos acá arriba), así
# que no necesita nada de la preparación ni de ODM — y nada de la
# reconstrucción la necesita a ella: sus únicos consumidores
# (compute_flight_quality, compute_coverage, situation-summary,
# export-products) corren mucho después, en el análisis cruzado.
#
# Corría SINCRÓNICA acá y era puro tiempo serial al frente de la corrida:
# medido en vivo, 10 min 54 s sobre 2335 fotos con TODO lo demás parado
# esperándola — el 7% de una misión de 2 h 46 min, antes de que el primer
# sensor empezara siquiera a preparar. Se lanza al fondo y se cosecha antes
# del análisis cruzado (ver _wait_flight_path más abajo), que es el primero
# que de verdad la necesita.
mkdir -p outputs/logs
_FLIGHT_PATH_STATUS="outputs/logs/.flight_path_status"
rm -f "$_FLIGHT_PATH_STATUS"
(
  set +e
  make flight-path
  echo "$?" > "$_FLIGHT_PATH_STATUS"
) &
_PID_FLIGHT_PATH=$!
_FLIGHT_PATH_OK=1
_wait_flight_path() {
  if [[ -n "${_PID_FLIGHT_PATH:-}" ]]; then
    wait "$_PID_FLIGHT_PATH" 2>/dev/null || true
    _PID_FLIGHT_PATH=""
  fi
  while [[ ! -f "$_FLIGHT_PATH_STATUS" ]]; do sleep 0.2; done
  if [[ "$(cat "$_FLIGHT_PATH_STATUS" 2>/dev/null)" == "0" ]]; then
    _FLIGHT_PATH_OK=1
  else
    _FLIGHT_PATH_OK=0
    PIPELINE_HAD_FAILURE=1
    echo "❌ ERROR: falló la ruta de vuelo (outputs/flight_path.geojson)."
  fi
}

if [[ "$MODE" == "thermal-convert" ]]; then
  echo ""
  echo "═══ RAPTOR: Modo Solo Conversión Térmica (R-JPEG → GeoTIFF Float32 / °C) ═══"
  echo "   Extrayendo datos radiométricos para Agisoft Metashape, Pix4D o DJI Terra..."
  mkdir -p outputs/thermal_converted outputs/logs

  # Ejecutar conversión usando DJI Thermal SDK
  make sdk-convert

  # Copiar TIFFs convertidos a outputs/thermal_converted
  cp -a preprocessing/thermal_dji_sdk/*.tif outputs/thermal_converted/ 2>/dev/null || true
  N_CONV=$(ls outputs/thermal_converted/*.tif 2>/dev/null | wc -l || echo 0)

  # Esperar a que termine la ruta de vuelo
  _wait_flight_path

  # Generar LEEME para software de fotogrametría
  cat <<'EOF' > outputs/thermal_converted/LEEME_FOTOGRAMETRIA.txt
================================================================================
RAPTOR - IMÁGENES TÉRMICAS RADIOMÉTRICAS CONVERTIDAS (Float32 / °C)
================================================================================
Estas imágenes fueron convertidas directamente desde los R-JPEG de DJI usando
el DJI Thermal SDK v1.8 y etiquetadas con metadatos EXIF / GPS completos.

ESPECIFICACIONES:
- Formato: GeoTIFF Float32 (1 banda, valores en grados Celsius °C).
- Metadatos EXIF embebidos en cada TIFF:
  * Posición GPS completa (Latitud, Longitud, Altitud WGS84 y Altitud Relativa).
  * Orientación del gimbal y del dron (Yaw, Pitch, Roll).
  * Marca, modelo de cámara y distancia focal (Make, Model, FocalLength).
  * Fecha y hora precisa de captura (DateTimeOriginal, SubSecTimeOriginal).

COMPATIBILIDAD CON SOFTWARE DE FOTOGRAMETRÍA:
1. Agisoft Metashape:
   - Importar carpeta 'thermal_converted' como Photos / Cameras.
   - Metashape detectará automáticamente las coordenadas GPS y ángulos de orientación.
   - En Camera Calibration, seleccionar tipo de cámara (Frame) y calibrar normalmente.
   - Al construir el Ortomosaico, seleccionar la banda Float32 para mantener los valores
     de temperatura en °C.
2. Pix4Dmapper / Pix4Dmatic:
   - Crear proyecto seleccionando las imágenes de esta carpeta.
   - Seleccionar plantilla térmica o procesar como cámara estándar con geotags.
3. DJI Terra / DroneDeploy / WebODM:
   - Cargar los archivos .tif como conjunto de imágenes aéreas.
4. QGIS / ArcGIS:
   - Cada archivo individual puede cargarse como capa ráster con escala de temperatura.
================================================================================
EOF

  echo "   Empaquetando outputs/termicas_convertidas_tiff.zip..."
  (cd outputs && zip -q -r termicas_convertidas_tiff.zip thermal_converted/)

  python3 -c "
import json, time, os, glob
tifs = glob.glob('outputs/thermal_converted/*.tif')
summary = {
    'ok': True,
    'modo': 'thermal-convert',
    'total_convertidos': len(tifs),
    'fecha': time.strftime('%Y-%m-%d %H:%M:%S'),
    'salidas': [
        'outputs/termicas_convertidas_tiff.zip',
        'outputs/thermal_converted/',
        'outputs/flight_path.geojson'
    ]
}
with open('outputs/run_summary.json', 'w') as f:
    json.dump(summary, f, indent=2)
"

  if [[ -n "${EXPORT_DIR:-}" && -d "${EXPORT_DIR:-}" ]]; then
    echo "   Copiando productos a carpeta de entrega: $EXPORT_DIR"
    cp -a outputs/termicas_convertidas_tiff.zip "$EXPORT_DIR/" 2>/dev/null || true
    cp -a outputs/thermal_converted "$EXPORT_DIR/" 2>/dev/null || true
  fi

  echo ""
  echo "✅ Conversión térmica completada: ${N_CONV} imágenes procesadas."
  echo "   Descarga ZIP disponible: outputs/termicas_convertidas_tiff.zip"
  exit 0
fi

# Hardware detectado + qué implica el preset elegido, ANTES de arrancar ODM.
# Mismo texto que puede pedirse sin lanzar nada:
#   python3 scripts/hardware.py estimate --preset "$PRESET" --photos N
# Total de fotos = todas las que ODM va a procesar de verdad (RGB + térmico +
# multiespectral cuentan cada una su propio SfM), no una mezcla rara. Con
# SUB_SAMPLE>1 (modo urgencia) se usa el conteo reducido y se avisa —
# multiespectral queda FUERA de esa reducción (no se submuestrea, ver el
# comentario grande junto a `python3 scripts/subsample_photos.py` más abajo).
_N_RGB_EST=${RGB_COUNT:-0}; _N_TH_EST=${TH_COUNT:-0}; _N_MS_EST=$(( ${MS_COUNT:-0} * 4 ))
if [[ "$SUB_SAMPLE" -gt 1 ]]; then
  _N_RGB_EST=$((_N_RGB_EST / SUB_SAMPLE)); _N_TH_EST=$((_N_TH_EST / SUB_SAMPLE))
fi
_TOTAL_FOTOS=$(( _N_RGB_EST + _N_TH_EST + _N_MS_EST ))
echo ""
python3 -c "
import json, sys
sys.path.insert(0, 'scripts')
from hardware import estimate_message
m = estimate_message('$PRESET', $_TOTAL_FOTOS,
                     por_sensor={'rgb': $_N_RGB_EST, 'thermal': $_N_TH_EST,
                                 'multispectral': $_N_MS_EST})
hw = m['hardware']
print('🖥️  Hardware detectado: {} núcleos, {} — {}'.format(
    hw['cores'],
    f\"{hw['mem_available_mb']/1024:.0f} GB RAM libres\" if hw['mem_available_mb'] else 'RAM no detectada',
    (hw['gpu_name'] or 'GPU disponible') if hw['gpu'] else 'sin GPU (corre en CPU)'))
print('🎚️  ' + m['modelo_texto'])
print('📐 ' + m['resolucion_texto'])
print('⏱️  ' + m['tiempo_texto'])
"
[[ "$SUB_SAMPLE" -gt 1 ]] && echo "   ⚡ Modo urgencia: se procesarán 1 de cada $SUB_SAMPLE fotos — las originales quedan intactas en la fuente."
echo ""

# ── Concurrencia acotada por MEMORIA ────────────────────────────────
# ODM documenta ~1 GB por hilo cada 2 MP y por defecto usa todos los núcleos:
# en una máquina grande con fotos grandes eso se traduce en decenas de GB y
# el kernel mata el proceso sin dejar traza (pasó con el band alignment del
# M3M). La cuenta vive en scripts/hardware.py — bash no puede hacer esta
# aritmética con megapíxeles fraccionarios sin arrastrar redondeo, y así la
# reusa también la webapp para estimar tiempos.
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
  # Los tiles parciales NO se llevan todos los núcleos: mientras esto corre
  # el sensor siguiente puede estar en pleno SfM (con su concurrencia
  # liviana), y gdal2tiles con NPROCS completos le pelearía la CPU (ver
  # RAPTOR_TILES_PROCESSES en scripts/generate_tiles.py). Con la mitad de los
  # núcleos el teselado tarda más pero el SfM en curso no se degrada; el
  # `make tiles` final corre SIN esta env y usa la máquina entera. El sello
  # de "al día" hace que lo ya teselado acá no se repita en el pase final.
  RAPTOR_TILES_PROCESSES=$(python3 -c "import os;print(max(1, os.cpu_count() // 2))") \
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
  # $3/$4: concurrencia liviana (SfM) / "segura" (MVS). ODM solo acepta UN
  # --max-concurrency por invocación, así que para darle a cada fase la suya
  # hay que partir la corrida en dos.
  #
  # DÓNDE SE PARTE, Y POR QUÉ AHÍ (esto estuvo mal y costó horas por misión):
  # el único lugar que escribe opensfm/config.yaml —con la concurrencia que
  # de verdad usan detect_features/match/reconstruct/undistort— es
  # OSFMContext.setup(), y lo llama la etapa **opensfm**, no la etapa dataset;
  # y solo lo (re)escribe si image_list.txt todavía no existe. Con el corte
  # anterior (`--end-with dataset`) la fase 1 nunca creaba image_list.txt, así
  # que era la fase 2 —la PESADA— la que fijaba el config: el split hacía
  # exactamente lo contrario de lo que buscaba. Cortando EN opensfm, la fase 2
  # revisita la etapa sin rerun y con image_list.txt ya presente, así que no
  # pisa el ajuste de la fase 1. OpenMVS lee args.max_concurrency directo.
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
DO_ENTREGA=0; [[ -n "${EXPORT_DIR:-}" ]] && DO_ENTREGA=1
# confidence-mask es "RGB ∩ térmico" — confidence_mask.py abre
# outputs/rgb_orthomosaic.tif sin chequear que exista, y en MODE=thermal
# (RUN_RGB=0) nunca existe. Sin este gate, toda misión solo-térmico
# terminaba en un traceback acá mismo, después de horas de ODM.
#
# Además de eso: confidence_mask.tif no lo usa NADA más en el pipeline —
# no aparece en
# coincide el nombre con un indicador de calidad de vuelo sin relación). Su
# único consumidor real es la exportación opcional a la carpeta de entrega
# (EXPORT_PRODUCTS=confidence). Calcularlo siempre que hay RGB+térmico,
# tenga o no sentido para ESTA corrida, es trabajo tirado en el caso común
# (sin entrega, o con entrega mismo pero sin pedir ese producto puntual)
# — reportado en vivo. Se gatea también contra eso.
_export_incluye() {
  local clave="$1" lista="${EXPORT_PRODUCTS:-all}"
  [[ ",${lista}," == *",all,"* || ",${lista}," == *",${clave},"* ]]
}
DO_CONFIANZA=0
if [[ "$RUN_RGB" -eq 1 && "$DO_THERMAL" -eq 1 && "$DO_ENTREGA" -eq 1 ]] && _export_incluye confidence; then
  DO_CONFIANZA=1
fi
# Reconstrucción + recorte + exportación: UNA sola etapa en el contador
# aunque adentro corran varios sensores en simultáneo (mismo criterio que ya
# usaba el recorte) — el detalle real se ve en los sub-encabezados que cada
# make target ya imprime por su cuenta.
DO_TRIM=0; [[ "$RUN_RGB" -eq 1 || "$DO_THERMAL" -eq 1 || "$DO_MS" -eq 1 || "$DO_DBAND" -eq 1 ]] && DO_TRIM=1

STAGE_FLAGS=("$DO_TRIM" "$DO_CONFIANZA" 1 1 "$DO_ENTREGA")
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
    # Mismo criterio que RGB: sin etapa densa el pico de memoria por hilo cae
    # (ya no hay depthmaps a resolución nativa simultáneos).
    multispectral) if [[ "$FAST_ORTHOPHOTO_RGB" == "1" ]]; then echo 2.5; else echo 5; fi ;;
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
  local EXTREME=""
  if [[ "${PRESET_NOMBRE:-}" == "vistazo" || "${PRESET_NOMBRE:-}" == "tactico" ]]; then
    EXTREME="--mesh-size 20000 --mesh-octree-depth 7"
  fi
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
           "$([[ "$FAST_ORTHOPHOTO_RGB" == "1" ]] && echo --fast-orthophoto)"\
           "$EXTREME" ;;
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
      # --fast-orthophoto (solo vistazo/rápido, ver FAST_ORTHOPHOTO_THERMAL):
      # salta DensifyPointCloud — en vistazo+escarpado real esa etapa sola se
      # llevó ~3.3 h de las 6.2 h del térmico, para una nube densa que
      # ningún producto usa. El render nativo (malla+textura) se mantiene.
      echo "--feature-quality $FEAT_QUALITY --radiometric-calibration camera"\
           "--orthophoto-resolution $ODM_RES_CM --crop 0"\
           "--min-num-features $MIN_FEATURES --matcher-neighbors $MATCHER_NEIGHBORS"\
           "--pc-quality $PC_QUALITY --sfm-algorithm $SFM_ALGORITHM --skip-report"\
           "$([[ "$FAST_ORTHOPHOTO_THERMAL" == "1" ]] && echo --fast-orthophoto)"\
           "$EXTREME" ;;
    multispectral)
      # --radiometric-calibration camera+sun: usa el sensor de sol embebido en
      # cada banda (DJI M3M) para calibrar a reflectancia sin panel físico.
      # ODM agrupa las 4 bandas por captura vía el tag XMP Camera:BandName —
      # nada que armar de nuestro lado, ya viene correcto en los TIFF del M3M.
      # --fast-orthophoto con el mismo criterio que RGB/térmico (lo fija el
      # preset): ningún producto multiespectral consume la nube densa —
      # ortomosaico e índices (NDVI/GNDVI/NDRE/MSAVI2) salen igual de la
      # dispersa. Sin esto el multiespectral quedaba como nuevo camino
      # crítico apenas RGB dejó de correr MVS: 60 min contra los ~50 de RGB.
      echo "--feature-quality $FEAT_QUALITY --radiometric-calibration camera+sun"\
           "--dsm --dem-resolution $ODM_RES_CM --crop 0 --dem-gapfill-steps 3"\
           "--min-num-features $MIN_FEATURES --matcher-neighbors $MATCHER_NEIGHBORS"\
           "--pc-quality $PC_QUALITY --sfm-algorithm $SFM_ALGORITHM --skip-report"\
           "$([[ "$FAST_ORTHOPHOTO_RGB" == "1" ]] && echo --fast-orthophoto)"\
           "$EXTREME" ;;
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
    rgb)
      make clean-dsm && make trim-edges-dsm && make trim-edges-rgb
      ;;
    thermal)
      make trim-edges-thermal
      # Resumen de situación TEMPRANO: apenas el térmico está recortado se
      # generan el hotspot y situation.json para que el geovisor muestre los
      # focos activos mientras los demás sensores siguen reconstruyendo — en
      # una emergencia los focos llegan antes. El análisis cruzado (etapa 5)
      # corre estrictamente DESPUÉS de todas las cadenas de sensor y los
      # vuelve a generar (ver más abajo) una vez que flight_path.geojson está
      # garantizado listo, por si esta primera pasada corrió antes de tiempo.
      make compute-thermal-hotspot
      make situation-summary
      python3 scripts/notify_alert.py || true
      ;;
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
      make compute-indices
      make classify-vegetation-indices ;;
    dband)   make trim-edges-dband ;;
  esac
}
# Exportación cloud-optimized de LO QUE PRODUJO este sensor — COG para sus
# rásters, COPC para su nube de puntos georreferenciada — apenas termina SU
# post-procesamiento, no al final de toda la misión. El COG de cada archivo
# es idempotente (export_cog.py se saltea lo que ya sea COG en el pase de
# seguridad final), así que no hay costo por teselar/exportar temprano acá Y
# tener igual el pase final que cubre lo que cruza sensores (confidence_mask,
# hotspot, etc. — ver el análisis cruzado más abajo).
# COG y COPC son productos independientes (un ráster, una nube de puntos) —
# si uno falla igual se intenta el otro, y se reporta fallo si CUALQUIERA de
# los dos falló. Con un `&&`/`set -e` ingenuo, un COG roto salteaba el COPC
# entero sin necesidad: son artefactos distintos con causas de falla distintas.
_odm_export() {
  local ok=0
  case "$1" in
    rgb) python3 scripts/export_cog.py outputs/rgb_orthomosaic.tif outputs/dsm.tif || ok=1 ;;
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

# ── Independencia real entre etapas ─────────────────────────────────
# Cada etapa espera SOLO a los sensores que necesita, nunca a todos. Antes un
# único `wait` sobre las cuatro cadenas serializaba dos cosas distintas:
#   · fallo — un sensor caído cortaba etapas que no dependían de él (banda D
#     tumbaba la exportación de RGB y térmico ya terminados);
#   · tiempo — hotspot térmico (térmico, listo a 1 h 20) arrancaba recién a
#     las 2 h 34 esperando a RGB, del que no depende. Medido en palmas.
#
# El estado de cada sensor viaja por ARCHIVO (outputs/logs/.sensor_estado_X,
# lo escribe el trap de su subshell) y no por `wait $pid`: las etapas del
# análisis cruzado corren cada una en su propio subshell para no bloquearse
# entre sí, y bash solo permite `wait` sobre hijos DIRECTOS — un subshell no
# puede esperar al pid de otro. Mismo patrón que FALLOFLAG, por lo mismo.
# PIPELINE_HAD_FAILURE acumula para el código de salida final.
declare -A _SENSOR_OK=()
declare -A _SENSOR_WAITED=()
PIPELINE_HAD_FAILURE=0
_sensor_status_file() { echo "outputs/logs/.sensor_estado_${1}"; }
_wait_sensor() {
  local lbl="$1"
  [[ " ${ODM_ORDEN[*]:-} " == *" $lbl "* ]] || return 0
  [[ "${_SENSOR_WAITED[$lbl]:-0}" -eq 1 ]] && return 0
  local f; f=$(_sensor_status_file "$lbl")
  while [[ ! -f "$f" ]]; do sleep 0.2; done
  _SENSOR_WAITED[$lbl]=1
  if [[ "$(cat "$f" 2>/dev/null)" == "0" ]]; then
    _SENSOR_OK[$lbl]=1
  else
    _SENSOR_OK[$lbl]=0
    PIPELINE_HAD_FAILURE=1
  fi
}
_dep_ok() {
  local lbl="$1"
  if [[ " ${ODM_ORDEN[*]:-} " == *" $lbl "* ]]; then
    _wait_sensor "$lbl"
    [[ "${_SENSOR_OK[$lbl]:-0}" -eq 1 ]]
  fi
}

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

  for _i in "${!ODM_ORDEN[@]}"; do
    _label="${ODM_ORDEN[$_i]}"
    _hilos_light="${ODM_HILOS_LIGHT[$_i]}"
    _hilos="${ODM_HILOS[$_i]}"
    (
      set -e
      # Trap único, instalado ANTES de _odm_prep (no después de agarrar la
      # ficha del semáforo, como antes): tiene que cubrir un fallo en
      # CUALQUIER punto de la cadena, incluida la preparación — si el
      # archivo de estado no se escribiera en ese caso, _wait_sensor() de
      # cualquier otro subshell que pregunte por este sensor esperaría ese
      # archivo PARA SIEMPRE. _liberado empieza en 1 (todavía no se agarró
      # ficha) para que _liberar_cupo no libere una ficha que nunca se tomó.
      _liberado=1
      _liberar_cupo() { [[ "$_liberado" -eq 1 ]] && return; _liberado=1; printf '\n' >&"$SEM_FD" 2>/dev/null || true; }
      _marcar_salida() {
        local _codigo=$?
        _liberar_cupo
        echo "$_codigo" > "$(_sensor_status_file "$_label")" 2>/dev/null || true
      }
      trap _marcar_salida EXIT

      _t_prep0=$(date +%s)
      _odm_prep "$_label"
      # Submuestreo de emergencia: 1 de cada N fotos, apenas termina SU
      # preparación. El set completo queda intacto en data/.
      # EXCEPTO multispectral: medido en vivo (terreno escarpado,
      # SUB_SAMPLE=3) RGB y térmico conservaron el 99% de sus fotos
      # reconstruidas pero el MS apenas el 15% (93/608) — sus bandas son
      # monocromáticas, con features débiles, y al bajar el solape el SfM se
      # fragmentó en 8 componentes sueltos. Se prioriza cobertura real.
      if [[ "${SUB_SAMPLE:-0}" -gt 1 && "$_label" != "multispectral" ]]; then
        echo "--- ${_label}: modo urgencia — procesando 1 de cada $SUB_SAMPLE fotos ---"
        python3 scripts/subsample_photos.py "processing/$(_odm_proyecto "$_label")" "$SUB_SAMPLE" || true
      elif [[ "${SUB_SAMPLE:-0}" -gt 1 ]]; then
        echo "--- ${_label}: modo urgencia activo, pero SIN submuestrear este sensor — su reconstrucción se fragmenta con menos solape (medido en vivo) ---"
      fi
      echo "⏱  Preparación de ${_label} completa en $(( $(date +%s) - _t_prep0 ))s"

      # Ficha del semáforo — bloquea ACÁ si los cupos de reconstrucción están
      # ocupados, no antes: la preparación de arriba ya corrió sin esperar a
      # nadie. _liberar_cupo y su trap ya están instalados desde el principio
      # (ver arriba) — acá solo se marca que HAY algo que liberar.
      read -u "$SEM_FD"
      _liberado=0

      if [[ -f "$FALLOFLAG" ]]; then
        echo "⏭  ${_label}: se salta — otra reconstrucción de esta misión ya falló"
        exit 1
      fi

      if [[ -n "${_FLIGHT_PATH_STATUS:-}" && -f "$_FLIGHT_PATH_STATUS" ]]; then
        if [[ "$(cat "$_FLIGHT_PATH_STATUS" 2>/dev/null)" != "0" ]]; then
          echo "❌ ${_label}: se cancela — falló la validación inicial de ruta de vuelo/extensión geográfica."
          touch "$FALLOFLAG"
          exit 1
        fi
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
      #
      # REINTENTO: reportado en vivo, dos veces en misiones reales distintas
      # —multiespectral las dos— ODM/OpenSfM abortó con un "No such file or
      # directory" sobre un archivo que en el momento de revisar SÍ existía
      # en disco (una vez en mvs_texturing sobre un TIFF undistorted, otra
      # en el undistort de OpenSfM sobre un TIFF fuente, ambas vía workers
      # paralelos de joblib/loky). No es un bug de este repo — pasa adentro
      # de OpenSfM/rasterio, código de la imagen base, no de acá — y las dos
      # veces el archivo estaba perfectamente bien; todo apunta a un hiccup
      # transitorio de I/O bajo la carga concurrente pesada que este
      # pipeline genera a propósito (varios sensores reconstruyendo a la vez,
      # cada uno con su propio pool de workers). run.py es resumible (cada
      # ODM_Stage detecta sola lo que ya está hecho y lo saltea) así que un
      # reintento inmediato retoma cerca de donde cortó, no repite la
      # reconstrucción entera.
      _ODM_REINTENTOS=2
      _odm_intento=1
      _odm_ok=0
      while [[ "$_odm_intento" -le "$_ODM_REINTENTOS" ]]; do
        if ( run_odm "$_label" "$(_odm_proyecto "$_label")" "$_hilos_light" "$_hilos" \
              $(_odm_args "$_label") "${HYBRID_BA_FLAG[@]}" ); then
          _odm_ok=1
          break
        fi
        if [[ "$_odm_intento" -lt "$_ODM_REINTENTOS" ]]; then
          echo "⚠ ODM ${_label}: falló el intento ${_odm_intento}/${_ODM_REINTENTOS} — reintentando (posible hiccup transitorio de I/O, ver comentario arriba)"
        fi
        _odm_intento=$((_odm_intento + 1))
      done
      if [[ "$_odm_ok" -eq 0 ]]; then
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
  done

  # SIN wait colectivo acá a propósito (ver el comentario grande de más
  # arriba, problema 2): cada sensor se espera LAZY, on-demand, la primera
  # vez que confianza/área/flight-quality/etc. preguntan por él vía
  # _dep_ok() — que ahora lee el archivo de estado que cada subshell escribe
  # al salir (ver el trap _marcar_salida más arriba), no un `wait $pid` que
  # solo el hilo principal podría hacer. El cierre del semáforo, el resumen
  # de fallos y el sourcing de tiempos quedan más abajo, DESPUÉS del análisis
  # cruzado — ahí sí hace falta que los cuatro sensores estén resueltos
  # (aunque no hayan hecho falta antes) para que tiles/COG/entrega vean el
  # estado final real.
elif [[ "$DO_TRIM" -eq 1 ]]; then
  # SKIP_ODM=1 (reusar lo ya reconstruido): sin reconstrucción que correr, el
  # recorte + exportación de los sensores presentes igual arrancan todos
  # juntos entre sí en vez de uno atrás de otro — mismo _odm_post/_odm_export
  # que usa el camino con ODM, así que el caso "sin RGB" de multiespectral
  # (limpieza de DSM propia) se resuelve exactamente igual en los dos casos.
  declare -A _REUSE_PID_LABEL=()
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
      _REUSE_PID_LABEL[$!]="$_label"
    fi
  done
  for pid in "${!_REUSE_PID_LABEL[@]}"; do
    _label="${_REUSE_PID_LABEL[$pid]}"
    # Ya esperado acá mismo: marcado para que _wait_sensor()/_dep_ok() más
    # abajo no intenten un `wait` de nuevo sobre un pid ya cosechado (error
    # en bash) — este camino (reuso) no necesita el `wait` lazy del camino
    # con ODM: son operaciones livianas, todas terminan rápido de todos modos.
    _SENSOR_WAITED[$_label]=1
    if wait "$pid"; then
      _SENSOR_OK[$_label]=1
    else
      _SENSOR_OK[$_label]=0
      PIPELINE_HAD_FAILURE=1
    fi
  done
  if [[ "$PIPELINE_HAD_FAILURE" -eq 1 ]]; then
    echo "⚠ ADVERTENCIA: falló al menos un recorte/exportación de post-procesamiento — se continúa con lo que sí terminó bien."
    for _label in rgb thermal multispectral dband; do
      [[ "${_SENSOR_OK[$_label]:-1}" -eq 0 ]] && echo "   ❌ ${_label}: falló"
    done
  fi
fi

[[ "$DO_TRIM" -eq 1 ]] && pipeline_progress_done "Reconstrucción y recortes listos"

# Reloj de "post" recién ACÁ, después del bloque de reconstrucción de
# arriba: antes T_POST_START se seteaba ANTES de la reconstrucción y la
# etapa "post" del resumen de tiempos tragaba TODO el ODM — en una corrida
# completa con ODM (barbosa-chorrera: ~9.5 h de reconstrucción) habría
# reportado ~10.5 h de "post" y nada decía dónde estaba el tiempo real.
T_POST_START=$(date +%s)

# La ruta de vuelo se lanzó al fondo al principio de la corrida (ver arriba);
# acá es donde recién hace falta de verdad — compute_flight_quality lee
# outputs/flight_path.geojson para la velocidad de vuelo. En la práctica ya
# terminó hace rato (tarda minutos, la reconstrucción tarda horas), así que
# esto no bloquea nada: solo garantiza el orden.
if declare -f _wait_flight_path >/dev/null; then
  _wait_flight_path
fi

# ── Análisis cruzado ─────────────────────────────────────────────────
# confidence-mask es de SOLO LECTURA: confidence_mask.py lee
# outputs/{rgb,thermal}_orthomosaic.tif (ya definitivos — el único recorte
# espacial de la misión es el casco convexo de trim_low_overlap_edges.py, que
# ya corrió) y solo escribe outputs/confidence_mask.tif, que nadie más lee.
# Antes reescribía esos mismos ortomosaicos in-place con su propia limpieza
# morfológica (motas + parches sueltos), lo que era una condición de carrera
# real contra flight-quality/área corriendo en paralelo — eliminada junto con
# esa limpieza (ver confidence_mask.py).
#
# Antes TAMBIÉN corría sincrónica y primera en el hilo principal del script
# (tenía sentido cuando reescribía archivos compartidos: nadie más podía
# tocarlos mientras tanto). Sin esa razón, correrla sincrónica-primera pasó
# a ser un problema nuevo: confianza SÍ depende de RGB (rgb+térmico), así
# que un `_dep_ok rgb` sincrónico en el hilo principal bloqueaba TODO lo que
# viene después en el script —incluida área, que NO depende de RGB— detrás
# de la reconstrucción de RGB, mucho más lenta (SfM incremental, secuencial
# por diseño). Reportado en vivo. Por eso ahora confianza se lanza en su
# propio subshell de fondo, igual que flight-quality/área — cada una bloquea
# solo contra lo que a ELLA le corresponde, nunca contra las demás.
# Las tres etapas de acá abajo lanzan su subshell SIEMPRE que están pedidas
# (DO_X==1) — la pregunta "¿mi dependencia terminó bien?" (_dep_ok) se
# evalúa DENTRO de cada subshell, no antes de lanzarlo, para que evaluarla
# no bloquee el hilo principal (ver el comentario grande de _wait_sensor()
# más arriba sobre por qué esto importa: confianza sí depende de RGB, y
# evaluar esa pregunta en el hilo principal bloqueaba a área detrás de RGB
# también, aunque área no lo necesite). Cada subshell hace `exit 1` si su
# propia dependencia falló — el wait loop de más abajo lo trata igual que
# cualquier otro fallo real.
declare -A _CROSS_PID_LABEL=()
if [[ "$DO_CONFIANZA" -eq 1 ]]; then
  stage_begin "Máscara de confianza"
  (
    if _dep_ok rgb && _dep_ok thermal; then
      make confidence-mask && python3 scripts/export_cog.py outputs/confidence_mask.tif
    else
      echo "⏭  Máscara de confianza: se salta — depende de RGB y térmico, y al menos uno falló."
      exit 1
    fi
  ) &
  _CROSS_PID_LABEL[$!]="máscara de confianza"
fi
if [[ "$DO_THERMAL" -eq 1 ]]; then
  # Calidad del LEVANTAMIENTO (solape de cámaras, velocidad de vuelo, % de
  # imágenes reconstruidas) — no depende de multiespectral, así que corre
  # siempre que haya térmico. Liviano — no tiene stage_begin propio.
  (
    if _dep_ok thermal; then
      make flight-quality
      # Regenera hotspot+situation.json una vez que TODOS los sensores
      # terminaron y flight_path.geojson está garantizado listo (ver
      # _wait_flight_path más arriba) — la primera pasada de _odm_post()
      # puede haber corrido antes de que la fecha de captura estuviera
      # disponible.
      make compute-thermal-hotspot
      make situation-summary
    else
      echo "⏭  Calidad de vuelo: se salta — depende del térmico, que falló."
      exit 1
    fi
  ) &
  _CROSS_PID_LABEL[$!]="calidad de vuelo"
fi

_CONFIANZA_OK=1
for pid in "${!_CROSS_PID_LABEL[@]}"; do
  if ! wait "$pid"; then
    PIPELINE_HAD_FAILURE=1
    echo "❌ ERROR: falló ${_CROSS_PID_LABEL[$pid]}."
    case "${_CROSS_PID_LABEL[$pid]}" in
      "máscara de confianza")      _CONFIANZA_OK=0 ;;
    esac
  fi
done
[[ "$DO_CONFIANZA" -eq 1 && "$_CONFIANZA_OK" -eq 1 ]] && pipeline_progress_done "Máscara de confianza lista"
[[ "${#_CROSS_PID_LABEL[@]}" -gt 0 ]] && publish_partial

# ── Cierre: acá SÍ hace falta que los cuatro sensores estén resueltos ──
# Ninguna etapa de arriba (confianza/flight-quality/área) tuvo por qué
# esperar a TODOS — cada una esperó solo lo suyo, lazy, vía _dep_ok(). Pero
# tiles/COG/entrega sí publican TODO lo que haya, así que antes de esas
# etapas se termina de esperar cualquier sensor que ninguna de las de arriba
# haya necesitado (p.ej. RGB si la misión no tiene confianza, o banda D, que
# nadie del análisis cruzado pide nunca) — sin esto, ese sensor podría seguir
# corriendo de fondo cuando tiles ya está publicando.
for _label in "${ODM_ORDEN[@]}"; do _wait_sensor "$_label"; done
[[ -n "${SEM_FD:-}" ]] && exec {SEM_FD}<&-

# Tiempos por sensor: cada cadena los dejó en su propio archivo — un
# subshell de fondo no puede exportar variables de vuelta al padre.
for _label in "${ODM_ORDEN[@]}"; do
  _f="outputs/logs/.timing_${_label}"
  [[ -f "$_f" ]] && source "$_f"
done
rm -f outputs/logs/.timing_* outputs/logs/.sensor_estado_* "${FALLOFLAG:-outputs/logs/.odm_fallo}"

if [[ "$PIPELINE_HAD_FAILURE" -eq 1 ]]; then
  echo ""
  echo "⚠ ADVERTENCIA: al menos un sensor o etapa falló durante la corrida — se continúa con lo que sí terminó bien."
  for _label in "${ODM_ORDEN[@]}"; do
    [[ "${_SENSOR_OK[$_label]:-1}" -eq 0 ]] && echo "   ❌ ${_label}: falló (ver outputs/logs/odm_${_label}.log)"
  done
  echo "   El detalle de cada fallo está más arriba."
fi

_ANY_SENSOR_OK=0
for _label in "${ODM_ORDEN[@]}"; do
  [[ "${_SENSOR_OK[$_label]:-0}" -eq 1 ]] && _ANY_SENSOR_OK=1
done

if [[ "$_ANY_SENSOR_OK" -eq 0 && ${#ODM_ORDEN[@]} -gt 0 ]]; then
  echo ""
  echo "❌ ERROR FATAL: Ningún sensor completó la reconstrucción exitosamente."
  echo "   Cancelando generación de tiles y exportación."
  exit 1
fi

# Cobertura vs. área volada (outputs/coverage.json): advierte si la
# reconstrucción dejó el mosaico recortado muy por debajo del área real
# volada — el síntoma del bug de planar en terreno con relieve (terminaba
# "bien" con 23-45% de cobertura sin ningún error visible). Corre en
# segundos y nunca corta la corrida (|| true).
python3 scripts/compute_coverage.py || true

# Alerta de focos activos (webhook, si está configurado) + informe de
# emergencia HTML imprimible — best-effort, nunca cortan la corrida.
# RAPTOR_MISSION: la webapp la pasa; en CLI se deriva de la carpeta de
# entrega (o queda 'mision').
export RAPTOR_MISSION="${RAPTOR_MISSION:-$(basename "${EXPORT_DIR:-/input}" 2>/dev/null || echo mision)}"
python3 scripts/notify_alert.py || true
python3 scripts/export_report.py || true

stage_begin "Generación de tiles XYZ"
if make tiles; then
  pipeline_progress_done "Tiles listos para el geovisor"
else
  echo "❌ ERROR: falló la generación de tiles."
  PIPELINE_HAD_FAILURE=1
fi

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
  PIPELINE_HAD_FAILURE=1
else
  pipeline_progress_done "Rasters COG y nubes COPC listos"
fi

# Entrega opcional a la carpeta que eligió el usuario (formato + CRS propios).
if [[ "$DO_ENTREGA" -eq 1 ]]; then
  stage_begin "Exportación a carpeta de entrega"
  if make export-products; then
    pipeline_progress_done "Entrega exportada a ${EXPORT_DIR}"
  else
    echo "❌ ERROR: falló la exportación a la carpeta de entrega."
    PIPELINE_HAD_FAILURE=1
  fi
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
# El código refleja PIPELINE_HAD_FAILURE (no un 0 fijo): con etapas
# independientes la corrida puede llegar hasta acá con un fallo/salteo parcial
# en el medio (ver _dep_ok más arriba) — antes eso era imposible porque
# cualquier fallo cortaba con exit 1 mucho antes de llegar a este resumen.
python3 scripts/run_summary.py "$PIPELINE_HAD_FAILURE" || echo "  ⚠ no se pudo generar run_summary.json"

echo ""
echo "═══════════════════════════════════════════════════"
if [[ "$PIPELINE_HAD_FAILURE" -eq 1 ]]; then
  echo "  ⚠ Pipeline terminado con fallos parciales (MODE=${MODE})"
else
  echo "  ✅ Pipeline completo (MODE=${MODE})"
fi
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

# Código de salida real de la corrida — SOLO se llega acá con SERVE=0 (el
# caso normal orquestado por la webapp, ver core/runner.py::PipelineRun, que
# lee este returncode para marcar la corrida como fallida en la UI). Con
# SERVE=1 el `exec` de arriba ya reemplazó este proceso, así que un fallo
# parcial en modo standalone (`docker run ... raptor run`, sin webapp
# orquestando) igual sirve el geovisor con lo que sí se pudo — mejor eso que
# no mostrar nada, que es lo que pasaba antes con el primer exit 1.
if [[ "$PIPELINE_HAD_FAILURE" -eq 1 ]]; then
  exit 1
fi
exit 0
