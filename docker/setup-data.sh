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
# Ver el comentario largo en docker/setup-data-multispectral.sh: cp
# secuencial uno por uno es el cuello de botella real, PERO paralelizarlo a
# lo bruto con todos los núcleos (sin mirar RAM) coincidió con un crash por
# memoria de toda la PC del usuario probándolo en vivo. 12.3 MP: mismo perfil
# que usa ODM para las fotos RGB del H20T/M3T (safe_concurrency en
# entrypoint.sh) — más conservador de lo que hace falta para las térmicas
# (mucho más chicas), pero es un solo NPROCS para todo el script y ante la
# duda gana el número más chico.
NPROCS="$(python3 "$REPO_DIR/scripts/hardware.py" concurrency 12.3 2>/dev/null || echo 4)"

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

# find -L (no el default -P): la webapp puede dejar $SRC como un SYMLINK
# directo a la carpeta del usuario (ver import_local() en webapp/main.py —
# evita duplicar las fotos). find sin -L NO sigue un symlink que sea el
# argumento de partida cuando no termina en "/" — lo trata como un
# archivo suelto de tipo symlink, no como un directorio, así que nunca
# entra a buscar adentro y devuelve 0 resultados aunque las fotos estén
# ahí y sean perfectamente legibles con `ls`. Bug real: rompía el primer
# uso end-to-end de "Usar esta carpeta" con "No se encontraron imágenes".
#
# ── RGB ────────────────────────────────────────────────────────────
echo "Buscando imágenes RGB (*_V.JPG, *_W.JPG) ..."
RGB_FILES=$(find -L "$SRC" -type f \( -iname "*_V.JPG" -o -iname "*_W.JPG" \) 2>/dev/null || true)
RGB_COUNT=$(echo "$RGB_FILES" | grep -c "JPG" || true)

if [[ "$RGB_COUNT" -eq 0 ]]; then
  echo "  ⚠ No se encontraron imágenes RGB."
else
  echo "  Encontradas: $RGB_COUNT"
  # `if`, no `[[ ]] && echo`: con set -e, el exit status de un `while` es el
  # de su ÚLTIMO comando ejecutado — si el último archivo que entrega find
  # YA existe (típico al reanudar una misión), `[[ ! -f ]]` da falso y ESE
  # exit status no-cero tumba todo el script en el `TO_COPY=$(...)` de
  # abajo. Pasó en vivo: misión con todo ya copiado, orden de find puso un
  # archivo existente al final → script moría después de "Encontradas: N"
  # sin ningún mensaje de error. `if` sin `else` siempre sale 0.
  TO_COPY=$(while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    base=$(basename "$f")
    if [[ ! -f "$DATA_DIR/rgb_mosaico/$base" ]]; then
      echo "$f"
    fi
  done <<< "$RGB_FILES")
  COPIED=$(echo "$TO_COPY" | grep -c . || true)
  if [[ "$COPIED" -gt 0 ]]; then
    echo "$TO_COPY" | xargs -P "$NPROCS" -I{} cp {} "$DATA_DIR/rgb_mosaico/"
  fi
  echo "  ✅ $COPIED copiadas a data/rgb_mosaico/"
fi

# ── Térmicas ───────────────────────────────────────────────────────
echo ""
echo "Buscando imágenes térmicas (*_T.JPG) ..."
TH_FILES=$(find -L "$SRC" -type f -iname "*_T.JPG" 2>/dev/null || true)
TH_COUNT=$(echo "$TH_FILES" | grep -c "JPG" || true)

if [[ "$TH_COUNT" -eq 0 ]]; then
  echo "  ⚠ No se encontraron imágenes térmicas."
else
  echo "  Encontradas: $TH_COUNT"
  TO_COPY=$(while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    base=$(basename "$f")
    if [[ ! -f "$DATA_DIR/termica_mosaico/$base" ]]; then
      echo "$f"
    fi
  done <<< "$TH_FILES")
  COPIED=$(echo "$TO_COPY" | grep -c . || true)
  if [[ "$COPIED" -gt 0 ]]; then
    echo "$TO_COPY" | xargs -P "$NPROCS" -I{} cp {} "$DATA_DIR/termica_mosaico/"
  fi
  echo "  ✅ $COPIED copiadas a data/termica_mosaico/"
fi

echo ""
echo "═══ Resumen ──────────────────────────────────"
echo "  RGB:     $(ls "$DATA_DIR/rgb_mosaico/"*_*.JPG 2>/dev/null | wc -l) imágenes"
echo "  Térmico: $(ls "$DATA_DIR/termica_mosaico/"*_T.JPG 2>/dev/null | wc -l) imágenes"
echo ""
echo "Siguiente paso: docker run --gpus all -v $SRC:/input -p 8080:8080 raptor run"
