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
# Sin GPU disponible en el host, omitir --gpus: ODM detecta nvidia-smi
# en tiempo de ejecución y cae a CPU automáticamente (más lento, sin
# cambios de configuración).
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
#   QUALITY         0-100 (default 75). Detalle del modelo de superficie (qué
#                    tan bien se resuelven copas de árboles y bordes) y techo
#                    de resolución del ortomosaico/DSM — nunca más fino que el
#                    GSD real del vuelo. Tabla completa en scripts/hardware.py;
#                    `python3 scripts/hardware.py estimate --quality N --photos
#                    N` muestra a qué resolución y en cuánto tiempo se espera
#                    que salga, sin arrancar nada.
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

MODE="${MODE:-rgb+thermal}"
SOURCE_DIR="${SOURCE_DIR:-/input}"
MS_SOURCE_DIR="${MS_SOURCE_DIR:-/input_ms}"
SKIP_ODM="${SKIP_ODM:-0}"
PORT="${PORT:-8080}"
SERVE="${SERVE:-1}"
VERBOSE="${VERBOSE:-0}"
QUALITY="${QUALITY:-75}"
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
if ! [[ "$QUALITY" =~ ^[0-9]+$ ]] || [[ "$QUALITY" -gt 100 ]]; then
  echo "❌ ERROR: QUALITY debe ser un entero 0-100 (recibido: $QUALITY)"; exit 1
fi
_TIER_JSON=$(python3 scripts/hardware.py quality-tier "$QUALITY")
PC_QUALITY=$(python3 -c "import json,sys;print(json.loads(sys.argv[1])['pc_quality'])" "$_TIER_JSON")
FEAT_QUALITY=$(python3 -c "import json,sys;print(json.loads(sys.argv[1])['feature_quality'])" "$_TIER_JSON")
ODM_RES_CM=$(python3 -c "import json,sys;print(json.loads(sys.argv[1])['res_cm'])" "$_TIER_JSON")

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

if [[ "$MODE" != "rgb" && "$MODE" != "rgb+thermal" && "$MODE" != "none" ]]; then
  echo "❌ ERROR: MODE debe ser 'rgb', 'rgb+thermal' o 'none' (recibido: $MODE)"; exit 1
fi

RUN_MULTISPECTRAL=0
[[ -d "$MS_SOURCE_DIR" ]] && RUN_MULTISPECTRAL=1

# MODE=none: misión sin vuelo RGB/térmico (el M3M es otro dron, puede volarse
# solo). Sin él tampoco habría nada que procesar, así que se exige el MS.
RUN_RGB=1
[[ "$MODE" == "none" ]] && RUN_RGB=0
if [[ "$RUN_RGB" -eq 0 && "$RUN_MULTISPECTRAL" -eq 0 ]]; then
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
    [[ -d "$SOURCE_DIR" ]] && N_RGB=$(find "$SOURCE_DIR" -type f \( -iname "*_V.JPG" -o -iname "*_W.JPG" \) 2>/dev/null | wc -l)
    [[ -d "$SOURCE_DIR" ]] && N_TH=$(find "$SOURCE_DIR" -type f -iname "*_T.JPG" 2>/dev/null | wc -l)
    [[ -d "$MS_SOURCE_DIR" ]] && N_MS=$(find "$MS_SOURCE_DIR" -type f -iname "*_MS_NIR.TIF" 2>/dev/null | wc -l)
    python3 -c "
import sys
sys.path.insert(0, 'scripts')
from hardware import estimate_message
m = estimate_message($QUALITY, $N_RGB + $N_TH + $N_MS * 4)
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

if [[ "$RUN_RGB" -eq 1 && ! -d "$SOURCE_DIR" ]]; then
  echo "❌ ERROR: SOURCE_DIR ('$SOURCE_DIR') no existe. Montá la carpeta de la misión, p.ej.:"
  echo "   docker run -v /ruta/mision:/input ..."
  exit 1
fi

