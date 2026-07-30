#!/usr/bin/env python3
"""Limpia el DSM de ODM: descarta relleno sintético + rechaza outliers.

ODM rellena huecos de su propia reconstrucción (zonas sin puntos MVS: agua,
sombra profunda, baja textura) con un valor CONSTANTE (no marca esas celdas
como nodata) — se verificó: ~30% de las celdas "válidas" del dsm.tif crudo
son exactamente iguales a un único valor (la mediana global), una huella
estadísticamente imposible para terreno real. Si no se detecta, esa altura
falsa se usa para proyectar el térmico ahí → contamina el mosaico.

Detección: varianza local ~0 en una ventana 5×5 (terreno real medido por
fotogrametría SIEMPRE tiene algo de ruido de reconstrucción; una ventana de
25 celdas con varianza exactamente cero solo puede ser relleno sintético).
Esas celdas se tratan como NODATA real, no se rellenan con ningún valor —
el ortorectificador térmico ya descarta correctamente los píxeles sin DSM.

Sobre lo que queda (terreno real), se aplica el rechazo de outliers
iterativo por mediana+MAD de siempre (picos puntuales de reconstrucción).
"""
import gc
import os
import numpy as np
from osgeo import gdal
from scipy import ndimage

gdal.UseExceptions()

# Fuente: el DSM NATIVO de ODM en processing/ (donde ODM lo escribe). NO se
# mantiene una copia intermedia en outputs/ — eso fue justo lo que causó el bug
# del DSM degradado (una copia vieja de 30cm/69.5% se quedó desincronizada del
# nativo 20cm/96.4%). outputs/ contiene solo el producto final (dsm.tif limpio).
#
# DSM_SRC es override: en una misión SIN vuelo RGB/térmico (solo multiespectral
# M3M, MODE=none) no existe processing/rgb_odm/ — el entrypoint apunta esto al
# DSM del proyecto multiespectral, que también corre con --dsm. El algoritmo de
# limpieza es idéntico (no depende del sensor, solo de la geometría del DSM).
SRC = os.environ.get("DSM_SRC", "processing/rgb_odm/odm_dem/dsm.tif")
OUT = "outputs/dsm.tif"                       # producto: DSM limpio
MIN_COVERAGE = 0.85   # un DSM bueno de este vuelo cubre ~96%; <85% = degradado
MAX_RES_CM   = 25.0   # ODM nativo da 20cm; >25cm = corrida de baja calidad
FLAT_VAR_THRESH = 1e-5  # m², ventana 5x5 — terreno real nunca es esto de plano
PYRAMID_FACTOR = 16     # downsample para estimar tendencia de gran escala
                        # en huecos reales interiores (agua, sombra, vegetación
                        # densa) — NO se rellenan con un valor constante, se
                        # interpola la tendencia del terreno circundante a baja
                        # resolución (sin pretender recuperar detalle que nunca
                        # se midió). Solo el hueco EXTERIOR (fuera del polígono
                        # de vuelo, toca el borde del raster) se deja sin tocar.

ds = gdal.Open(SRC)
gt = ds.GetGeoTransform()
W, H = ds.RasterXSize, ds.RasterYSize
z = ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
res = gt[1]

m_raw = np.isfinite(z) & (z > -500)
nd = ds.GetRasterBand(1).GetNoDataValue()
if nd is not None and np.isfinite(nd):
    m_raw &= (z != nd)
print(f"DSM fuente: {SRC}")
print(f"DSM {W}x{H} @ {res*100:.0f}cm, válidos (crudo) {100*m_raw.mean():.1f}%")

# Chequeo de sanidad: alertar si el DSM de entrada parece degradado (la causa
# raíz del problema fue procesar silenciosamente un DSM malo).
if m_raw.mean() < MIN_COVERAGE or res * 100 > MAX_RES_CM:
    print(f"  ⚠ ADVERTENCIA: el DSM de entrada parece DEGRADADO "
          f"(cobertura {100*m_raw.mean():.1f}% < {100*MIN_COVERAGE:.0f}% o "
          f"resolución {res*100:.0f}cm > {MAX_RES_CM:.0f}cm). "
          f"¿Es el DSM nativo de la última corrida de ODM? Revisa {SRC}")

