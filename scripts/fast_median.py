#!/usr/bin/env python3
"""Mediana (size×size) sobre arrays 2D float32 — el motor que el pipeline usa
para las pasadas de outliers del DSM (scripts/dsm_clean.py).

Por qué existe: la etapa clean-dsm de la misión barbosa-chorrera (DSM real de
~65 Mpx, 8226×7880) tardó ~54 min, y las 6 pasadas de mediana 9×9 (3 de
mediana + 3 de MAD) son el grueso de ese tiempo. `scipy.ndimage.median_filter`
corre en UN solo hilo: medido en la imagen base, 43 s por cada 4 Mpx, o sea
~700 s por pasada sobre un DSM de 65 Mpx.

CÓMO SE ACELERA, Y POR QUÉ NO CON OPENCV. La versión anterior de este archivo
delegaba en `cv2.medianBlur` diciendo que corre a ~50-100 Mpx/s. Eso es cierto
pero NO aplica acá: OpenCV solo acepta ventanas mayores a 5×5 sobre imágenes
de 8 bits (`medianBlur` documenta que para ksize > 5 la profundidad tiene que
ser CV_8U), y el DSM es float32 con ventana 9×9. Verificado en la imagen base
(OpenCV 4.12.0): float32 con ksize 3 y 5 funciona, con 7 y 9 lanza
`(-215:Assertion failed) src.depth() == CV_8U`. O sea que ese camino "rápido"
no era lento: no funcionaba, y `make clean-dsm` habría reventado con un
cv2.error después de horas de ODM.

Lo que sí funciona es paralelizar el scipy de siempre: `median_filter` libera
el GIL, así que repartir el array en bloques de filas entre hilos escala casi
lineal con los núcleos. Medido en la imagen base sobre 4 Mpx: 42.9 s en un
hilo → 3.9 s con 16 (11×), con resultado BIT A BIT idéntico. Sobre el DSM de
65 Mpx eso son ~64 s por pasada en vez de ~700 s: las 6 pasadas bajan de
~70 min a ~6.

Los bloques se solapan `size//2` filas (el mismo halo que necesita la ventana)
y el array llega pre-padeado por réplica, así que cada bloque ve exactamente
los mismos vecinos que vería la pasada global — de ahí que el resultado sea
idéntico y no "equivalente". Se verifica en tests/test_fast_median.py.
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from scipy import ndimage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hardware import cpu_count  # noqa: E402

# Debajo de esto el reparto entre hilos cuesta más que lo que ahorra (el pad y
# el arranque de los hilos dominan sobre el filtrado en sí).
MIN_PX_PARA_PARALELIZAR = 1_000_000


def _hilos():
    """Hilos a usar. MAX_CONCURRENCY manda (misma perilla que el resto del
    pipeline); si no, los núcleos reales — la memoria no acota acá: los
    bloques suman el tamaño del array una sola vez, no N veces."""
    override = os.environ.get("MAX_CONCURRENCY")
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            pass
    return max(1, cpu_count())


def median_filter_fast(a, size=9):
    """Mediana size×size con padding de borde por réplica (== scipy
    mode='nearest'). `a` 2D numérico; se castea a float32 contiguo.

    Asume entrada FINITA: en dsm_clean el array llega relleno por el vecino
    válido (EDT) antes de estas pasadas, así que nunca entra NaN acá.
    """
    a = np.ascontiguousarray(a, dtype=np.float32)
    nthr = _hilos()
    if nthr == 1 or a.size < MIN_PX_PARA_PARALELIZAR or a.ndim != 2:
        return ndimage.median_filter(a, size=size, mode="nearest")

    r = size // 2
    alto, ancho = a.shape
    # Pad por réplica UNA vez para todo el array: así cada bloque puede tomar
    # su halo de las filas de arriba/abajo (o de la réplica de borde, en el
    # primero y el último) sin ningún caso especial.
    pad = np.pad(a, r, mode="edge")
    salida = np.empty_like(a)
    filas = max(1, -(-alto // nthr))

    def _bloque(limites):
        y0, y1 = limites
        # El bloque incluye r filas de halo a cada lado; mode="nearest" acá
        # solo afecta a las columnas de los extremos, que ya vienen padeadas.
        parcial = ndimage.median_filter(pad[y0:y1 + 2 * r], size=size, mode="nearest")
        salida[y0:y1] = parcial[r:r + (y1 - y0), r:r + ancho]

    tramos = [(y0, min(y0 + filas, alto)) for y0 in range(0, alto, filas)]
    with ThreadPoolExecutor(max_workers=nthr) as pool:
        for _ in pool.map(_bloque, tramos):
            pass
    return salida
