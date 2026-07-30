"""Configuración común de los tests.

Los scripts de `scripts/` son ejecutables sueltos, no un paquete: se importan
agregando ese directorio al path, igual que hace el propio
`trim_low_overlap_edges.py` con `compute_vegetation_indices`.

Ninguno tiene efectos al importarse (todo el trabajo está detrás de
`if __name__ == "__main__"`), así que se pueden importar sin tocar disco.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
for p in (REPO, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)
