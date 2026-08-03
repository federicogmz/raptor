#!/usr/bin/env python3
"""Clasifica focos térmicos (hotspot) SOLO a partir del ortomosaico térmico,
sin necesitar el multiespectral ni el polígono de área afectada detectado.

El hotspot es un concepto puramente térmico —temperatura ABSOLUTA, ver el
docstring de compute_severity_classes.py para por qué absoluta y no anomalía
relativa— pero antes solo se generaba como subproducto de ese script, que a
su vez exige detect_area_afectada.py, que a su vez EXIGE multiespectral (la
señal primaria de la detección de área afectada es NDVI). Una misión
RGB+térmico sin vuelo M3M se quedaba sin la capa de hotspot sin ninguna
necesidad real: nada en su cálculo depende de NDVI ni de un polígono.

Se muestra SIN recortar a ningún polígono de área afectada —no hay uno sin
multiespectral—: cubre toda la extensión con dato térmico válido, en vez de
solo el interior de un perímetro que acá no existe.

En una misión que SÍ tiene multiespectral, compute_severity_classes.py sigue
siendo la fuente del hotspot (recortado al área detectada, más específico);
docker/entrypoint.sh no llama a este script en ese caso, para no pisar ese
resultado con uno sin recortar.

Uso: python3 scripts/compute_thermal_hotspot.py
Lee: outputs/thermal_orthomosaic.tif
Escribe: outputs/termico_hotspot_class.tif
"""
import os
import sys

import numpy as np
from osgeo import gdal

gdal.UseExceptions()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# classify()/write_class_tif() son funciones puras (sin I/O propio más que
# los argumentos explícitos) y compute_severity_classes.py no tiene efectos
# de importación — reusarlas evita mantener los mismos cortes de 40/60/88°C
# copiados en dos archivos.
from compute_severity_classes import TERM_BREAKS, classify, write_class_tif

TH_PATH = "outputs/thermal_orthomosaic.tif"
OUT_PATH = "outputs/termico_hotspot_class.tif"
LABELS = ["normal", "elevado", "caliente", "foco activo"]


def main():
    if not os.path.isfile(TH_PATH):
        print(f"❌ ERROR: {TH_PATH} no encontrado (¿corrió trim-edges-thermal?)")
        sys.exit(1)

    ds = gdal.Open(TH_PATH)
    gt, proj = ds.GetGeoTransform(), ds.GetProjection()
    temp = ds.GetRasterBand(1).ReadAsArray()
    ds = None

    valid = np.isfinite(temp)
    n_valid = int(valid.sum())
    thc = classify(temp, TERM_BREAKS, valid)

    for i, lbl in enumerate(LABELS, 1):
        n = int((thc == i).sum())
        pct = 100 * n / n_valid if n_valid else 0
        print(f"  térmico {lbl}: {n:,} px ({pct:.1f}%)")

    write_class_tif(OUT_PATH, thc, gt, proj)
    print(f"✅ {OUT_PATH} (hotspot térmico sobre toda la cobertura — "
          f"sin multiespectral no hay polígono de área afectada al que recortar)")


if __name__ == "__main__":
    main()
