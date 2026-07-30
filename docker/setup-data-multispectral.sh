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
# *_MS_NIR.TIF y las copia a data/multiespectral_mosaico/. La banda RGB
# "D" (cámara aparte del M3M, no coalineada con las 4 lentes MS) NO se
# copia acá — es un sensor distinto, fuera de alcance de este módulo (se
# puede reconstruir con el pipeline RGB existente si hace falta).
# ═════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
DATA_DIR="$REPO_DIR/data/multiespectral_mosaico"

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

echo "Buscando bandas MS (*_MS_G.TIF, *_MS_R.TIF, *_MS_RE.TIF, *_MS_NIR.TIF) ..."
MS_FILES=$(find "$SRC" -type f \( -iname "*_MS_G.TIF" -o -iname "*_MS_R.TIF" \
  -o -iname "*_MS_RE.TIF" -o -iname "*_MS_NIR.TIF" \) 2>/dev/null || true)
MS_COUNT=$(echo "$MS_FILES" | grep -c "TIF" || true)

if [[ "$MS_COUNT" -eq 0 ]]; then
  echo "  ⚠ No se encontraron bandas multiespectrales."
else
  echo "  Encontradas: $MS_COUNT"
  COPIED=0
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    base=$(basename "$f")
    if [[ ! -f "$DATA_DIR/$base" ]]; then
      cp "$f" "$DATA_DIR/"
      ((COPIED++)) || true
    fi
  done <<< "$MS_FILES"
  echo "  ✅ $COPIED copiadas a data/multiespectral_mosaico/"
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