# Detectar relleno sintético: varianza local ~0 dentro de lo "válido"
# MEMORIA: este script procesa DSMs de cientos de millones de píxeles (del
# orden de 17000x19000 = 324M px) como variables de módulo, que nunca se
# liberan solas por scope, y varias operaciones de acá generan arrays de
# tamaño completo (2.6-5.2 GB cada uno). Sin cuidado explícito se acumulan
# TODAS simultáneamente hasta el final del script y el kernel mata el proceso
# por falta de memoria. De ahí las dos reglas: float32 en vez de float64
# (mitad de memoria) y `del` tan pronto un array deja de usarse, para que el
# refcounting de CPython lo libere de inmediato.
#
# NUMÉRICA: la varianza local se calcula con la fórmula de DOS pasadas
# (centrar por la media local antes de elevar al cuadrado), no con
# E[X²]-E[X]². Esta última sufre cancelación catastrófica en float32 cuando X
# tiene un offset grande, que es exactamente el caso de una elevación absoluta:
# a ~2000 m, X² ~ 4·10⁶ ya satura casi toda la precisión de float32 (~7
# dígitos) y no quedan dígitos para resolver una varianza objetivo de 1e-5. El
# resultado sería ruido de redondeo en vez de la varianza real, y con él este
# script puede marcar como "relleno sintético" más del 70% de un DSM cuya
# varianza real es del orden del 5%. Con dos pasadas el residuo local es chico
# (orden de cm de rugosidad real) sin importar la escala
# absoluta de Z, así que el cuadrado es numéricamente estable en float32
# también. Mismo conteo de arrays temporales que antes, sin regresión de
# memoria.
work0 = np.where(m_raw, z, np.float32(0.0))
local_mean = ndimage.uniform_filter(work0, 5, mode="nearest")
residual = work0 - local_mean
del work0, local_mean
local_var = ndimage.uniform_filter(residual * residual, 5, mode="nearest")
del residual
fake_flat = m_raw & (local_var < FLAT_VAR_THRESH)
del local_var
print(f"  relleno sintético detectado: {int(fake_flat.sum()):,} px "
      f"({100*fake_flat.sum()/m_raw.sum():.1f}% de lo 'válido') — se marca como nodata real")

m = m_raw & ~fake_flat
del fake_flat
print(f"  válidos reales: {100*m.mean():.1f}%")

# Para el rechazo de outliers necesitamos números en todo el array (no NaN);
# se usa el vecino válido más cercano SOLO para esta estadística local — el
# resultado final igual queda NaN donde m es False (no se inventa altura).
# return_indices=True sobre la imagen COMPLETA es el array más pesado del
# script (shape (2,H,W) int64 = ~5.2GB para este DSM) — se libera apenas se
# usa, no se necesita de nuevo.
_, idx = ndimage.distance_transform_edt(~m, return_indices=True)
work = np.where(m, z, z[tuple(idx)])
del idx
gc.collect()

for it in range(3):
    med = ndimage.median_filter(work, size=9, mode="nearest")
    mad = ndimage.median_filter(np.abs(work - med), size=9, mode="nearest") + 1e-3
    outlier = m & (np.abs(work - med) > np.maximum(4.0 * mad, 3.0))
    work[outlier] = med[outlier]
    print(f"  iter {it+1}: {int(outlier.sum()):,} outliers reemplazados")
del med, mad, outlier
gc.collect()

