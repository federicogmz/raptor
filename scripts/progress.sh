#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# Funciones de progreso para el pipeline.
#
# Uso:
#   source scripts/progress.sh
#   pipeline_progress_start "Etapa X" 3 7
#   pipeline_progress_step 50 100 "Procesando..."
#   pipeline_progress_done
# ═══════════════════════════════════════════════════════════════════

_PROGRESS_SCRIPT="$(dirname "${BASH_SOURCE[0]}")/progress.py"

pipeline_header() {
    python3 "$_PROGRESS_SCRIPT" pipeline-header "${MODE:-rgb+thermal}"
}

pipeline_progress_start() {
    local name="$1" stage_n="$2" total="$3"
    python3 "$_PROGRESS_SCRIPT" stage-header "$name" "$stage_n" "$total"
}

pipeline_progress_step() {
    local current="$1" total="$2" label="${3:-}"
    python3 "$_PROGRESS_SCRIPT" bar "$label" "$current" "$total"
}

pipeline_progress_done() {
    local label="${1:-}"
    python3 "$_PROGRESS_SCRIPT" done "$label"
}