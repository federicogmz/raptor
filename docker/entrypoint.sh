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
# ya existentes), `shell` (debug). Hubo un modo `tui` (misma interacción que
# `webapp` pero en terminal, sin navegador) — retirado jul 2026, la webapp
# lo reemplaza por completo y sin ella era una segunda UI para mantener sin
# ganancia real.
#
# Multiespectral (DJI M3M u otro dron distinto, misma zona): montar un
# SEGUNDO volumen aparte de /input (es otro vuelo/sensor, no se mezcla) —
#   -v /ruta/vuelo-multiespectral:/input_ms
# Si /input_ms no está montado, este módulo ni se toca (cero impacto en
# misiones RGB+térmico existentes).
#
# Térmico: se procesa con el renderizador NATIVO de ODM (malla 3D +
# textura + ortofoto real), no un blending heurístico propio — ver
# scripts/prepare_thermal_native_odm.py. Reemplaza el pipeline anterior
# (orthorectify_thermal_rigorous.py/poisson_seam_blend.py, retirados jul
# 2026 tras compararse contra una entrega de referencia de Agisoft:
# el pipeline propio daba 16.9% de cobertura con artefactos de
# fragmentación: el nativo da 68.6%, sin artefactos, más resolución).
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
export MODE VERBOSE

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
    exec python3 geovisor/serve.py "$PORT"
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
    echo "─── últimas 150 líneas ───"
    tail -150 "$logfile"
    exit $status
  fi
}

if [[ "$SKIP_ODM" -eq 0 ]]; then
  if [[ "$RUN_RGB" -eq 1 ]]; then
    echo "--- [2/5] ODM RGB (SfM + DSM + ortofoto) ---"
    run_odm rgb rgb_odm \
      --feature-quality high --orthophoto-resolution 2 \
      --dsm --dem-resolution 8 --crop 0 --dem-gapfill-steps 3 \
      --min-num-features 12000 --matcher-neighbors 0 --pc-quality low \
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
    run_odm thermal thermal_native_odm \
      --feature-quality high --radiometric-calibration camera \
      --orthophoto-resolution 10 --crop 0 \
      --min-num-features 8000 --matcher-neighbors 0 --pc-quality medium \
      --skip-report --rerun-from dataset
  fi

  if [[ "$RUN_MULTISPECTRAL" -eq 1 ]]; then
    echo "--- [3b/5] ODM Multiespectral (SfM + calibración + ortofoto multibanda) ---"
    # --radiometric-calibration camera+sun: usa el sensor de sol embebido en
    # cada banda (DJI M3M) para calibrar a reflectancia sin panel físico.
    # ODM agrupa las 4 bandas por captura vía el tag XMP Camera:BandName —
    # nada que armar de nuestro lado, ya viene correcto en los TIFF del M3M.
    run_odm multispectral multispectral_odm \
      --feature-quality high --radiometric-calibration camera+sun \
      --dsm --dem-resolution 8 --crop 0 --dem-gapfill-steps 3 \
      --min-num-features 12000 --matcher-neighbors 0 --pc-quality low \
      --skip-report --rerun-from dataset
    publish_partial
  fi
fi

STAGE_START=2; [[ "$SKIP_ODM" -eq 0 ]] && STAGE_START=4
echo "--- [${STAGE_START}/5] Recorte + tiles ---"
# Se cuenta exactamente una etapa por cada pipeline_progress_start de abajo:
# 2 fijas (tiles + export) + recorte base + las condicionales. (Antes esto
# arrancaba en 5 y contaba de más cuando no había multiespectral: la barra
# llegaba a "4/5" y terminaba, sin la etapa faltante.)
TOTAL_STAGES=3
[[ "$MODE" == "rgb+thermal" ]] && TOTAL_STAGES=$((TOTAL_STAGES + 1))
[[ "$RUN_MULTISPECTRAL" -eq 1 ]] && TOTAL_STAGES=$((TOTAL_STAGES + 1))
[[ "$RUN_MULTISPECTRAL" -eq 1 && "$MODE" == "rgb+thermal" ]] && TOTAL_STAGES=$((TOTAL_STAGES + 1))
STAGE=1