source scripts/progress.sh

echo "═══════════════════════════════════════════════════"
echo "  Pipeline Mosaico — MODE=${MODE}"
[[ "$RUN_RGB" -eq 1 ]] && echo "  Fuente: ${SOURCE_DIR}"
[[ "$RUN_MULTISPECTRAL" -eq 1 ]] && echo "  Fuente multiespectral: ${MS_SOURCE_DIR}"
echo "═══════════════════════════════════════════════════"

if [[ "$RUN_RGB" -eq 1 ]]; then
  echo "--- [0/5] Organizando imágenes desde: ${SOURCE_DIR} ---"
  ./docker/setup-data.sh "$SOURCE_DIR"

  RGB_COUNT=$(ls data/rgb_mosaico/*_V.JPG data/rgb_mosaico/*_W.JPG 2>/dev/null | wc -l || true)
  [[ "$RGB_COUNT" -eq 0 ]] && { echo "❌ ERROR: No hay imágenes RGB (*_V.JPG / *_W.JPG) en ${SOURCE_DIR}"; exit 1; }
  echo "   RGB: ${RGB_COUNT} imágenes"

  if [[ "$MODE" == "rgb+thermal" ]]; then
    TH_COUNT=$(ls data/termica_mosaico/*_T.JPG 2>/dev/null | wc -l || true)
    if [[ "$TH_COUNT" -eq 0 ]]; then
      echo "❌ ERROR: MODE=rgb+thermal pero no hay imágenes térmicas (*_T.JPG) en ${SOURCE_DIR}."
      echo "   Agregá las fotos térmicas del vuelo, o corré con MODE=rgb para procesar solo RGB."
      exit 1
    fi
    echo "   Térmico: ${TH_COUNT} imágenes"
  fi
fi

if [[ "$RUN_MULTISPECTRAL" -eq 1 ]]; then
  echo "--- [0b/5] Organizando bandas multiespectrales desde: ${MS_SOURCE_DIR} ---"
  ./docker/setup-data-multispectral.sh "$MS_SOURCE_DIR"
  MS_COUNT=$(ls data/multiespectral_mosaico/*_MS_NIR.TIF 2>/dev/null | wc -l || true)
  [[ "$MS_COUNT" -eq 0 ]] && { echo "❌ ERROR: No hay bandas multiespectrales en data/multiespectral_mosaico/"; exit 1; }
  echo "   Multiespectral: ${MS_COUNT} capturas (4 bandas c/u)"
fi

# Hardware detectado + qué implica la calidad elegida, ANTES de arrancar ODM.
# Mismo texto que puede pedirse sin lanzar nada:
#   python3 scripts/hardware.py estimate --quality "$QUALITY" --photos N
# Total de fotos = todas las que ODM va a procesar de verdad (RGB + térmico +
# multiespectral cuentan cada una su propio SfM), no una mezcla rara.
_TOTAL_FOTOS=$(( ${RGB_COUNT:-0} + ${TH_COUNT:-0} + ${MS_COUNT:-0} * 4 ))
echo ""
python3 -c "
import json, sys
sys.path.insert(0, 'scripts')
from hardware import estimate_message
m = estimate_message($QUALITY, $_TOTAL_FOTOS)
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

echo "--- [1/5] Preparación (GPS de EXIF) ---"
pipeline_header
[[ "$RUN_RGB" -eq 1 ]] && make prepare-rgb
[[ "$RUN_MULTISPECTRAL" -eq 1 ]] && make prepare-multispectral

if [[ "$MODE" == "rgb+thermal" ]]; then
  # La conversión radiométrica (SDK DJI → °C) y el re-encoding para ODM
  # nativo tienen que pasar ANTES de invocar ODM (el proyecto térmico
  # nativo se arma sobre esos TIFF, no sobre los R-JPEG crudos).
  make sdk-convert
  make denoise-thermal
  make prepare-thermal-native
fi

# Ruta de vuelo ANTES de ODM: da algo real que mirar en el geovisor durante
# la hora que tarda la reconstrucción (recorrido, capturas, extensión) en vez
# de una pantalla vacía hasta el final.
make flight-path

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
mkdir -p outputs/logs

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
# ~12 MP) y en térmico, así que ahora se acota a las tres.
#
# La cuenta vive en scripts/hardware.py (única fuente — bash no puede hacer
# esta aritmética con megapíxeles fraccionarios como los del sensor térmico,
# 0.33 MP, sin arrastrar errores de redondeo). Delegar además la deja
# reusable desde la webapp (Python) para el mismo mensaje de estimación de
# tiempo que ve el usuario antes de arrancar.
safe_concurrency() {
  python3 scripts/hardware.py concurrency "${1:-2}"
}

run_odm() {
  local label="$1" name="$2"; shift 2
  local logfile="outputs/logs/odm_${label}.log"
  set +e
  (cd /code && python3 run.py --project-path /app/processing "$name" "$@") 2>&1 \
    | python3 /app/scripts/odm_progress_filter.py "ODM ${label^^}" "$logfile"
  local status=${PIPESTATUS[0]}
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

if [[ "$SKIP_ODM" -eq 0 ]]; then
  if [[ "$RUN_RGB" -eq 1 ]]; then
    echo "--- [2/5] ODM RGB (SfM + DSM + ortofoto) ---"
    echo "    calidad ${QUALITY}% → pc-quality=${PC_QUALITY}, feature-quality=${FEAT_QUALITY}"
    # 12.3 MP (fotos de 4056x3040 del H20T/M3T wide): concurrencia acotada por
    # RAM con el mismo criterio que ya protegía al multiespectral.
    RGB_CONCURRENCY=$(safe_concurrency 12.3)
    echo "    concurrencia: ${RGB_CONCURRENCY} hilos (de $(nproc) núcleos, acotado por RAM disponible)"
    run_odm rgb rgb_odm \
      --feature-quality "$FEAT_QUALITY" --orthophoto-resolution "$ODM_RES_CM" \
      --dsm --dem-resolution "$ODM_RES_CM" --crop 0 --dem-gapfill-steps 3 \
      --min-num-features 12000 --matcher-neighbors 0 --pc-quality "$PC_QUALITY" \
      --max-concurrency "$RGB_CONCURRENCY" \
      --skip-report --rerun-from dataset
    # Vista previa del mosaico RGB apenas ODM lo renderiza (~25 min), sin
    # esperar a que termine el resto de la corrida (~1 h).
    publish_partial
  fi

  if [[ "$MODE" == "rgb+thermal" ]]; then
    echo "--- [3/5] ODM Térmico nativo (SfM + calibración + malla + ortofoto) ---"
    # --radiometric-calibration camera: dispara la conversión Kelvin×100→°C
    # nativa de ODM para DJI H20T (opendm/thermal.py) sobre los TIFF que
    # prepare_thermal_native_odm.py ya re-encodeó + etiquetó (Make=DJI,
    # Model=ZH20T, XMP Camera:BandName=LWIR). Sin --skip-orthophoto: acá
    # es donde ODM renderiza la malla 3D con la textura calibrada — el
    # ortomosaico térmico REAL sale de esta etapa, no de un script propio.
    # El sensor térmico es de 640x512, así que su GSD es ~10x más grueso que el
    # del RGB: pedir 1 cm no lo mejora (ODM recorta al GSD real igual) pero sí
    # evita fijar un número que quede corto en un vuelo más bajo.
    # 0.33 MP (sensor térmico 640x512): con imágenes tan chicas el límite real
    # casi siempre son los núcleos, no la RAM — igual se pasa por la misma
    # cuenta para no dejar una etapa sin acotar por costumbre.
    TH_CONCURRENCY=$(safe_concurrency 0.33)
    run_odm thermal thermal_native_odm \
      --feature-quality "$FEAT_QUALITY" --radiometric-calibration camera \
      --orthophoto-resolution "$ODM_RES_CM" --crop 0 \
      --min-num-features 8000 --matcher-neighbors 0 --pc-quality "$PC_QUALITY" \
      --max-concurrency "$TH_CONCURRENCY" \
      --skip-report --rerun-from dataset
  fi

  if [[ "$RUN_MULTISPECTRAL" -eq 1 ]]; then
    echo "--- [3b/5] ODM Multiespectral (SfM + calibración + ortofoto multibanda) ---"
    # --radiometric-calibration camera+sun: usa el sensor de sol embebido en
    # cada banda (DJI M3M) para calibrar a reflectancia sin panel físico.
    # ODM agrupa las 4 bandas por captura vía el tag XMP Camera:BandName —
    # nada que armar de nuestro lado, ya viene correcto en los TIFF del M3M.
    #
    # --max-concurrency acotado por RAM: el band alignment de esta etapa carga
    # dos bandas completas (5 MP) por hilo y ODM usaría todos los núcleos, lo
    # que en una máquina con muchos cores pide decenas de GB y termina en un
    # SIGKILL sin traceback. Ver safe_concurrency() arriba.
    MS_CONCURRENCY=$(safe_concurrency 5)
    echo "    concurrencia: ${MS_CONCURRENCY} hilos ($(nproc) núcleos, acotado por RAM disponible)"
    run_odm multispectral multispectral_odm \
      --feature-quality "$FEAT_QUALITY" --radiometric-calibration camera+sun \
      --dsm --dem-resolution "$ODM_RES_CM" --crop 0 --dem-gapfill-steps 3 \
      --min-num-features 12000 --matcher-neighbors 0 --pc-quality "$PC_QUALITY" \
      --max-concurrency "$MS_CONCURRENCY" \
      --skip-report --rerun-from dataset
    publish_partial
  fi
fi

STAGE_START=2; [[ "$SKIP_ODM" -eq 0 ]] && STAGE_START=4
echo "--- [${STAGE_START}/5] Recorte + tiles ---"

# ── Etapas de post-procesamiento ────────────────────────────────────
# Qué etapas corren en ESTA misión se decide una sola vez acá, y esa misma
# decisión se usa dos veces: para el total de la barra de progreso y para
# ejecutar (o no) cada bloque de abajo. Antes el total era una suma aparte que
# repetía las condiciones a mano, y bastaba con agregar una etapa y olvidarse
# de sumarla para que la barra terminara en "4/5".
DO_THERMAL=0; [[ "$MODE" == "rgb+thermal" ]] && DO_THERMAL=1
DO_MS=0;      [[ "$RUN_MULTISPECTRAL" -eq 1 ]] && DO_MS=1
# Área afectada + severidad necesita AMBAS señales: el brillo multiespectral y
# la anomalía térmica son las dos que separan quemado de suelo desnudo o vías.
DO_AREA=0;    [[ "$DO_MS" -eq 1 && "$DO_THERMAL" -eq 1 ]] && DO_AREA=1
DO_ENTREGA=0; [[ -n "${EXPORT_DIR:-}" ]] && DO_ENTREGA=1

# Un elemento por etapa, en el mismo orden en que corren. Los `1` son las que
# siempre están (recorte base, tiles, export cloud-optimized); sin número
# mágico que actualizar por separado.
STAGE_FLAGS=(1 "$DO_THERMAL" "$DO_MS" "$DO_AREA" 1 1 "$DO_ENTREGA")
TOTAL_STAGES=0
for _f in "${STAGE_FLAGS[@]}"; do TOTAL_STAGES=$((TOTAL_STAGES + _f)); done

# Avanza el contador solo: ningún bloque tiene que acordarse de incrementarlo.
STAGE=0
stage_begin() {
  STAGE=$((STAGE + 1))
  pipeline_progress_start "$1" "$STAGE" "$TOTAL_STAGES"
}

if [[ "$RUN_RGB" -eq 1 ]]; then
  stage_begin "Limpieza DSM + recorte RGB"
  make clean-dsm
  make trim-edges-dsm
  make trim-edges-rgb
  pipeline_progress_done "DSM limpio y recortado, RGB recortado"
else
  # Sin vuelo RGB/térmico el DSM sale del proyecto multiespectral (también
  # corre con --dsm). Son los MISMOS scripts, solo apuntados por env a
  # processing/multispectral_odm — la limpieza y el recorte por solape de
  # cámaras no dependen de qué sensor produjo la reconstrucción.
  stage_begin "Limpieza + recorte del DSM (multiespectral)"
  DSM_SRC=processing/multispectral_odm/odm_dem/dsm.tif make clean-dsm
  ODM_RGB_DIR=processing/multispectral_odm make trim-edges-dsm
  pipeline_progress_done "DSM limpio y recortado"
fi

if [[ "$DO_THERMAL" -eq 1 ]]; then
  stage_begin "Recorte térmico + máscara de confianza"
  make trim-edges-thermal
  make confidence-mask
  # Calidad del LEVANTAMIENTO (solape de cámaras, velocidad de vuelo, % de
  # imágenes reconstruidas) — no depende de multiespectral, así que corre
  # acá siempre, no solo en el bloque solo-térmico de más abajo. Necesita
  # outputs/rgb_orthomosaic.tif (ya recortado, bloque RUN_RGB de arriba) y
  # outputs/thermal_orthomosaic.tif (recién recortado en esta misma línea).
  make flight-quality
  if [[ "$DO_MS" -eq 0 ]]; then
    # El hotspot es puramente térmico (temperatura absoluta) y no depende de
    # NDVI ni del polígono de área afectada — pero antes SOLO se generaba
    # como subproducto de compute-severity, que exige multiespectral (su señal
    # primaria es NDVI). Una misión RGB+térmico sin M3M se quedaba sin esta
    # capa sin ninguna necesidad real. Con multiespectral, el bloque de más
    # abajo (DO_AREA) ya genera un hotspot recortado al área detectada, más
    # específico — no se pisa acá.
    make compute-thermal-hotspot
    # situation.json en modo SOLO térmico: sin multiespectral no hay área
    # afectada ni severidad que resumir (ver compute_situation_summary.py),
    # pero la fecha de vuelo (de flight_path.geojson, ya escrita antes de
    # ODM) y los focos activos del hotspot recién generado sí se pueden
    # reportar — antes esta misión nunca tenía situation.json, así que ni
    # siquiera la fecha de captura llegaba a mostrarse en el geovisor.
    make situation-summary
  fi
  pipeline_progress_done "Bordes térmicos recortados, máscara lista"
fi

if [[ "$DO_MS" -eq 1 ]]; then
  stage_begin "Recorte multiespectral + índices de vegetación"
  make trim-edges-multispectral
  make compute-indices
  pipeline_progress_done "NDVI/GNDVI/NDRE generados"
  # A partir de acá el geovisor ya puede mostrar bandas multiespectrales e
  # índices de vegetación — publicar sin esperar a severidad/hotspot.
  publish_partial

  if [[ "$DO_AREA" -eq 1 ]]; then
    stage_begin "Área afectada + clasificación de severidad"
    make detect-area-afectada
    make compute-severity
    make situation-summary
    pipeline_progress_done "Área afectada y severidad listas"
    publish_partial
  fi
fi

stage_begin "Generación de tiles XYZ"
make tiles
pipeline_progress_done "Tiles listos para el geovisor"

stage_begin "Exportación cloud-optimized (COG + COPC)"
make export-cog
make export-copc
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
[[ "$MODE" == "rgb+thermal" ]] && TILE_LAYERS="$TILE_LAYERS thermal"
[[ "$RUN_MULTISPECTRAL" -eq 1 ]] && TILE_LAYERS="$TILE_LAYERS ndvi gndvi ndre"
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
