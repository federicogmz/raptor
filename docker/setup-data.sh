#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# setup-data.sh — Organiza las imágenes desde la tarjeta SD del dron
#
# Uso:
#   ./docker/setup-data.sh /media/usb/DCIM    # desde la SD del dron
#   ./docker/setup-data.sh ~/vuelo_2024/      # desde una carpeta local
#
# Busca recursivamente archivos *_V.JPG, *_W.JPG (RGB) y *_T.JPG
# (térmicos) y los copia a data/rgb_mosaico/ y data/termica_mosaico/.
# ═════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
DATA_DIR="$REPO_DIR/data"

if [[ $# -lt 1 ]]; then
  echo "Uso: $0 <directorio_fuente>"
  echo ""
  echo "  <directorio_fuente> = carpeta raíz de la SD del dron, o donde"
  echo "  estén las fotos del vuelo. Se buscan recursivamente."
  echo ""
  echo "Ejemplos:"
  echo "  $0 /media/usb/DCIM"
  echo "  $0 ~/Descargas/vuelo_2024-06-15"
  exit 1
fi

SRC="$1"

if [[ ! -d "$SRC" ]]; then
  echo "❌ ERROR: '$SRC' no existe o no es un directorio."
  exit 1
fi

echo "═══ Organizando imágenes desde: $SRC"
echo ""

# Crear directorios destino
mkdir -p "$DATA_DIR/rgb_mosaico" "$DATA_DIR/termica_mosaico"

# ── RGB ────────────────────────────────────────────────────────────
echo "Buscando imágenes RGB (*_V.JPG, *_W.JPG) ..."
RGB_FILES=$(find "$SRC" -type f \( -iname "*_V.JPG" -o -iname "*_W.JPG" \) 2>/dev/null || true)
RGB_COUNT=$(echo "$RGB_FILES" | grep -c "JPG" || true)

if [[ "$RGB_COUNT" -eq 0 ]]; then
  echo "  ⚠ No se encontraron imágenes RGB."
else
  echo "  Encontradas: $RGB_COUNT"
  COPIED=0
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    base=$(basename "$f")
    if [[ ! -f "$DATA_DIR/rgb_mosaico/$base" ]]; then
      cp "$f" "$DATA_DIR/rgb_mosaico/"
      ((COPIED++)) || true
    fi
  done <<< "$RGB_FILES"
  echo "  ✅ $COPIED copiadas a data/rgb_mosaico/"
fi

# ── Térmicas ───────────────────────────────────────────────────────
echo ""
echo "Buscando imágenes térmicas (*_T.JPG) ..."
TH_FILES=$(find "$SRC" -type f -iname "*_T.JPG" 2>/dev/null || true)
TH_COUNT=$(echo "$TH_FILES" | grep -c "JPG" || true)

if [[ "$TH_COUNT" -eq 0 ]]; then
  echo "  ⚠ No se encontraron imágenes térmicas."
else
  echo "  Encontradas: $TH_COUNT"
  COPIED=0
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    base=$(basename "$f")
    if [[ ! -f "$DATA_DIR/termica_mosaico/$base" ]]; then
      cp "$f" "$DATA_DIR/termica_mosaico/"
      ((COPIED++)) || true
    fi
  done <<< "$TH_FILES"
  echo "  ✅ $COPIED copiadas a data/termica_mosaico/"
fi

echo ""
echo "═══ Resumen ──────────────────────────────────"
echo "  RGB:     $(ls "$DATA_DIR/rgb_mosaico/"*_*.JPG 2>/dev/null | wc -l) imágenes"
echo "  Térmico: $(ls "$DATA_DIR/termica_mosaico/"*_T.JPG 2>/dev/null | wc -l) imágenes"
echo ""
echo "Siguiente paso: docker run --gpus all -v $SRC:/input -p 8080:8080 raptor run"
