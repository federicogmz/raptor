#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# setup-data-multispectral.sh — Organiza las bandas multiespectrales (DJI
# M3M u otro dron multiespectral) desde la carpeta fuente del vuelo.
#
# Uso:
#   ./docker/setup-data-multispectral.sh /media/usb/DCIM
#   ./docker/setup-data-multispectral.sh ~/vuelo_ms_2024/
#
# Busca recursivamente las 4 bandas *_MS_G.TIF, *_MS_R.TIF, *_MS_RE.TIF,
# *_MS_NIR.TIF y las copia a data/multiespectral_mosaico/. También organiza
# la banda "D" (cámara RGB aparte del M3M, no coalineada con las 4 lentes
# MS) a data/dband_mosaico/, si aparece — es opcional: si no hay *_D.JPG no
# pasa nada, el resto del módulo multiespectral sigue igual. Se procesa como
# su propio mosaico visible independiente (scripts/prepare_dband_odm.py +
# ODM propio), no se mezcla con las 4 bandas espectrales.
# ═════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
DATA_DIR="$REPO_DIR/data/multiespectral_mosaico"
DATA_DIR_D="$REPO_DIR/data/dband_mosaico"
# Confirmado en vivo como cuello de botella real: un vuelo M3M mediano son
# miles de TIF (2340 en la corrida donde se encontró esto), copiados uno por
# uno con `cp` secuencial — ~1 seg/archivo, con el resto de la máquina
# ocioso mientras tanto. PERO: paralelizar esto a lo bruto (todos los
# núcleos, sin mirar RAM) coincidió con un crash por memoria de toda la PC
# del usuario en la corrida donde se probó — mismo riesgo que ODM ya conoce
# para este sensor (M3M, bandas de 5 MP, ver el comentario de
# safe_concurrency() en entrypoint.sh/scripts/hardware.py: "~2.5 GB por
# hilo... con el kernel matando el proceso sin dejar ninguna traza"). Se usa
# la misma cuenta memory-aware en vez de `nproc` a secas (hardware.py ya
# respeta MAX_CONCURRENCY como vía de escape manual, igual que en ODM).
NPROCS="$(python3 "$REPO_DIR/scripts/hardware.py" concurrency 5 2>/dev/null || echo 4)"

