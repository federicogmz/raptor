#!/usr/bin/env python3
"""gdal.Open() con reintentos — protección contra un fallo transitorio de
lectura reportado en vivo: compute_flight_quality.py falló con
`RuntimeError: 'outputs/rgb_orthomosaic.tif' not recognized as being in a
supported file format` sobre un archivo que, reabierto segundos después, era
perfectamente válido. No se identificó una causa de fondo (probable hiccup
del filesystem bajo I/O concurrente pesado — varios sensores reconstruyendo
y varios COG escribiéndose a la vez, mismo momento del pipeline donde
confidence-mask y flight-quality leen en paralelo los ortomosaicos grandes
que cada sensor recién terminó de escribir), así que no hay garantía de que
no vuelva a pasar — esto lo vuelve un fallo transitorio recuperable en vez
de un corte de la corrida entera.

Uso: en vez de `gdal.Open(path)`, usar `gdal_open_retry(path)` en los
scripts del análisis cruzado (confidence_mask.py, compute_flight_quality.py)
que abren ortomosaicos de sensores que pueden haber terminado de escribirse
hace apenas segundos.
"""
import time

from osgeo import gdal

DEFAULT_TRIES = 3
DEFAULT_DELAY_S = 2.0


def gdal_open_retry(path, tries=DEFAULT_TRIES, delay_s=DEFAULT_DELAY_S, **kwargs):
    """gdal.Open(path, **kwargs), reintentando con espera fija si falla.

    Deja pasar cualquier excepción que NO sea la de "formato no reconocido"
    tal cual (p.ej. archivo realmente inexistente) — reintentar eso no
    cambia nada y solo demora el error real."""
    last_exc = None
    for intento in range(1, tries + 1):
        try:
            return gdal.Open(path, **kwargs)
        except RuntimeError as exc:
            last_exc = exc
            if "not recognized as being in a supported file format" not in str(exc):
                raise
            if intento < tries:
                print(f"  ⚠ {path}: lectura falló (intento {intento}/{tries}), "
                      f"reintentando en {delay_s:.0f}s — {exc}")
                time.sleep(delay_s)
    raise last_exc
