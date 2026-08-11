#!/usr/bin/env python3
"""
Sistema de barras de progreso para el pipeline RAPTOR.

Provee dos mecanismos:
  1. progress.py <etapa> <paso_actual> <total_pasos> [--done]
     → Imprime barra de progreso de la etapa actual.

  2. Variables de entorno para progreso global:
     PIPELINE_STAGE = nombre de la etapa actual
     PIPELINE_STAGE_N = número de etapa (1..N)
     PIPELINE_TOTAL_STAGES = total de etapas

Uso desde bash:
  source scripts/progress.sh
  pipeline_progress_start "Conversión SDK" 4 7
  pipeline_progress_step 1 135   # paso 1 de 135
  pipeline_progress_done

O desde Python:
  from scripts.progress import StageProgress
  p = StageProgress("Ortorectificación", 5, 7)
  p.update(50, 100)
  p.done()
"""

import os
import sys
import shutil
import time


# ── Colores y formato ──────────────────────────────────────────────
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    RESET = '\033[0m'

# ── Canal estructurado opcional para consumidores externos ─────────
# Las barras normales van a stderr con ANSI/\r (para terminal humana). La
# webapp (webapp/main.py, vía core/runner.py) no quiere parsear eso — si
# PROGRESS_FILE está seteado, además se apendean líneas planas tab-separadas
# key=value, fáciles de tailear sin tocar el comportamiento normal de
# terminal.
_PROGRESS_FILE = os.environ.get("PROGRESS_FILE")


def _emit_progress(event, **kwargs):
    if not _PROGRESS_FILE:
        return
    try:
        with open(_PROGRESS_FILE, "a") as f:
            # t=epoch en TODOS los eventos, no solo "stage": el HUD del
            # geovisor lo usa para calcular cuánto duró cada fase (diff entre
            # el t de una fase y el de la siguiente) — con la hora del
            # SERVIDOR, no la del navegador, así que sigue siendo correcto
            # aunque se reconecte o recargue la página a mitad de una corrida
            # de horas (la SSE reproduce el historial completo desde el
            # principio en cada conexión nueva, ver /api/missions/{m}/events).
            parts = [event, f"t={int(time.time())}"] + [f"{k}={v}" for k, v in kwargs.items()]
            f.write("\t".join(str(p) for p in parts) + "\n")
    except Exception:
        pass

# ── Ancho de terminal ──────────────────────────────────────────────
def term_width():
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 80


def format_bar(filled, total, width, label="", stage_info=""):
    """Renderiza una barra de progreso."""
    pct = filled / total if total > 0 else 0
    bar_width = width - 30  # espacio para etiquetas
    if bar_width < 10:
        bar_width = 10

    n_filled = int(bar_width * pct)
    n_empty = bar_width - n_filled

    bar = "█" * n_filled + "░" * n_empty

    if stage_info:
        header = f"{Colors.BOLD}{Colors.CYAN}{stage_info}{Colors.RESET} "
    else:
        header = ""

    if label:
        lbl = f" {label}"
    else:
        lbl = ""

    line = f"{header}{Colors.GREEN}{bar}{Colors.RESET} {pct*100:5.1f}%{lbl}"
    return line


def print_progress(filled, total, label="", stage_info=""):
    """Imprime una línea de progreso (sobrescribe la anterior con \\r)."""
    w = term_width()
    bar = format_bar(filled, total, w, label, stage_info)
    # Clear to end of line, then print
    sys.stderr.write(f"\r\033[K{bar}")
    sys.stderr.flush()
    _emit_progress("bar", current=filled, total=total, label=label)


def print_done(label="", stage_info=""):
    """Imprime barra completada."""
    w = term_width()
    bar = format_bar(1, 1, w, label, stage_info)
    sys.stderr.write(f"\r\033[K{bar}\n")
    sys.stderr.flush()
    _emit_progress("done", label=label)


def print_stage_header(stage_name, stage_n, total_stages):
    """Imprime encabezado de etapa."""
    w = term_width()
    sep = "─" * (w - 4)
    header = (f"{Colors.BOLD}{Colors.YELLOW}"
              f"[{stage_n}/{total_stages}] {stage_name}"
              f"{Colors.RESET}")
    sys.stderr.write(f"\n{header}\n")
    sys.stderr.flush()
    _emit_progress("stage", name=stage_name, n=stage_n, total=total_stages)


# ── CLI ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: progress.py <etapa> <actual> <total> [--done]", file=sys.stderr)
        print("      progress.py stage-header <nombre> <n> <total>", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "stage-header":
        name = sys.argv[2]
        n = int(sys.argv[3])
        total = int(sys.argv[4])
        print_stage_header(name, n, total)

    elif cmd == "bar":
        label = sys.argv[2] if len(sys.argv) > 2 else ""
        current = int(sys.argv[3]) if len(sys.argv) > 3 else 0
        total = int(sys.argv[4]) if len(sys.argv) > 4 else 1
        stage_info = sys.argv[5] if len(sys.argv) > 5 else ""
        print_progress(current, total, label, stage_info)

    elif cmd == "done":
        label = sys.argv[2] if len(sys.argv) > 2 else ""
        stage_info = sys.argv[3] if len(sys.argv) > 3 else ""
        print_done(label, stage_info)

    elif cmd == "pipeline-header":
        mode = sys.argv[2] if len(sys.argv) > 2 else "rgb+thermal"
        w = term_width()
        sys.stderr.write(f"\n{Colors.BOLD}{'═'*w}{Colors.RESET}\n")
        sys.stderr.write(f"{Colors.BOLD}  RAPTOR — {mode.upper()}{Colors.RESET}\n")
        sys.stderr.write(f"{Colors.BOLD}{'═'*w}{Colors.RESET}\n\n")
        sys.stderr.flush()