if [[ $# -lt 1 ]]; then
  echo "Uso: $0 <directorio_fuente>"
  exit 1
fi

SRC="$1"

if [[ ! -d "$SRC" ]]; then
  echo "❌ ERROR: '$SRC' no existe o no es un directorio."
  exit 1
fi

echo "═══ Organizando bandas multiespectrales desde: $SRC"
echo ""

mkdir -p "$DATA_DIR"

# find -L: ver el comentario largo en docker/setup-data.sh sobre por qué
# hace falta (SRC puede ser un symlink de import_local(), y find sin -L
# no lo sigue cuando es el argumento de partida sin "/" al final).
echo "Buscando bandas MS (*_MS_G.TIF, *_MS_R.TIF, *_MS_RE.TIF, *_MS_NIR.TIF) ..."
MS_FILES=$(find -L "$SRC" -type f \( -iname "*_MS_G.TIF" -o -iname "*_MS_R.TIF" \
  -o -iname "*_MS_RE.TIF" -o -iname "*_MS_NIR.TIF" \) 2>/dev/null || true)
MS_COUNT=$(echo "$MS_FILES" | grep -c "TIF" || true)

if [[ "$MS_COUNT" -eq 0 ]]; then
  echo "  ⚠ No se encontraron bandas multiespectrales."
else
  echo "  Encontradas: $MS_COUNT"
  # Filtra primero lo que YA está (copia incremental en un rerun), y recién
  # ahí paraleliza la copia de lo que falta — xargs -P, no un `cp` por
  # iteración de un while secuencial.
  # `if`, no `[[ ]] && echo`: con set -e, el exit status de un `while` es el
  # de su ÚLTIMO comando ejecutado — si el último archivo que entrega find
  # YA existe (típico al reanudar), `[[ ! -f ]]` da falso y ESE exit status
  # tumba todo el script en el `TO_COPY=$(...)` de abajo, sin ningún mensaje
  # de error (bug real, ver el mismo fix en docker/setup-data.sh). `if` sin
  # `else` siempre sale 0.
  TO_COPY=$(while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    base=$(basename "$f")
    if [[ ! -f "$DATA_DIR/$base" ]]; then
      echo "$f"
    fi
  done <<< "$MS_FILES")
  COPIED=$(echo "$TO_COPY" | grep -c . || true)
  if [[ "$COPIED" -gt 0 ]]; then
    echo "$TO_COPY" | xargs -P "$NPROCS" -I{} cp --reflink=auto {} "$DATA_DIR/"
  fi
  echo "  ✅ $COPIED copiadas a data/multiespectral_mosaico/"
fi

# ── Banda D (RGB, opcional) ──────────────────────────────────────────
# A diferencia de las 4 bandas espectrales, ausente acá NO es un error: no
# todos los vuelos M3M la usan (o el usuario solo quiere NDVI/severidad, sin
# el mosaico visible rápido). Ver scripts/prepare_dband_odm.py.
echo ""
echo "Buscando banda D (*_D.JPG, cámara RGB del M3M) ..."
mkdir -p "$DATA_DIR_D"
D_FILES=$(find -L "$SRC" -type f -iname "*_D.JPG" 2>/dev/null || true)
D_COUNT=$(echo "$D_FILES" | grep -c "JPG" || true)
if [[ "$D_COUNT" -eq 0 ]]; then
  echo "  (sin banda D en esta carpeta — opcional, no es un error)"
else
  echo "  Encontradas: $D_COUNT"
  D_TO_COPY=$(while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    base=$(basename "$f")
    if [[ ! -f "$DATA_DIR_D/$base" ]]; then
      echo "$f"
    fi
  done <<< "$D_FILES")
  D_COPIED=$(echo "$D_TO_COPY" | grep -c . || true)
  if [[ "$D_COPIED" -gt 0 ]]; then
    echo "$D_TO_COPY" | xargs -P "$NPROCS" -I{} cp --reflink=auto {} "$DATA_DIR_D/"
  fi
  echo "  ✅ $D_COPIED copiadas a data/dband_mosaico/"
fi

echo ""
echo "═══ Resumen ──────────────────────────────────"
# `ls patrón-sin-coincidencias | wc -l` devuelve != 0 y bajo `set -euo
# pipefail` MATABA el script acá mismo, sin imprimir ningún error: una misión
# a la que le faltara UNA banda moría a mitad del resumen y el usuario no
# tenía forma de saber por qué. `find` no falla cuando no encuentra nada.
MISSING=""
declare -A BAND_N
for b in G R RE NIR; do
  n=$(find "$DATA_DIR" -maxdepth 1 -iname "*_MS_${b}.TIF" 2>/dev/null | wc -l)
  BAND_N[$b]=$n
  echo "  Banda ${b}: ${n} imágenes"
  [[ "$n" -eq 0 ]] && MISSING="${MISSING} ${b}"
done
echo "  Banda D (RGB, opcional): $(find "$DATA_DIR_D" -maxdepth 1 -iname "*_D.JPG" 2>/dev/null | wc -l) imágenes"

if [[ -n "$MISSING" ]]; then
  echo ""
  echo "❌ ERROR: faltan bandas multiespectrales:${MISSING}"
  echo "   ODM necesita las 4 (G, R, RE, NIR) para agrupar cada captura."
  echo "   Revisá que la carpeta del vuelo M3M esté completa (archivos"
  echo "   *_MS_G.TIF, *_MS_R.TIF, *_MS_RE.TIF, *_MS_NIR.TIF)."
  exit 1
fi

# Las 4 bandas tienen que tener EXACTAMENTE la misma cantidad de archivos. ODM
# empareja las bandas de cada captura y aborta si a alguna le falta su
# compañera, pero recién adentro de compute_band_maps() — o sea después del
# SfM completo, con un mensaje genérico sobre CaptureUUID. Una captura
# incompleta (tarjeta llena, vuelo cortado, copia a medias) cuesta así una hora
# de procesamiento para terminar en un error críptico; acá cuesta segundos y
# dice exactamente qué captura revisar.
UNEVEN=0
for b in R RE NIR; do
  [[ "${BAND_N[$b]}" -ne "${BAND_N[G]}" ]] && UNEVEN=1
done
if [[ "$UNEVEN" -eq 1 ]]; then
  echo ""
  echo "❌ ERROR: las bandas no están parejas (G=${BAND_N[G]}, R=${BAND_N[R]}, RE=${BAND_N[RE]}, NIR=${BAND_N[NIR]})."
  echo "   Cada captura del M3M son 4 archivos y ODM las empareja por nombre:"
  echo "   si a una captura le falta una banda, la reconstrucción aborta."
  echo ""
  echo "   Capturas incompletas (les falta al menos una banda):"
  # Se listan por prefijo de captura, que es el nombre sin el sufijo de banda.
  find "$DATA_DIR" -maxdepth 1 -iname "*_MS_*.TIF" -printf "%f\n" 2>/dev/null \
    | sed -E 's/_MS_(G|R|RE|NIR)\.TIF$//I' | sort | uniq -c \
    | awk '$1 != 4 {printf "     %s — %d de 4 bandas\n", $2, $1}' | head -20
  n_bad=$(find "$DATA_DIR" -maxdepth 1 -iname "*_MS_*.TIF" -printf "%f\n" 2>/dev/null \
    | sed -E 's/_MS_(G|R|RE|NIR)\.TIF$//I' | sort | uniq -c | awk '$1 != 4' | wc -l)
  [[ "$n_bad" -gt 20 ]] && echo "     … y $((n_bad - 20)) más"
  echo ""
  echo "   Copiá las bandas que faltan, o quitá esas capturas de la carpeta."
  exit 1
fi