# NOTA: aquí había un ndimage.median_filter(work, size=5) GLOBAL incondicional
# — difuminaba TODO el DSM (terreno real incluido), no solo los píxeles que
# la corrección de outliers de arriba ya reemplazó selectivamente. Se verificó:
# 44.3% del DSM "válido" quedaba con rugosidad local anormalmente baja
# (std<0.05m en ventana 9x9) por este filtro — terreno fotogramétrico real
# siempre tiene ruido de reconstrucción, ese aplanado es artefacto de
# procesamiento, no terreno real. Eliminado: la corrección iterativa de
# outliers ya es selectiva y suficiente.

resid = z[m] - work[m]
print(f"corrección aplicada sobre datos reales: std={np.nanstd(resid):.2f}m")
del z, resid

# Relleno de huecos REALES interiores (no plataformas falsas, ya descartadas
# arriba) — agua, sombra profunda, vegetación densa donde el MVS no triangula.
# Se excluye SOLO el hueco exterior verdadero (toca el borde del raster: fuera
# del polígono de vuelo). Todo hueco interior, sin importar tamaño, se rellena
# con la TENDENCIA de gran escala del terreno circundante (no un valor
# constante ni detalle inventado): se reduce la resolución (factor
# PYRAMID_FACTOR), se interpola a baja resolución (donde cualquier hueco es
# chico) y se reescala — análogo a cómo un mesh de Agisoft "cierra" superficies
# de agua sin textura usando la forma global, no un parche plano.
lbl, n = ndimage.label(~m)
touches_border = np.unique(np.concatenate([lbl[0, :], lbl[-1, :], lbl[:, 0], lbl[:, -1]]))
exterior = np.isin(lbl, touches_border[touches_border > 0])
del lbl, touches_border
interior_holes = (~m) & ~exterior
del exterior
print(f"  huecos reales: {n:,} | exterior (fuera de vuelo): {int(interior_holes.size - m.sum() - interior_holes.sum()):,}px | "
      f"interiores rellenables: {int(interior_holes.sum()):,}px")
gc.collect()

# Esta rama trabaja en la piramide reducida (factor PYRAMID_FACTOR): los
# arrays aqui son ~PYRAMID_FACTOR**2 veces mas chicos, no son el problema de
# memoria — solo `trend` al final vuelve a tamaño completo.
Hs, Ws = H // PYRAMID_FACTOR + 1, W // PYRAMID_FACTOR + 1
low_val = ndimage.zoom(np.where(m, work, np.float32(0.0)), (Hs / H, Ws / W), order=1)
low_wt = ndimage.zoom(m.astype(np.float32), (Hs / H, Ws / W), order=1)
low_est = np.where(low_wt > 1e-3, low_val / np.maximum(low_wt, 1e-9), np.nan)
del low_val, low_wt
_, lidx = ndimage.distance_transform_edt(~np.isfinite(low_est), return_indices=True)
low_filled = np.where(np.isfinite(low_est), low_est, low_est[tuple(lidx)])
del low_est, lidx
low_smooth = ndimage.gaussian_filter(low_filled, 1.0, mode="nearest")
del low_filled
trend = ndimage.zoom(low_smooth, (H / Hs, W / Ws), order=1)[:H, :W].astype(np.float32)
del low_smooth
gc.collect()

filled = np.where(interior_holes, trend, work)
del trend, work
m_final = m | interior_holes
del interior_holes
out = np.where(m_final, filled, np.nan).astype(np.float32)
del filled, m_final
gc.collect()
print(f"cobertura final: {100*np.isfinite(out).mean():.1f}% (antes {100*m_raw.mean():.1f}% incluyendo relleno falso de ODM, "
      f"{100*m.mean():.1f}% real estricto sin relleno)")

drv = gdal.GetDriverByName("GTiff")
o = drv.Create(OUT, W, H, 1, gdal.GDT_Float32, ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_NEEDED"])
o.SetGeoTransform(gt)
o.SetProjection(ds.GetProjection())
o.GetRasterBand(1).WriteArray(out)
o.GetRasterBand(1).SetNoDataValue(float("nan"))
o = None
print(f"✅ {OUT}")