if [[ "$RUN_RGB" -eq 1 ]]; then
  pipeline_progress_start "Limpieza DSM + recorte RGB" $STAGE $TOTAL_STAGES
  make clean-dsm
  make trim-edges-dsm
  make trim-edges-rgb
  pipeline_progress_done "DSM limpio y recortado, RGB recortado"
else
  # Sin vuelo RGB/térmico el DSM sale del proyecto multiespectral (también
  # corre con --dsm). Son los MISMOS scripts, solo apuntados por env a
  # processing/multispectral_odm — la limpieza y el recorte por solape de
  # cámaras no dependen de qué sensor produjo la reconstrucción.
  pipeline_progress_start "Limpieza + recorte del DSM (multiespectral)" $STAGE $TOTAL_STAGES
  DSM_SRC=processing/multispectral_odm/odm_dem/dsm.tif make clean-dsm
  ODM_RGB_DIR=processing/multispectral_odm make trim-edges-dsm
  pipeline_progress_done "DSM limpio y recortado"
fi
STAGE=$((STAGE + 1))

if [[ "$MODE" == "rgb+thermal" ]]; then
  pipeline_progress_start "Recorte térmico + máscara de confianza" $STAGE $TOTAL_STAGES
  make trim-edges-thermal
  make confidence-mask
  pipeline_progress_done "Bordes térmicos recortados, máscara lista"
  STAGE=$((STAGE + 1))
fi

if [[ "$RUN_MULTISPECTRAL" -eq 1 ]]; then
  pipeline_progress_start "Recorte multiespectral + índices de vegetación" $STAGE $TOTAL_STAGES
  make trim-edges-multispectral
  make compute-indices
  pipeline_progress_done "NDVI/GNDVI/NDRE generados"
  STAGE=$((STAGE + 1))
  # A partir de acá el geovisor ya puede mostrar bandas multiespectrales e
  # índices de vegetación — publicar sin esperar a severidad/hotspot.
  publish_partial

  # Área afectada + severidad: necesita multiespectral Y térmico (el
  # brillo multiespectral + la anomalía térmica son las dos señales que
  # separan quemado de suelo desnudo/vías — ver detect_area_afectada.py).
  if [[ "$MODE" == "rgb+thermal" ]]; then
    pipeline_progress_start "Área afectada + clasificación de severidad" $STAGE $TOTAL_STAGES
    make detect-area-afectada
    make compute-severity
    make situation-summary
    pipeline_progress_done "Área afectada y severidad listas"
    STAGE=$((STAGE + 1))
    publish_partial
  fi
fi

pipeline_progress_start "Generación de tiles XYZ" $STAGE $TOTAL_STAGES
make tiles
pipeline_progress_done "Tiles listos para el geovisor"
STAGE=$((STAGE + 1))

pipeline_progress_start "Exportación cloud-optimized (COG + COPC)" $STAGE $TOTAL_STAGES
make export-cog
make export-copc
pipeline_progress_done "Rasters COG y nubes COPC listos"

# Todo lo que escribe este contenedor queda dueño de root (corre como root
# adentro), lo que deja los productos ilegibles para el usuario del host —
# ArcGIS/QGIS abiertos desde Windows vía \\wsl$\ los reportan como "no
# válidos" en vez de un error de permisos claro. Se abre a todos los
# usuarios al final de cada corrida, no solo una vez a mano.
chmod -R a+rwX outputs preprocessing processing geovisor/tiles 2>/dev/null || true

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
  echo "🌐 Geovisor: http://localhost:${PORT}"
  exec python3 geovisor/serve.py "$PORT"
fi
