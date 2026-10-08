#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
# RAPTOR Test Runner
# ═══════════════════════════════════════════════════════════════════
# Ejecuta la suite de pruebas de RAPTOR dentro de un contenedor desechable.
#
# Uso:
#   ./run_tests.sh                            # Toda la suite (tests/ -q)
#   ./run_tests.sh tests/test_webapp.py       # Archivo específico
#   ./run_tests.sh -k "odm_slots"             # Filtrar por expresión
#
# El checkout se monta de SOLO LECTURA y se copia a /app dentro del
# contenedor: varios tests (los que llegan a un /start exitoso de la
# webapp) llaman a core.runner.activate_mission(), que reemplaza
# /app/{processing,outputs,preprocessing,data,geovisor/tiles} por symlinks.
# Con el checkout montado directo en /app, eso reescribía los directorios
# del propio repo con links a un tmpdir de pytest que después desaparece.
#
# Nunca correr la suite dentro del contenedor persistente (raptor-web):
# esos mismos tests lanzan docker/entrypoint.sh de verdad.
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

IMAGE="${RAPTOR_IMAGE:-raptor:latest}"
REPO="$(cd "$(dirname "$0")" && pwd)"

if [[ $# -eq 0 ]]; then
  set -- tests/ -q
fi

echo "== Ejecutando pytest en $IMAGE: $* =="
docker run --rm -v "$REPO:/src:ro" --entrypoint bash "$IMAGE" -c '
  set -euo pipefail
  cd /src
  tar --exclude=./venv --exclude=./.git --exclude=./paper --exclude=./runs \
      --exclude="*/__pycache__" --exclude=./.pytest_cache -cf - . | tar -xf - -C /app
  cd /app
  exec python3 -m pytest -p no:cacheprovider "$@"
' pytest "$@"
