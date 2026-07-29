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
for b in G R RE NIR; do
  n=$(find "$DATA_DIR" -maxdepth 1 -iname "*_MS_${b}.TIF" 2>/dev/null | wc -l)
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
